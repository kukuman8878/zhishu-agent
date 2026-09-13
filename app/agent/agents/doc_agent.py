"""
文档问答子 Agent

封装进程内文档问答引擎（app/rag_engine/，向量 + BM25 + RRF 多路检索），
回答问题并返回答案与引用页码，供 orchestrator 单用或与 SQL 结果综合。
"""

from langgraph.runtime import Runtime

from app.agent.agents.base import AgentResult
from app.agent.context import DataAgentContext
from app.agent.tool_loop import collect_tool_supplement
from app.clients.mcp_client_manager import mcp_client_manager
from app.core.log import logger

# 文档引擎未命中时返回的兜底标记
_NOT_FOUND = "NOT_FOUND"


class DocAgent:
    """文档问答子 Agent：调进程内文档引擎取答案与页码"""

    name = "doc"

    # 执行一次文档问答子任务，返回 AgentResult(answer/cited_pages/rounds/route/data)；参数 question=自然语言子问题，runtime=运行上下文，history 未使用
    async def run(
        self, question: str, runtime: Runtime[DataAgentContext], history: str = ""
    ) -> AgentResult:
        rag_client = runtime.context.get("rag_client")
        if rag_client is None:
            return AgentResult(agent=self.name, ok=False, error="engine_disabled")
        try:
            # 专属工具预处理：归属 doc 的 MCP 工具先静默取补充信息，拼进问句再检索
            tools = await mcp_client_manager.get_tools(self.name)
            if tools:
                supplement = await collect_tool_supplement(question, self.name, tools)
                if supplement:
                    question = f"{question}\n工具补充信息：{supplement}"
            result = await rag_client.answer(question)
            answer = str(result.get("answer", "")).strip()
            if not answer or answer == _NOT_FOUND:
                return AgentResult(agent=self.name, ok=False, error="NOT_FOUND")
            return AgentResult(
                agent=self.name,
                ok=True,
                payload={
                    "answer": answer,
                    "cited_pages": [str(p) for p in result.get("cited_pages", [])],
                    "rounds": int(result.get("rounds", 0)),
                    "route": result.get("route") or {},
                    # 原始引擎返回值：doc 单终端事件直接透传给前端
                    "data": result,
                },
            )
        except Exception as e:
            logger.error(f"文档子 Agent 执行失败：{e}")
            return AgentResult(agent=self.name, ok=False, error=str(e)[:200])
