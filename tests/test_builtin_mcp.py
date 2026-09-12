"""
内置本地工具 MCP Server 单元测试

覆盖 app/mcp_servers/builtin_server.py 中无需进程/网络即可验证的纯逻辑：
  - 安全算术计算器（白名单运算符、格式、拒绝危险表达式）
  - 单位换算（长度/重量、类型不符、未知单位）
  - 时间工具（默认时区、未知时区兜底）
"""

from app.mcp_servers.builtin_server import (
    calculate,
    convert_unit,
    get_current_time,
)


class TestCalculate:
    """安全算术计算器"""

    def test_四则运算与优先级(self):
        assert calculate("(12+8)*3/5") == "12"
        assert calculate("2+3*4") == "14"

    def test_整数结果不带小数点(self):
        assert calculate("10/2") == "5"

    def test_幂运算(self):
        assert calculate("2**10") == "1024"

    def test_拒绝函数调用(self):
        # 只允许算术表达式，函数调用/名字访问都应被拒绝
        result = calculate("__import__('os').system('ls')")
        assert result.startswith("无法计算")

    def test_拒绝过大指数(self):
        assert "指数过大" in calculate("9**9**9")

    def test_非法表达式给出提示(self):
        assert calculate("1+").startswith("无法计算")


class TestConvertUnit:
    """单位换算"""

    def test_长度换算(self):
        assert convert_unit(1, "km", "m") == "1 km = 1000 m"
        assert convert_unit(10, "km", "mile") == "10 km = 6.213712 mile"

    def test_重量换算(self):
        assert convert_unit(1, "kg", "g") == "1 kg = 1000 g"

    def test_单位类型不一致报错(self):
        assert "类型不一致" in convert_unit(1, "km", "kg")

    def test_未知单位报错(self):
        assert "不支持的单位" in convert_unit(1, "光年", "m")


class TestCurrentTime:
    """时间工具"""

    def test_默认时区返回星期与北京时间(self):
        out = get_current_time()
        assert "Asia/Shanghai" in out
        assert "星期" in out

    def test_未知时区兜底提示(self):
        assert "未知时区" in get_current_time("Mars/Olympus")
