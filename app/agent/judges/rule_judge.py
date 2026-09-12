"""
规则判定器（确定性检查）

不消耗模型：按路由做一组确定性检查（非空、是否有结果/引用/证据），
得分 = 通过项 / 总项。作为 LLM-as-judge 的稳定兜底，二者加权聚合。
"""

from app.agent.judges.base import EvalSample, JudgeResult


class RuleJudge:
    """基于规则的确定性判定器"""

    name = "rule"

    # 构造函数：设置聚合权重；参数 weight=该判定器在综合分中的权重
    def __init__(self, weight: float = 0.3):
        self.weight = weight

    # 执行确定性检查；参数 sample=待评估样本，返回 JudgeResult
    async def evaluate(self, sample: EvalSample) -> JudgeResult:
        evidence = sample.evidence or {}
        dimensions: dict[str, bool] = {
            # 任何路由都要求答案非空
            "non_empty": bool((sample.answer or "").strip()),
        }
        # 按路由追加证据类检查
        if sample.route == "sql":
            dimensions["has_result"] = int(evidence.get("row_count") or 0) > 0
        elif sample.route == "doc":
            dimensions["has_citation"] = bool(evidence.get("cited_pages"))
        elif sample.route == "hybrid":
            dimensions["has_data"] = bool(evidence.get("sql_rows"))
            dimensions["has_doc"] = bool(evidence.get("doc_answer"))

        passed_count = sum(1 for ok in dimensions.values() if ok)
        score = passed_count / len(dimensions) if dimensions else 0.0
        return JudgeResult(
            judge=self.name,
            score=score,
            passed=score >= 0.5,
            dimensions=dimensions,
            reason="",
        )
