"""
内置本地工具 MCP Server

用途：以 stdio 方式对外暴露一组无需任何外部 API Key 的通用小工具，
供智数闲聊/域外问答节点（answer_general）绑定给廉价 chat_llm 使用，
回答当前时间、简单计算、单位换算等日常问题。

启动方式（由 conf/app_config.yaml 的 mcp.servers.builtin 配置拉起）：
    python app/mcp_servers/builtin_server.py

本文件刻意只依赖标准库与 mcp 包，不 import 项目内其他模块，
保证作为独立子进程启动时零副作用。
"""

import ast
import operator
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("zhishu-builtin")

# 中文星期映射，供时间/日期类工具输出
_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 允许的算术运算符白名单：只放行四则运算、整除、取模与乘方
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# 单位换算基准表：每个分类里，value 先换算到基准单位，再换算到目标单位
# 长度基准=米，重量基准=千克
_UNIT_TABLE = {
    # 长度
    "mm": ("length", 0.001),
    "cm": ("length", 0.01),
    "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "inch": ("length", 0.0254),
    "ft": ("length", 0.3048),
    "mile": ("length", 1609.344),
    # 重量
    "g": ("weight", 0.001),
    "kg": ("weight", 1.0),
    "t": ("weight", 1000.0),
    "lb": ("weight", 0.45359237),
    "oz": ("weight", 0.028349523125),
}


# 递归求值安全算术表达式 AST 节点；参数 node=已解析的 AST 节点，返回数值结果
def _eval_node(node):
    """只求值白名单内的算术表达式，遇到函数调用/属性访问等一律拒绝"""

    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        # 限制乘方指数，避免 9**9**9 这类天文数字拖垮进程
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("指数过大")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("仅支持数字与 + - * / // % ** 运算")


# 把数值结果格式化成简洁字符串：整数不带小数点，浮点数去掉多余尾零；参数 value=数值
def _format_number(value) -> str:
    """数值转字符串，整数不显示小数位"""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@mcp.tool()
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """获取当前日期和时间。

    Args:
        timezone: IANA 时区名，默认 Asia/Shanghai（北京时间），如 America/New_York。
    """

    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:
        return f"未知时区：{timezone}，请使用如 Asia/Shanghai 的 IANA 时区名。"
    return (
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} {_WEEKDAYS[now.weekday()]}（{timezone}）"
    )


@mcp.tool()
def calculate(expression: str) -> str:
    """计算一个算术表达式，支持 + - * / // % ** 和括号。

    Args:
        expression: 算术表达式文本，如 "(12+8)*3/5"。
    """

    try:
        # mode="eval" 只允许表达式，从语法层面挡掉赋值、语句等
        tree = ast.parse(expression, mode="eval")
        return _format_number(_eval_node(tree))
    except Exception as e:
        return f"无法计算：{e}"


@mcp.tool()
def convert_unit(value: float, from_unit: str, to_unit: str) -> str:
    """单位换算，支持长度（mm/cm/m/km/inch/ft/mile）与重量（g/kg/t/lb/oz）。

    Args:
        value: 待换算的数值。
        from_unit: 源单位，如 km。
        to_unit: 目标单位，如 mile。
    """

    src = _UNIT_TABLE.get(from_unit.lower())
    dst = _UNIT_TABLE.get(to_unit.lower())
    if src is None or dst is None:
        return f"不支持的单位：{from_unit} 或 {to_unit}"
    if src[0] != dst[0]:
        return f"单位类型不一致：{from_unit}({src[0]}) 与 {to_unit}({dst[0]})"
    result = value * src[1] / dst[1]
    return f"{_format_number(value)} {from_unit} = {_format_number(round(result, 6))} {to_unit}"


if __name__ == "__main__":
    # 以 stdio 传输运行本 MCP Server，供智数后端作为子进程拉起
    mcp.run()
