"""
子 Agent 注册表

orchestrator（主 Agent）通过 AGENTS 名称表取用各能力子 Agent，实现"主 Agent 编排分发"。
"""

from app.agent.agents.base import AgentResult
from app.agent.agents.chat_agent import ChatAgent
from app.agent.agents.doc_agent import DocAgent
from app.agent.agents.knowledge_agent import KnowledgeAgent
from app.agent.agents.sql_agent import SQLAgent

# 名称 → 子 Agent 单例；orchestrator 规划输出 agent 名称后据此调用
AGENTS = {
    SQLAgent.name: SQLAgent(),
    DocAgent.name: DocAgent(),
    KnowledgeAgent.name: KnowledgeAgent(),
    ChatAgent.name: ChatAgent(),
}

# 允许出现在编排计划里的子 Agent 名称（knowledge 由 orchestrator 的知识门固定先行调用）
PLANNABLE_AGENTS = {SQLAgent.name, DocAgent.name, ChatAgent.name}

__all__ = ["AGENTS", "PLANNABLE_AGENTS", "AgentResult"]
