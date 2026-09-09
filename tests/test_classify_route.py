"""
classify_route 路由判别单元测试

覆盖入口意图路由的本地零成本快判（_fast_route）与模型输出解析（_parse_route）：
  - 数据类问题 → sql
  - 文档类问题 → doc
  - 数据+文档/跨域词共存 → hybrid
  - 闲聊词 → chat
  - 无法确定 → None（交给 LLM 兜底）
纯本地逻辑，不发起任何模型/网络调用。
"""

from app.agent.nodes.classify_route import _fast_route, _parse_route


class TestFastRoute:
    """本地特征词快判路由"""

    def test_数据问题走sql(self):
        assert _fast_route("统计华北地区的销售总额") == "sql"

    def test_文档问题走doc(self):
        assert _fast_route("产品手册里怎么配置参数") == "doc"

    def test_数据与文档词共存走hybrid(self):
        assert _fast_route("结合数仓数据和产品文档分析销量") == "hybrid"

    def test_跨域词加数据词走hybrid(self):
        assert _fast_route("对照文档看下销售额为什么降了") == "hybrid"

    def test_纯闲聊走chat(self):
        assert _fast_route("你好呀") == "chat"

    def test_空输入走chat(self):
        assert _fast_route("   ") == "chat"

    def test_数据加谢谢仍走sql非chat(self):
        # 混合输入含闲聊词也不应被误判成闲聊
        assert _fast_route("统计一下销售额，谢谢") == "sql"

    def test_无法确定返回None(self):
        assert _fast_route("你能帮我看下这个文件吗") is None


class TestParseRoute:
    """模型输出路由解析（含 fail-open 兜底）"""

    def test_解析合法route(self):
        assert _parse_route('{"route": "sql"}') == "sql"

    def test_解析带杂讯JSON(self):
        # 兼容模型在 JSON 外包代码块的情况
        assert _parse_route('```json\n{"route": "doc"}\n```') == "doc"

    def test_未知route_failopen为hybrid(self):
        # 返回不在合法集合里的值，默认兜底 hybrid（宁可多跑不误拦）
        assert _parse_route('{"route": "unknown_thing"}') == "hybrid"

    def test_完全无法解析_failopen为hybrid(self):
        assert _parse_route("抱歉我不太懂你在说什么") == "hybrid"
