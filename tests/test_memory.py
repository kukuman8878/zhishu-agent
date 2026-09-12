"""
四类记忆单元测试

覆盖：
  - MemoryContext.render 渲染（摘要 + 用户偏好）
  - render_history 注入长期记忆片段
  - MemoryManager 摘要记忆触发阈值 + 用户记忆抽取去重
纯逻辑 + 假仓储/RunnableLambda 假模型，不发起真实模型或数据库调用。
"""

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from app.agent.history import render_history
from app.agent.memory import MemoryContext
from app.agent.memory import manager as memory_manager_module
from app.agent.memory.manager import MemoryManager
from app.conf.app_config import app_config


class FakeSession:
    async def commit(self):
        return None


class FakeRepo:
    """假长期记忆仓储"""

    def __init__(self, summary=None, facts=None):
        self.summary = summary
        self.facts = list(facts or [])
        self.upserts: list[str] = []
        self.added: list[str] = []
        self.ensured = False
        self.session = FakeSession()

    async def ensure_table(self):
        self.ensured = True

    async def get_summary(self, session_id):
        return self.summary

    async def upsert_summary(self, session_id, content):
        self.summary = content
        self.upserts.append(content)

    async def list_user_facts(self, user_id, limit=8):
        return self.facts[:limit]

    async def add_user_facts(self, user_id, facts):
        for fact in facts:
            if fact not in self.facts:
                self.facts.append(fact)
                self.added.append(fact)


def _messages(n: int) -> list:
    """构造 n 条消息"""
    out = []
    for i in range(n):
        out.append(HumanMessage(content=f"问题{i}"))
        out.append(AIMessage(content=f"回答{i}"))
    return out[:n]


class TestMemoryContextRender:
    def test_渲染摘要与偏好(self):
        text = MemoryContext("会话摘要", ["关注华北"]).render()
        assert "【会话摘要】" in text
        assert "【用户偏好】" in text
        assert "- 关注华北" in text

    def test_空记忆渲染为空串(self):
        assert MemoryContext().render() == ""


class TestRenderHistoryMemory:
    def test_注入记忆片段(self):
        text = render_history([HumanMessage(content="你好")], memory="【会话摘要】\nX")
        assert text.startswith("【会话摘要】")
        assert "用户：你好" in text

    def test_无记忆时不变(self):
        text = render_history([HumanMessage(content="你好")])
        assert "【会话摘要】" not in text
        assert "用户：你好" in text


class TestLoadContext:
    async def test_加载摘要与偏好(self):
        repo = FakeRepo(summary="旧摘要", facts=["偏好GMV", "关注华东"])
        manager = MemoryManager(repo)
        ctx = await manager.load_context("s1", "u1")
        assert ctx.summary == "旧摘要"
        assert ctx.user_facts == ["偏好GMV", "关注华东"]

    async def test_总开关关闭返回空(self, monkeypatch):
        monkeypatch.setattr(app_config.memory, "enabled", False)
        ctx = await MemoryManager(FakeRepo(summary="x")).load_context("s1", "u1")
        assert ctx.summary == "" and ctx.user_facts == []


class TestUpdateMemory:
    async def test_未达阈值不生成摘要(self, monkeypatch):
        monkeypatch.setattr(app_config.memory, "summary_trigger_messages", 12)
        monkeypatch.setattr(app_config.memory, "user_memory_enabled", False)
        repo = FakeRepo()
        await MemoryManager(repo).update_after_turn("s1", "u1", _messages(4), "q")
        assert repo.upserts == []

    async def test_达到阈值生成摘要(self, monkeypatch):
        monkeypatch.setattr(app_config.memory, "summary_trigger_messages", 4)
        monkeypatch.setattr(app_config.memory, "user_memory_enabled", False)
        monkeypatch.setattr(
            memory_manager_module,
            "chat_llm",
            RunnableLambda(lambda _: "这是会话摘要"),
        )
        repo = FakeRepo()
        await MemoryManager(repo).update_after_turn("s1", "u1", _messages(6), "q")
        assert repo.upserts == ["这是会话摘要"]

    async def test_抽取用户偏好并去重(self, monkeypatch):
        monkeypatch.setattr(app_config.memory, "summary_trigger_messages", 100)
        monkeypatch.setattr(app_config.memory, "user_memory_enabled", True)
        monkeypatch.setattr(
            memory_manager_module,
            "chat_llm",
            RunnableLambda(lambda _: '["关注华北", "偏好GMV"]'),
        )
        repo = FakeRepo(facts=["关注华北"])
        # 问题含偏好线索（我/以后）才会触发抽取
        await MemoryManager(repo).update_after_turn(
            "s1", "u1", _messages(2), "我以后都关注华北"
        )
        # 已存在的"关注华北"不重复写入，仅新增"偏好GMV"
        assert repo.added == ["偏好GMV"]

    async def test_无偏好线索不抽取省调用(self, monkeypatch):
        monkeypatch.setattr(app_config.memory, "summary_trigger_messages", 100)
        monkeypatch.setattr(app_config.memory, "user_memory_enabled", True)
        monkeypatch.setattr(app_config.memory, "user_memory_cue_only", True)
        monkeypatch.setattr(
            memory_manager_module,
            "chat_llm",
            RunnableLambda(lambda _: '["不该被抽取"]'),
        )
        repo = FakeRepo()
        # 普通取数问题无偏好线索 → 跳过抽取（省一次模型调用）
        await MemoryManager(repo).update_after_turn(
            "s1", "u1", _messages(2), "统计销售额"
        )
        assert repo.added == []


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q"])
