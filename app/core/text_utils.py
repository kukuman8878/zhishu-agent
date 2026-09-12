"""
文本数字工具

bge 等语义向量模型对数字不敏感（"2025年第一季度"与"2025年第二季度"相似度仍很高），
但问数场景下数字差异往往意味着口径不同（年份/季度/月份/TopN/金额阈值）。
这里提供数字序列提取（含中文序数归一化）与一致性比较，供知识库复用与去重做口径守卫。
"""

import re
from datetime import datetime

# 匹配整数与小数（归一化后文本里的阿拉伯数字）
_NUM_PATTERN = re.compile(r"\d+(?:\.\d+)?")

# 匹配 19xx/20xx 年份，用于"未来年份不复用沉淀答案"的时间口径守卫
_YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}")

# 中文数字"第X"序数（第1~第999 级别）：归一化成阿拉伯数字再参与数字口径比较
_CN_ORDINAL_PATTERN = re.compile(r"第([零一二两三四五六七八九十百千]+)")

# 中文个位数字映射表
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


# 把中文数字字符串转成整数（支持十/百/千位的简单组合，如"十二"→12、"二十一"→21）；参数 text=中文数字串
def _cn_to_int(text: str) -> int:
    """把简单中文数字串转换为整数（个位到千位组合）"""
    total = 0
    current = 0
    for ch in text:
        if ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        elif ch == "千":
            total += (current or 1) * 1000
            current = 0
        elif ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
    return total + current


# 把文本里的中文序数（第X）归一化为阿拉伯数字；参数 text=原始文本
def _normalize_cn_ordinals(text: str) -> str:
    """把"第X"形式的中文序数替换为阿拉伯数字，使中文/阿拉伯写法口径可比"""

    def _repl(match: re.Match) -> str:
        return f"第{_cn_to_int(match.group(1))}"

    return _CN_ORDINAL_PATTERN.sub(_repl, text or "")


# 提取文本中的数字序列（按出现顺序，中文序数先归一化）；参数 text=待提取文本
def extract_numbers(text: str) -> list[str]:
    """提取文本中的数字序列（含归一化后的中文序数），用于时间/TopN 口径比较"""
    normalized = _normalize_cn_ordinals(text or "")
    return _NUM_PATTERN.findall(normalized)


# 比较两个文本的数字序列是否完全一致；参数 a/b=两个待比较文本
def same_numbers(a: str, b: str) -> bool:
    """两个文本的数字序列一致返回 True；不一致视为口径不同（年份/季度/TopN 变化）"""
    return extract_numbers(a) == extract_numbers(b)


# 省略式追问的起始词：这类问句依赖上一轮上下文才有意义（如"那按月呢"）
_CONTEXT_SHORTCUT_LEADS = ("那", "这", "也", "还", "再", "换", "按", "并", "或")

# 业务指标关键词：用于知识复用/去重时的指标口径守卫。
# bge 对单个业务词变化不敏感（"会员数量"与"订单数量"相似度仍很高），
# 两个问句都含指标词且交集为空时视为口径不同，不互相复用
_METRIC_KEYWORDS = (
    "销售",
    "销量",
    "订单",
    "会员",
    "客户",
    "退款",
    "退货",
    "库存",
    "利润",
    "毛利",
    "营收",
    "收入",
    "客单",
    "金额",
    "成本",
    "gmv",
    "aov",
)


# 判断文本是否包含晚于当前年份的年份数字（如问 2030 年）；参数 text=待判断文本
def has_future_year(text: str) -> bool:
    """文本含未来年份返回 True，用于跳过知识沉淀复用（避免历史答案时间口径错配）"""
    current_year = datetime.now().year
    return any(int(year) > current_year for year in _YEAR_PATTERN.findall(text or ""))


# 判断文本是否为依赖上下文的省略式追问（如"那按月呢"），这类问句沉淀后脱离上下文无意义；参数 text=待判断文本
def is_context_shortcut(text: str) -> bool:
    """省略式追问（短问句且以追问词开头）返回 True，用于知识沉淀时跳过此类问句"""
    stripped = (text or "").strip()
    return (
        len(stripped) <= 8
        and len(stripped) > 0
        and stripped[0] in _CONTEXT_SHORTCUT_LEADS
    )


# 提取文本命中的业务指标关键词集合；参数 text=待提取文本
def metric_keywords(text: str) -> set[str]:
    """提取文本命中的业务指标关键词集合（小写化处理英文指标）"""
    lowered = (text or "").lower()
    return {keyword for keyword in _METRIC_KEYWORDS if keyword in lowered}


# 地区/大区关键词：知识复用前要求地区口径一致，避免"华中"问题复用到"华南"答案
_REGION_KEYWORDS = ("华北", "华东", "华南", "华中", "西南", "西北", "东北")


# 提取文本命中的地区关键词集合；参数 text=待提取文本
def region_keywords(text: str) -> set[str]:
    """提取文本命中的地区/大区关键词集合"""
    return {region for region in _REGION_KEYWORDS if region in (text or "")}


# 比较两个文本的地区口径是否兼容：都含地区但交集为空视为不同地区（如华中 vs 华南）；参数 a/b=待比较文本
def same_region_scope(a: str, b: str) -> bool:
    """两个文本的地区口径不冲突返回 True；一方无地区或地区有交集视为兼容"""
    regions_a = region_keywords(a)
    regions_b = region_keywords(b)
    if regions_a and regions_b and not (regions_a & regions_b):
        return False
    return True


# 比较两个文本的指标口径是否兼容：两者都含指标词但交集为空视为口径不同；参数 a/b=两个待比较文本
def same_metric_domain(a: str, b: str) -> bool:
    """两个文本的指标关键词不冲突返回 True；冲突（如会员 vs 订单）返回 False"""
    keywords_a = metric_keywords(a)
    keywords_b = metric_keywords(b)
    if keywords_a and keywords_b and not (keywords_a & keywords_b):
        return False
    return True
