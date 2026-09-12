"""
RAG 强约束（重排分门 + 可溯源）单元测试

覆盖：
  - prompts 的强约束规则是否附加到作答 system prompt
  - _rerank_gate（最高重排分低于阈值 → 拒绝）
  - _valid_cited（引用页必须落在证据页内）
  - 文档引擎客户端的可溯源强制门（无引用页 → NOT_FOUND）
纯逻辑/打桩 Agent，不发起真实检索或模型调用。
"""

import pytest

from app.clients.doc_engine_client_manager import DocEngineClient
from app.conf.app_config import app_config
from app.rag_engine.agent.agent import AgentTrace, _rerank_gate, _valid_cited
from app.rag_engine.agent.prompts import ANSWER_SYSTEM, COMPARISON_ANSWER_SYSTEM
from app.rag_engine.config import settings
from app.rag_engine.retrieval.rerank import PageEvidence


def _ev(page_idx=0, rerank=None, doc_id="d1"):
    ev = PageEvidence(
        pkey=f"{doc_id}|{page_idx}",
        doc_id=doc_id,
        page_idx=page_idx,
        text="page text",
        page_image="",
    )
    if rerank is not None:
        ev.sources = {"rerank": rerank}
    return ev


class FakeAgent:
    """打桩 Agent：返回预置 trace"""

    def __init__(self, answer, trace):
        self._answer = answer
        self._trace = trace

    async def answer(self, question):
        return self._answer, self._trace


class TestPromptConstraints:
    def test_作答prompt含可溯源强约束(self):
        assert "TRACEABILITY & STRICT GROUNDING" in ANSWER_SYSTEM
        assert "cited_pages" in ANSWER_SYSTEM
        assert "NOT_FOUND" in ANSWER_SYSTEM

    def test_对比prompt含可溯源强约束(self):
        assert "TRACEABILITY & STRICT GROUNDING" in COMPARISON_ANSWER_SYSTEM


class TestRerankGate:
    def test_低于阈值拒绝(self):
        refused, reason = _rerank_gate([_ev(0, rerank=0.05)])
        assert refused and "rerank" in reason

    def test_高于阈值放行(self):
        refused, _ = _rerank_gate([_ev(0, rerank=0.9), _ev(1, rerank=0.2)])
        assert refused is False

    def test_无重排分不判定(self):
        # 视觉/元素路径无 rerank 分，不应误拒
        refused, _ = _rerank_gate([_ev(0, rerank=None)])
        assert refused is False


class TestValidCited:
    def test_仅保留证据内页码(self):
        assert _valid_cited([1, 5, 2], [_ev(0), _ev(1)]) == [1, 2]

    def test_全部非法则空(self):
        assert _valid_cited([9], [_ev(0)]) == []


class TestCitationEnforcement:
    async def test_无引用页则拒答(self, monkeypatch):
        monkeypatch.setattr(settings, "require_citation", True)
        client = DocEngineClient(app_config.doc_engine)
        client._agent = FakeAgent(
            "这是一个答案",
            AgentTrace(answer="这是一个答案", evidence=[], cited_pages=[]),
        )
        result = await client.answer("q")
        assert result["answer"] == "NOT_FOUND"
        assert result["refused"] is True
        assert result["cited_pages"] == []

    async def test_有引用页则透传(self, monkeypatch):
        monkeypatch.setattr(settings, "require_citation", True)
        client = DocEngineClient(app_config.doc_engine)
        client._agent = FakeAgent(
            "这是一个答案",
            AgentTrace(answer="这是一个答案", evidence=[], cited_pages=[2]),
        )
        result = await client.answer("q")
        assert result["answer"] == "这是一个答案"
        assert result["cited_pages"] == [2]
        assert result["refused"] is False

    async def test_关闭可溯源门则不拦(self, monkeypatch):
        monkeypatch.setattr(settings, "require_citation", False)
        client = DocEngineClient(app_config.doc_engine)
        client._agent = FakeAgent(
            "这是一个答案",
            AgentTrace(answer="这是一个答案", evidence=[], cited_pages=[]),
        )
        result = await client.answer("q")
        assert result["answer"] == "这是一个答案"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
