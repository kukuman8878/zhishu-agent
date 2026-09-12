"""
问答结果评估单元测试

覆盖：
  - RuleJudge 各路由的确定性检查
  - EvaluationService 的样本抽取（sql/doc/hybrid/knowledge/chat）
  - 多判定器按权重聚合、判定器异常跳过、低分 passed=False
  - QueryEval 实体/mapper 往返（dimensions JSON）
纯逻辑 + 假判定器/假仓储，不发起真实模型或数据库调用。
"""

import pytest
from langchain_core.messages import AIMessage

from app.agent.judges.base import EvalSample, JudgeResult
from app.agent.judges.rule_judge import RuleJudge
from app.conf.app_config import app_config
from app.entities.query_eval import QueryEval
from app.repositories.mysql.meta.mappers.query_eval_mapper import QueryEvalMapper
from app.services.evaluation_service import EvaluationService


class FakeJudge:
    """假判定器：返回固定分或抛异常"""

    def __init__(self, name, score, weight=1.0, explode=False, dimensions=None):
        self.name = name
        self.score = score
        self.weight = weight
        self.explode = explode
        self.dimensions = dimensions or {"d": score}

    async def evaluate(self, sample):
        if self.explode:
            raise RuntimeError("判定器炸了")
        return JudgeResult(
            judge=self.name,
            score=self.score,
            passed=self.score >= 0.6,
            dimensions=self.dimensions,
            reason="ok",
        )


class FakeSession:
    async def commit(self):
        return None


class FakeRepo:
    """假评估仓储：记录落库与建表调用"""

    def __init__(self):
        self.saved: list[QueryEval] = []
        self.ensured = False
        self.session = FakeSession()

    async def ensure_table(self):
        self.ensured = True

    def save(self, record):
        self.saved.append(record)


class TestRuleJudge:
    """规则判定器"""

    async def test_sql有结果满分(self):
        result = await RuleJudge().evaluate(
            EvalSample("q", "sql", "表格", {"row_count": 3})
        )
        assert result.score == 1.0
        assert result.dimensions["has_result"] is True

    async def test_sql空结果半分(self):
        result = await RuleJudge().evaluate(
            EvalSample("q", "sql", "（数仓未返回数据）", {"row_count": 0})
        )
        assert result.score == 0.5

    async def test_doc无引用半分(self):
        result = await RuleJudge().evaluate(
            EvalSample("q", "doc", "答案", {"cited_pages": []})
        )
        assert result.dimensions["has_citation"] is False

    async def test_hybrid双证据满分(self):
        result = await RuleJudge().evaluate(
            EvalSample(
                "q", "hybrid", "三段式", {"sql_rows": [{"a": 1}], "doc_answer": "文档"}
            )
        )
        assert result.score == 1.0

    async def test_闲聊仅校验非空(self):
        result = await RuleJudge().evaluate(EvalSample("q", "chat", "你好", {}))
        assert result.score == 1.0
        assert set(result.dimensions) == {"non_empty"}


