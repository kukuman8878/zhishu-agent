"""
评估判定器统一契约

评估采用"可插拔 judge"：每个 judge 实现 `evaluate(sample) -> JudgeResult`，
由 EvaluationService 统一调度并按权重聚合。默认提供两类：
  - LLMJudge：LLM-as-judge，评忠实性/相关性/完整性；
  - RuleJudge：确定性规则检查（空结果/无引用等）。

判定器必须 fail 可控：内部异常由 Service 捕获并跳过，不影响其他判定器与主流程。
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class EvalSample:
    """待评估样本（一次问答的终端答案 + 证据）"""

    query: str
    route: str
    answer: str
    # 证据：sql→{sql,row_count}；doc→{cited_pages}；hybrid→{sql_rows,doc_answer}；knowledge→{item_id}
    evidence: dict = field(default_factory=dict)


@dataclass
class JudgeResult:
    """单个判定器的评估结果"""

    judge: str
    # 本次判定分数 0~1
    score: float
    # 该判定器是否认为通过
    passed: bool
    # 维度明细：{维度: 分数/布尔}
    dimensions: dict = field(default_factory=dict)
    # 判定理由（规则判定可为空）
    reason: str = ""


@runtime_checkable
class Judge(Protocol):
    """判定器协议：名称 + 聚合权重 + 评估方法"""

    name: str
    weight: float

    async def evaluate(self, sample: EvalSample) -> JudgeResult: ...
