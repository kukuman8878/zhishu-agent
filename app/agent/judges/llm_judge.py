"""
LLM-as-judge 判定器

用廉价 chat_llm 对照问题、路由、答案与证据，评忠实性/相关性/完整性并给出综合分。
输出 JSON；解析失败或调用异常时抛出，由 EvaluationService 捕获并跳过该判定器（fail-open）。
"""

import json

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate

from app.agent.judges.base import EvalSample, JudgeResult
from app.agent.llm import chat_llm
from app.agent.llm_utils import retry_async
from app.prompt.prompt_loader import load_prompt

# 参与综合分计算的数值维度
_DIMENSIONS = ("faithfulness", "relevance", "completeness")


class LLMJudge:
    """基于 LLM 的评估判定器"""

    name = "llm"

    # 构造函数：设置聚合权重；参数 weight=该判定器在综合分中的权重
    def __init__(self, weight: float = 0.7):
        self.weight = weight

    # 调用 LLM 评估样本；参数 sample=待评估样本，返回 JudgeResult（异常向上抛出由 Service 兜底）
    async def evaluate(self, sample: EvalSample) -> JudgeResult:
        prompt = PromptTemplate(
            template=load_prompt("evaluate_result"),
            input_variables=["query", "route", "answer", "evidence"],
        )
        # 证据转 JSON 文本并截断，避免超长上下文拖慢/失控
        evidence_text = json.dumps(
            sample.evidence or {}, ensure_ascii=False, default=str
        )[:2000]
        chain = prompt | chat_llm | JsonOutputParser()
        raw = await retry_async(
            chain.ainvoke,
            {
                "query": sample.query,
                "route": sample.route,
                "answer": sample.answer[:4000],
                "evidence": evidence_text,
            },
            node_name="llm_judge",
        )

        dimensions = {
            key: round(float(raw[key]), 4)
            for key in _DIMENSIONS
            if isinstance(raw.get(key), (int, float))
        }
        score = raw.get("score")
        if not isinstance(score, (int, float)):
            # 模型未给综合分时用各维度均值兜底
            score = sum(dimensions.values()) / len(dimensions) if dimensions else 0.0
        score = max(0.0, min(1.0, float(score)))
        return JudgeResult(
            judge=self.name,
            score=score,
            passed=score >= 0.6,
            dimensions=dimensions,
            reason=str(raw.get("reason", "")).strip(),
        )
