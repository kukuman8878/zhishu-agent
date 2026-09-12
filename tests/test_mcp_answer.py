"""
通用工具循环与 MCP 专属工具分配单元测试

覆盖：
  - 工具返回值规整、工具调用异常/超时兜底
  - 有界工具调用循环：请求工具→回喂→收口，且轮数受限
  - MCP Server 按 agents 字段归属不同子 Agent（含默认归 chat、enabled=false 跳过）
不发起真实模型或 MCP 网络调用，快且稳定。
"""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agent import tool_loop as tl
from app.clients.mcp_client_manager import MCPClientManager
from app.conf.app_config import app_config


class TestStringifyToolResult:
    """工具返回值转文本"""

    def test_字符串原样返回(self):
        assert tl.stringify_tool_result("晴天") == "晴天"

    def test_MCP内容块列表提取文本(self):
        result = [{"type": "text", "text": "北京晴"}, {"type": "text", "text": "25度"}]
        assert tl.stringify_tool_result(result) == "北京晴\n25度"

    def test_其他类型直接转字符串(self):
        assert tl.stringify_tool_result({"temp": 25}) == "{'temp': 25}"


class TestRunToolCall:
    """单次工具调用兜底"""

    async def test_未知工具返回提示(self):
        out = await tl.run_tool_call({}, {"name": "ghost", "args": {}, "id": "1"})
        assert "不存在" in out

    async def test_工具异常转错误文本(self):
        class BoomTool:
            name = "boom"

            async def ainvoke(self, args):
                raise RuntimeError("接口挂了")

        out = await tl.run_tool_call(
            {"boom": BoomTool()}, {"name": "boom", "args": {}, "id": "1"}
        )
        assert "调用失败" in out and "接口挂了" in out

    async def test_工具超时转错误文本(self, monkeypatch):
        monkeypatch.setattr(app_config.mcp, "timeout", 0.01)

        class SlowTool:
            name = "slow"

            async def ainvoke(self, args):
                await asyncio.sleep(0.1)
                return "too late"

        out = await tl.run_tool_call(
            {"slow": SlowTool()}, {"name": "slow", "args": {}, "id": "1"}
        )
        assert "超时" in out


class FakeModel:
    """支持 bind_tools 的假模型（工具循环只依赖 bind_tools 与 astream）"""

    def bind_tools(self, tools):
        return self


class TestRunToolLoop:
    """有界工具调用循环"""

    async def test_请求工具后回喂并收口(self, monkeypatch):
        called: list[str] = []

        class WeatherTool:
            name = "get_weather"

            async def ainvoke(self, args):
                called.append(args["city"])
                return [{"type": "text", "text": "北京晴 25 度"}]

        # 第一次返回工具请求，第二次返回最终答案文本
        scripts = [
            SimpleNamespace(
                tool_calls=[
                    {"name": "get_weather", "args": {"city": "北京"}, "id": "1"}
                ]
            ),
            SimpleNamespace(tool_calls=[]),
        ]

        async def fake_stream(model, messages, pieces, writer, reset_sent, stream):
            gathered = scripts.pop(0)
            if not gathered.tool_calls:
                pieces.append("北京今天晴，25 度。")
                if stream and writer:
                    writer(
                        {
                            "type": "delta",
                            "content": "北京今天晴，25 度。",
                            "reset": not reset_sent,
                        }
                    )
                reset_sent = True
            return gathered, reset_sent

        monkeypatch.setattr(tl, "_stream_once", fake_stream)

        events: list[dict] = []
        content = await tl.run_tool_loop(
            FakeModel(),
            [HumanMessage(content="北京天气")],
            [WeatherTool()],
            events.append,
            max_rounds=3,
            stream=True,
        )

        assert called == ["北京"]
        assert content == "北京今天晴，25 度。"
        assert events[-1]["content"] == "北京今天晴，25 度。"

    async def test_达到轮数上限强制收口(self, monkeypatch):
        class LoopTool:
            name = "loop"

            async def ainvoke(self, args):
                return "again"

        stream_calls = {"n": 0}

        async def always_tool(model, messages, pieces, writer, reset_sent, stream):
            stream_calls["n"] += 1
            return (
                SimpleNamespace(
                    tool_calls=[
                        {"name": "loop", "args": {}, "id": str(stream_calls["n"])}
                    ]
                ),
                reset_sent,
            )

        monkeypatch.setattr(tl, "_stream_once", always_tool)

        # 2 轮工具 + 1 次强制收口调用
        await tl.run_tool_loop(
            FakeModel(),
            [HumanMessage(content="循环")],
            [LoopTool()],
            None,
            max_rounds=2,
            stream=False,
        )
        assert stream_calls["n"] == 3


class TestMCPAgentAssignment:
    """MCP Server 按 agents 字段归属子 Agent"""

    def _patch_servers(self, monkeypatch):
        monkeypatch.setattr(
            app_config.mcp,
            "servers",
            {
                "sql_box": {"transport": "stdio", "command": "x", "agents": ["sql"]},
                "shared": {
                    "transport": "stdio",
                    "command": "y",
                    "agents": ["chat", "doc"],
                },
                "legacy": {"transport": "stdio", "command": "z"},  # 缺省归 chat
                "off": {
                    "transport": "stdio",
                    "command": "w",
                    "enabled": False,
                    "agents": ["sql"],
                },
            },
        )

    def test_仅返回归属该agent的连接(self, monkeypatch):
        self._patch_servers(monkeypatch)
        mgr = MCPClientManager()
        assert set(mgr._enabled_connections("sql")) == {"sql_box"}
        assert set(mgr._enabled_connections("doc")) == {"shared"}

    def test_缺省归属chat(self, monkeypatch):
        self._patch_servers(monkeypatch)
        mgr = MCPClientManager()
        assert set(mgr._enabled_connections("chat")) == {"shared", "legacy"}

    def test_停用服务被跳过(self, monkeypatch):
        self._patch_servers(monkeypatch)
        mgr = MCPClientManager()
        assert "off" not in mgr._enabled_connections("sql")

    def test_不过滤时返回全部启用服务(self, monkeypatch):
        self._patch_servers(monkeypatch)
        mgr = MCPClientManager()
        assert set(mgr._enabled_connections(None)) == {"sql_box", "shared", "legacy"}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
