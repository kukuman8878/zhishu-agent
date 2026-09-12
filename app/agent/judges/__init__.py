"""
评估判定器注册与构建

按配置权重组装默认判定器组合：LLMJudge（LLM-as-judge）+ RuleJudge（规则检查）。
后续要扩展新的判定器（如人工反馈、检索忠实性校验），只需在此追加并实现 Judge 协议。
"""

from app.agent.judges.base import EvalSample, Judge, JudgeResult
from app.agent.judges.llm_judge import LLMJudge
from app.agent.judges.rule_judge import RuleJudge
from app.conf.app_config import app_config


# 按配置构建默认判定器组合；参数：无，返回 Judge 列表
def build_judges() -> list[Judge]:
    """构建默认判定器：LLM-as-judge + 规则检查，权重由 eval.llm_weight 决定"""

    llm_weight = max(0.0, min(1.0, app_config.eval.llm_weight))
    return [LLMJudge(weight=llm_weight), RuleJudge(weight=1.0 - llm_weight)]


__all__ = [
    "EvalSample",
    "JudgeResult",
    "Judge",
    "build_judges",
    "LLMJudge",
    "RuleJudge",
]
