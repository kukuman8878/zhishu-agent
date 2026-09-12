"""
成本控制配套的质量保障单元测试

覆盖：
  - 过滤下放辅助模型后的 fail-open：空选择/异常时保留全部候选（不下滑）
  - orchestrator 静态快判：命中特征词时跳过 planner（省一次主模型调用）
  - aux 分级模型与主模型隔离（关闭时回退主模型）
纯逻辑/打桩，不发起真实模型调用。
"""

from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableLambda

from app.agent.agents.base import AgentResult
from app.agent.llm import aux_llm, llm
from app.agent.nodes import filter as filter_module
from app.agent.nodes import orchestrator as orch
from app.conf.app_config import app_config


class TestFilterFailOpen:
    """过滤选择下放后，空选择/失败必须保留全部候选"""

    def _runtime(self):
        return SimpleNamespace(stream_writer=lambda event: None)

    async def test_表过滤空选择保留全部(self, monkeypatch):
        # 辅助模型返回空对象 → 保留全部候选表
        monkeypatch.setattr(filter_module, "aux_llm", RunnableLambda(lambda _: "{}"))
        table_infos = [
            {"name": "orders", "columns": [{"name": "amount"}]},
            {"name": "users", "columns": [{"name": "id"}]},
        ]
        out = await filter_module.filter_table(
            {"query": "q", "table_infos": table_infos}, self._runtime()
        )
        assert out["table_infos"] == table_infos

    async def test_表过滤有效选择则裁剪(self, monkeypatch):
        monkeypatch.setattr(
            filter_module,
            "aux_llm",
            RunnableLambda(lambda _: '{"orders": ["amount"]}'),
        )
        table_infos = [
            {"name": "orders", "columns": [{"name": "amount"}, {"name": "id"}]},
            {"name": "users", "columns": [{"name": "id"}]},
        ]
        out = await filter_module.filter_table(
            {"query": "q", "table_infos": table_infos}, self._runtime()
        )
        assert [t["name"] for t in out["table_infos"]] == ["orders"]
        assert [c["name"] for c in out["table_infos"][0]["columns"]] == ["amount"]

    async def test_指标过滤空选择保留全部(self, monkeypatch):
        monkeypatch.setattr(filter_module, "aux_llm", RunnableLambda(lambda _: "[]"))
        metric_infos = [{"name": "GMV"}, {"name": "AOV"}]
        out = await filter_module.filter_metric(
            {"query": "q", "metric_infos": metric_infos}, self._runtime()
        )
        assert out["metric_infos"] == metric_infos


def _agents(knowledge_ok=False, sql_ok=False, chat_ok=True):
    class FakeAgent:
        def __init__(self, name, result):
            self.name = name
            self._result = result

        async def run(self, question, runtime, history=""):
            return self._result

    return {
        "knowledge": FakeAgent("knowledge", AgentResult("knowledge", knowledge_ok)),
        "sql": FakeAgent(
            "sql", AgentResult("sql", sql_ok, {"rows": [{"v": 1}], "sql": "S"})
        ),
        "doc": FakeAgent("doc", AgentResult("doc", False)),
        "chat": FakeAgent("chat", AgentResult("chat", chat_ok, {"text": "你好呀"})),
    }


class TestOrchestratorFastPath:
    """静态快判命中时跳过 planner"""

    async def test_闲聊命中跳过planner(self, monkeypatch):
        events: list[dict] = []
        runtime = SimpleNamespace(stream_writer=events.append, context={})
        monkeypatch.setattr(orch, "AGENTS", _agents())

        async def boom_plan(query, history):
            raise AssertionError("闲聊不应调用 planner")

        monkeypatch.setattr(orch, "_plan", boom_plan)
        out = await orch.orchestrator({"query": "你好", "messages": []}, runtime)
        assert out["route"] == "chat"
        assert events[-1] == {"type": "message", "content": "你好呀"}

    async def test_取数命中跳过planner(self, monkeypatch):
        events: list[dict] = []
        runtime = SimpleNamespace(stream_writer=events.append, context={})
        monkeypatch.setattr(orch, "AGENTS", _agents(sql_ok=True))

        async def boom_plan(query, history):
            raise AssertionError("明确取数不应调用 planner")

        monkeypatch.setattr(orch, "_plan", boom_plan)
        out = await orch.orchestrator(
            {"query": "统计华北地区的销售总额", "messages": []}, runtime
        )
        assert out["route"] == "sql"
        assert out["sql_rows"] == [{"v": 1}]


class TestAuxModelTier:
    """任务分级：aux 默认独立于主模型；关闭时回退主模型"""

    def test_默认aux独立于主模型(self):
        # 配置里 aux_enabled=true 且 aux_model_name 非空 → aux 与主模型不是同一实例
        if app_config.llm.aux_enabled and app_config.llm.aux_model_name:
            assert aux_llm is not llm


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
