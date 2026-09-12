"""
子 Agent 统一契约

多智能体编排（Supervisor）下，每个能力被封装为一个子 Agent，对外暴露统一的
run(question, runtime, history) 接口，返回统一的 AgentResult，这样 orchestrator
不需要关心各能力内部如何实现，只按计划调用并综合结果。

设计要点：
  - 子 Agent 只负责"取数/取答案"，不发送终端 SSE 事件（终端事件由 orchestrator
    根据结果组合决定），避免多 Agent 协同时事件互相覆盖；
  - 失败一律返回 ok=False + error，不抛异常，保证单个子 Agent 失败不拖垮整体编排。
"""

from dataclasses import dataclass, field


@dataclass
class AgentResult:
    """子 Agent 执行结果（统一返回契约）"""

    # 子 Agent 名称：sql / doc / knowledge / chat
    agent: str
    # 是否成功产出可用结果
    ok: bool
    # 结构化载荷：各 Agent 自定义（sql→rows/sql；doc→answer/cited_pages；chat→text；knowledge→answer/item_id）
    payload: dict = field(default_factory=dict)
    # 失败原因摘要（ok=False 时填充）
    error: str = ""
