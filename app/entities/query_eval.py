"""
问答结果评估业务实体

每次问答结束后，评估服务对终端答案做一次质量评估（LLM-as-judge + 规则检查），
把综合分与各维度得分落库到 query_eval 表，用于质量监控、低质答案复盘与回归评估。
"""

from dataclasses import dataclass, field


@dataclass
class QueryEval:
    """一次问答结果评估的业务表达"""

    id: str
    query: str
    route: str
    # 判定器名称（如 llm+rule 组合），便于区分评分来源
    judge: str
    # 综合分 0~1
    score: float
    # 是否通过（score >= pass_threshold）
    passed: bool
    # 各维度得分：{维度: 分数或布尔}
    dimensions: dict = field(default_factory=dict)
    # 判定理由（LLM 给出，规则判定为空）
    reason: str = ""
    # 会话标识（多轮追问共享）
    session_id: str | None = None
    # 被评估的终端答案（截断后存储，便于离线复核）
    answer: str | None = None
    # 评估耗时（毫秒）
    duration_ms: int | None = None
