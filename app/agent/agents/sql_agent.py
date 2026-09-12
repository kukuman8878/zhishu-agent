"""
SQL 数据分析子 Agent

封装完整的数据分析链路：元数据三路混合检索（字段/指标 Qdrant 向量 + 取值 ES 全文）
→ Meta MySQL 补齐 → 过滤 → SQL 生成/校验/自愈/执行。
复用独立编译的 sql_graph（app/agent/sql_subgraph.py），供 orchestrator 按子任务调用。
"""

from langgraph.runtime import Runtime

from app.agent.agents.base import AgentResult
from app.agent.context import DataAgentContext
from app.agent.sql_subgraph import sql_graph
from app.agent.state import DataAgentState
from app.agent.tool_loop import collect_tool_supplement
from app.clients.mcp_client_manager import mcp_client_manager
from app.core.log import logger


class SQLAgent:
    """数据分析子 Agent：跑完整 SQL 链路并返回行数据"""

    name = "sql"

    # 执行一次数据分析子任务，返回 AgentResult(rows/sql)；参数 question=自然语言子问题，runtime=运行上下文，history 未使用
    async def run(
        self, question: str, runtime: Runtime[DataAgentContext], history: str = ""
    ) -> AgentResult:
        try:
            # 专属工具预处理：如配置了归属 sql 的 MCP 工具，先静默调用取补充信息，
            # 拼进子问句再走原生检索/SQL 链路（无专属工具时零额外开销）
            tools = await mcp_client_manager.get_tools(self.name)
            if tools:
                supplement = await collect_tool_supplement(question, self.name, tools)
                if supplement:
                    question = f"{question}\n工具补充信息：{supplement}"
            out = await sql_graph.ainvoke(
                DataAgentState(query=question), context=runtime.context
            )
            rows = out.get("sql_rows") or []
            return AgentResult(
                agent=self.name,
                ok=bool(rows),
                payload={"rows": rows, "sql": out.get("sql") or ""},
            )
        except Exception as e:
            logger.error(f"SQL 子 Agent 执行失败：{e}")
            return AgentResult(agent=self.name, ok=False, error=str(e)[:200])


# 模块级单例，供注册表复用
sql_agent = SQLAgent()