class TestExtractSample:
    """样本抽取"""

    def _service(self):
        return EvaluationService(None, judges=[])

    def test_sql抽取答案与证据(self):
        sample = self._service().extract_sample(
            {"route": "sql", "query": "q", "sql_rows": [{"a": 1}], "sql": "SELECT 1"}
        )
        assert sample.route == "sql"
        assert "| a |" in sample.answer
        assert sample.evidence["row_count"] == 1

    def test_doc抽取引用(self):
        sample = self._service().extract_sample(
            {
                "route": "doc",
                "query": "q",
                "doc_answer": "答案",
                "doc_cited_pages": ["2"],
            }
        )
        assert sample.evidence["cited_pages"] == ["2"]

    def test_hybrid抽取两段证据(self):
        sample = self._service().extract_sample(
            {
                "route": "hybrid",
                "query": "q",
                "hybrid_content": "综合",
                "sql_rows": [{"a": 1}],
                "doc_answer": "文档",
            }
        )
        assert sample.answer == "综合"
        assert sample.evidence["sql_rows"] and sample.evidence["doc_answer"]

    def test_knowledge抽取(self):
        sample = self._service().extract_sample(
            {
                "route": "knowledge",
                "query": "q",
                "knowledge_answer": "存量",
                "knowledge_item_id": "k1",
            }
        )
        assert sample.evidence["item_id"] == "k1"

    def test_chat取最后一条AI消息(self):
        sample = self._service().extract_sample(
            {
                "route": "chat",
                "query": "q",
                "messages": [AIMessage(content="旧"), AIMessage(content="最新")],
            }
        )
        assert sample.answer == "最新"

    def test_无路由返回None(self):
        assert self._service().extract_sample({}) is None

    def test_无答案返回None(self):
        # chat 无任何 AI 消息 → 无可评答案
        assert self._service().extract_sample({"route": "chat", "messages": []}) is None

    def test_sql空结果仍可评估(self):
        # 空结果会转成占位文本，仍参与评估（规则分较低）
        sample = self._service().extract_sample({"route": "sql", "sql_rows": []})
        assert sample is not None
        assert sample.evidence["row_count"] == 0


class TestEvaluateAggregation:
    """判定器聚合与落库"""

    async def test_按权重聚合落库(self, monkeypatch):
        monkeypatch.setattr(app_config.eval, "enabled", True)
        repo = FakeRepo()
        service = EvaluationService(
            repo,
            judges=[FakeJudge("a", 0.8, weight=0.7), FakeJudge("b", 0.4, weight=0.3)],
        )
        record = await service.evaluate(
            {"route": "sql", "query": "q", "sql_rows": [{"a": 1}], "sql": "SELECT 1"}
        )
        # 0.8*0.7 + 0.4*0.3 = 0.68
        assert record.score == pytest.approx(0.68)
        assert record.passed is True
        assert repo.ensured and len(repo.saved) == 1
        assert record.judge == "a+b"

    async def test_低分不通过(self, monkeypatch):
        monkeypatch.setattr(app_config.eval, "enabled", True)
        service = EvaluationService(
            FakeRepo(), judges=[FakeJudge("a", 0.2, weight=1.0)]
        )
        record = await service.evaluate(
            {"route": "sql", "query": "q", "sql_rows": [{"a": 1}]}
        )
        assert record.passed is False

    async def test_判定器异常跳过不阻断(self, monkeypatch):
        monkeypatch.setattr(app_config.eval, "enabled", True)
        service = EvaluationService(
            FakeRepo(),
            judges=[
                FakeJudge("bad", 0.0, weight=0.5, explode=True),
                FakeJudge("ok", 0.9, weight=0.5),
            ],
        )
        record = await service.evaluate(
            {"route": "sql", "query": "q", "sql_rows": [{"a": 1}]}
        )
        assert record.score == pytest.approx(0.9)
        assert record.judge == "ok"

    async def test_未启用时返回None(self, monkeypatch):
        monkeypatch.setattr(app_config.eval, "enabled", False)
        service = EvaluationService(FakeRepo(), judges=[FakeJudge("a", 0.9)])
        assert (
            await service.evaluate(
                {"route": "sql", "query": "q", "sql_rows": [{"a": 1}]}
            )
            is None
        )


class TestQueryEvalMapper:
    """实体与 ORM 往返（dimensions JSON）"""

    def test_往返保留维度与布尔(self):
        entity = QueryEval(
            id="e1",
            query="q",
            route="sql",
            judge="llm+rule",
            score=0.75,
            passed=True,
            dimensions={"llm.faithfulness": 0.8, "rule.non_empty": True},
            reason="理由",
            answer="答案",
            session_id="s1",
            duration_ms=12,
        )
        model = QueryEvalMapper.to_model(entity)
        assert isinstance(model.dimensions, str)  # JSON 文本落库
        back = QueryEvalMapper.to_entity(model)
        assert back.dimensions == entity.dimensions
        assert back.passed is True
        assert back.score == 0.75


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
