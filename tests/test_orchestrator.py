"""
多智能体编排（orchestrator）单元测试

覆盖：
  - 计划解析 _parse_plan（嵌套 JSON、非法 agent 过滤、数量上限、畸形输入）
  - 终端事件映射：知识命中短路、纯 chat、仅 sql、sql+doc、全部未命中
纯逻辑 + 打桩子 Agent，不发起真实模型/数据库/文档调用。
"""

from types import SimpleNamespace

import pytest

from app.agent.agents.base import AgentResult
from app.agent.nodes import orchestrator as orch


class FakeAgent:
    """可编排的假子 Agent：按预设结果返回"""

    def __init__(self, name: str, result: AgentResult):
        self.name = name
        self._result = result

    async def run(self, question, runtime, history=""):
        return self._result


class FakeRepo:
    """假知识仓储：记录命中计数"""

    def __init__(self):
        self.hits = []

    async def increment_hit_count(self, item_id):
        self.hits.append(item_id)


def _agents(knowledge_ok=False, sql_ok=False, doc_ok=False, chat_ok=False):
    """构造一组假子 Agent 注册表"""
    return {
        "knowledge": FakeAgent(
            "knowledge",
            AgentResult(
                "knowledge",
                knowledge_ok,
                {"answer": "存量答案", "item_id": "k1", "score": 0.9},
            ),
        ),
        "sql": FakeAgent(
            "sql", AgentResult("sql", sql_ok, {"rows": [{"v": 1}], "sql": "SELECT 1"})
        ),
        "doc": FakeAgent(
            "doc",
            AgentResult(
                "doc",
                doc_ok,
                {
                    "answer": "文档答案",
                    "cited_pages": ["3"],
                    "rounds": 1,
                    "route": {},
                    "data": {"answer": "文档答案", "cited_pages": ["3"]},
                },
            ),
        ),
        "chat": FakeAgent("chat", AgentResult("chat", chat_ok, {"text": "你好呀"})),
    }


class TestParsePlan:
    """计划解析"""

    def test_解析嵌套JSON计划(self):
        raw = '{"tasks":[{"task_id":"t1","agent":"doc","question":"文档里参数是啥","depends_on":[]}]}'
        tasks = orch._parse_plan(raw, 3)
        assert tasks == [
            {
                "task_id": "t1",
                "agent": "doc",
                "question": "文档里参数是啥",
                "depends_on": [],
            }
        ]

    def test_过滤非法agent与空问句(self):
        raw = '{"tasks":[{"agent":"knowledge","question":"x y z"},{"agent":"sql","question":""}]}'
        assert orch._parse_plan(raw, 3) == []

    def test_严格遵守数量上限(self):
        raw = (
            '{"tasks":['
            '{"agent":"sql","question":"a b c"},'
            '{"agent":"doc","question":"d e f"},'
            '{"agent":"chat","question":"g h i"}]}'
        )
        assert len(orch._parse_plan(raw, 2)) == 2

    def test_畸形输入返回空计划(self):
        assert orch._parse_plan("抱歉我不懂", 3) == []
        assert orch._parse_plan("{not json}", 3) == []


class TestOrchestratorTerminal:
    """编排终端事件映射"""

    async def test_知识命中短路(self, monkeypatch):
        events: list[dict] = []
        repo = FakeRepo()
        runtime = SimpleNamespace(
            stream_writer=events.append,
            context={"knowledge_mysql_repository": repo},
        )
        monkeypatch.setattr(orch, "AGENTS", _agents(knowledge_ok=True))

        out = await orch.orchestrator(
            {"query": "之前问过的问题", "messages": []}, runtime
        )
        assert out["route"] == "knowledge"
        assert out["knowledge_item_id"] == "k1"
        assert repo.hits == ["k1"]
        assert events[-1] == {"type": "message", "content": "存量答案"}

    async def test_纯闲聊计划走message(self, monkeypatch):
        events: list[dict] = []
        runtime = SimpleNamespace(stream_writer=events.append, context={})
        monkeypatch.setattr(orch, "AGENTS", _agents(chat_ok=True))

        async def fake_plan(query, history):
            return [
                {"task_id": "t1", "agent": "chat", "question": "你好", "depends_on": []}
            ]

        monkeypatch.setattr(orch, "_plan", fake_plan)
        out = await orch.orchestrator({"query": "你好", "messages": []}, runtime)
        assert out["route"] == "chat"
        assert events[-1] == {"type": "message", "content": "你好呀"}

    async def test_仅sql成功发result(self, monkeypatch):
        events: list[dict] = []
        runtime = SimpleNamespace(stream_writer=events.append, context={})
        monkeypatch.setattr(orch, "AGENTS", _agents(sql_ok=True, doc_ok=False))

        async def fake_plan(query, history):
            return [
                {
                    "task_id": "t1",
                    "agent": "sql",
                    "question": "销售额",
                    "depends_on": [],
                },
                {"task_id": "t2", "agent": "doc", "question": "文档", "depends_on": []},
            ]

        monkeypatch.setattr(orch, "_plan", fake_plan)
        out = await orch.orchestrator({"query": "销售额", "messages": []}, runtime)
        assert out["route"] == "sql"
        assert out["sql_rows"] == [{"v": 1}]
        assert events[-1]["type"] == "result"

    async def test_sql与doc同时成功发hybrid(self, monkeypatch):
        events: list[dict] = []
        runtime = SimpleNamespace(stream_writer=events.append, context={})
        monkeypatch.setattr(orch, "AGENTS", _agents(sql_ok=True, doc_ok=True))

        async def fake_plan(query, history):
            return [
                {
                    "task_id": "t1",
                    "agent": "sql",
                    "question": "销售额",
                    "depends_on": [],
                },
                {"task_id": "t2", "agent": "doc", "question": "文档", "depends_on": []},
            ]

        async def fake_compose(query, data_sections, doc_sections, writer):
            return "三段式综合"

        monkeypatch.setattr(orch, "_plan", fake_plan)
        monkeypatch.setattr(orch, "_compose", fake_compose)
        out = await orch.orchestrator({"query": "对比", "messages": []}, runtime)
        assert out["route"] == "hybrid"
        assert out["hybrid_content"] == "三段式综合"
        assert events[-1]["type"] == "hybrid"

    async def test_全部未命中发兜底message(self, monkeypatch):
        events: list[dict] = []
        runtime = SimpleNamespace(stream_writer=events.append, context={})
        monkeypatch.setattr(orch, "AGENTS", _agents())  # 全部失败

        async def fake_plan(query, history):
            return [
                {"task_id": "t1", "agent": "sql", "question": "q q q", "depends_on": []}
            ]

        monkeypatch.setattr(orch, "_plan", fake_plan)
        out = await orch.orchestrator({"query": "q q q", "messages": []}, runtime)
        assert out["route"] == "hybrid"
        assert events[-1]["type"] == "message"
        assert "未能" in events[-1]["content"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
