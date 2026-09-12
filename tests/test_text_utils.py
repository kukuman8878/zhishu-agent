"""
text_utils 口径守卫单元测试

验证知识沉淀复用/去重前的数字口径、指标口径与省略式追问判定逻辑。
这些都是纯函数，不依赖数据库或模型，跑得快、稳定。
"""

from app.core.text_utils import (
    extract_numbers,
    has_future_year,
    is_context_shortcut,
    metric_keywords,
    region_keywords,
    same_metric_domain,
    same_numbers,
    same_region_scope,
)


class TestExtractNumbers:
    """数字提取与中文序数归一化"""

    def test_提取普通数字序列(self):
        assert extract_numbers("2025年销售了1234.5万元") == ["2025", "1234.5"]

    def test_中文序数归一化为阿拉伯(self):
        # "第一季度" 与 "第1季度" 应提取出相同数字
        assert extract_numbers("统计第一季度销售") == ["1"]
        assert extract_numbers("统计第1季度销售") == ["1"]

    def test_中文多位数序数(self):
        assert extract_numbers("第12季度") == ["12"]
        assert extract_numbers("第二十一季度") == ["21"]

    def test_无数字返回空(self):
        assert extract_numbers("纯文本没有数字") == []


class TestSameNumbers:
    """数字序列一致性（年份/季度/TopN 口径守卫）"""

    def test_数字一致返回True(self):
        assert same_numbers("2025年第一季度销售", "2025年第1季度销售") is True

    def test_年份不同返回False(self):
        assert same_numbers("2024年销售总额", "2025年销售总额") is False

    def test_季度不同返回False(self):
        assert same_numbers("第一季度销售额", "第二季度销售额") is False

    def test_topN不同返回False(self):
        assert same_numbers("销量Top10商品", "销量Top20商品") is False


class TestMetricKeywords:
    """业务指标关键词提取"""

    def test_提取命中指标(self):
        assert "销售" in metric_keywords("统计销售额")
        assert "会员" in metric_keywords("分析会员数量")

    def test_大小写归一(self):
        assert "gmv" in metric_keywords("统计GMV")

    def test_未命中为空(self):
        assert metric_keywords("你好") == set()


class TestSameMetricDomain:
    """指标口径守卫：两问都含指标词但无交集视为口径不同"""

    def test_同指标兼容(self):
        assert same_metric_domain("会员数量", "会员总数") is True

    def test_冲突指标不复用(self):
        # bge 对 "会员数量" vs "订单数量" 不敏感，靠此守卫拦截
        assert same_metric_domain("统计会员数量", "统计订单数量") is False

    def test_一方无指标词视为兼容(self):
        assert same_metric_domain("这个数是多少", "统计销售额") is True

    def test_双方都无指标词视为兼容(self):
        assert same_metric_domain("介绍一下", "你在干嘛") is True


class TestHasFutureYear:
    """未来年份守卫（历史沉淀答案不能复用到未来时间）"""

    def test_未来年份命中(self):
        assert has_future_year("预测2030年的销售额") is True

    def test_当前或历史年份不命中(self):
        assert has_future_year("2024年的销售额") is False

    def test_无年份不命中(self):
        assert has_future_year("统计华北地区的销售总额") is False

    def test_空串不命中(self):
        assert has_future_year("") is False


class TestSameRegionScope:
    """地区口径守卫（不同地区不复用，防止"华中"复用到"华南"答案）"""

    def test_不同地区不兼容(self):
        assert (
            same_region_scope("统计华中地区的订单总量", "统计华南地区订单的销售额")
            is False
        )

    def test_相同地区兼容(self):
        assert same_region_scope("统计华南地区的销售额", "华南地区订单量") is True

    def test_一方无地区视为兼容(self):
        assert same_region_scope("统计销售额", "华南地区订单量") is True

    def test_提取地区关键词(self):
        assert region_keywords("华东和华南对比") == {"华东", "华南"}
        assert region_keywords("没有地区的问句") == set()


class TestIsContextShortcut:
    """省略式追问判定（如"那按月呢"不沉淀）"""

    def test_省略式追问(self):
        assert is_context_shortcut("那按月呢") is True

    def test_普通问题不算(self):
        assert is_context_shortcut("统计华北地区的销售总额") is False

    def test_空串不算(self):
        assert is_context_shortcut("") is False
