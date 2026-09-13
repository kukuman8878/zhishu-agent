"""
智数 Agent 图编排

使用 LangGraph 把问数智能体的各个节点串成一条可观测的执行链路
当前链路已经落地关键词抽取和多路召回，字段和指标走 Qdrant 向量检索，字段取值走 ES 全文检索
整体流程先抽取用户问题关键词，再并行召回字段 字段取值和指标信息，
随后合并召回结果 过滤候选表和指标 补充额外上下文，最后生成 校验 修正并执行 SQL

SQL 链路拓扑复用 sql_subgraph.build_sql_chain，主图和子图共享同一份边定义。
"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.answer_general import answer_general
from app.agent.nodes.answer_knowledge import answer_knowledge
from app.agent.nodes.classify_route import classify_route
from app.agent.nodes.doc_query import doc_query
from app.agent.nodes.hybrid_split import hybrid_split
from app.agent.nodes.hybrid_v2 import hybrid_v2
from app.agent.nodes.orchestrator import orchestrator
from app.agent.nodes.synthesize import synthesize
from app.agent.sql_subgraph import _register_sql_nodes, build_sql_chain
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.clients.rerank_client_manager import rerank_client_manager
from app.conf.app_config import app_config
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.knowledge_mysql_repository import (
    KnowledgeMySQLRepository,
)
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.knowledge_qdrant_repository import (
    KnowledgeQdrantRepository,
)
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

# StateGraph 声明整张图使用的状态结构和运行时上下文结构
graph_builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)

# 注册非 SQL 链路的专属节点（分类、闲聊、文档、综合、hybrid 扇出/计划）
graph_builder.add_node("classify_route", classify_route)
graph_builder.add_node("answer_general", answer_general)
graph_builder.add_node("answer_knowledge", answer_knowledge)
graph_builder.add_node("doc_query", doc_query)
graph_builder.add_node("synthesize", synthesize)
graph_builder.add_node("hybrid_split", hybrid_split)
graph_builder.add_node("hybrid_v2", hybrid_v2)
# 多智能体编排节点（orchestrator.enabled=true 时由 classify_route 转入）
graph_builder.add_node("orchestrator", orchestrator)

# 注册 SQL 链路的 13 个节点（与 sql_subgraph 共享注册逻辑，并统一包裹耗时统计）
_register_sql_nodes(graph_builder)

# 入口：意图路由
graph_builder.add_edge(START, "classify_route")


# 路由闸门回调：按 state 里的 route 决定下一步进入哪个节点并返回节点名；参数 state=承载 route 等业务数据的 LangGraph 状态
def _route_target(state):
    """意图路由结果 → 下一个节点名

    五类路由均已落地：
      knowledge → 知识沉淀复用，直接返回存量答案；
      sql     → 进入 SQL 分析链；
      doc     → 进入文档问答；
      chat    → 通用问答；
      hybrid  → 按配置 hybrid.v2_enabled 选择：v1=hybrid_split 并行扇出后 synthesize；
                v2=hybrid_v2 计划-执行（子任务复用 sql_graph/文档引擎）。
    """
    route = state.get("route", "sql")
    if route == "hybrid":
        return "hybrid_v2" if app_config.hybrid.v2_enabled else "hybrid_split"
    return {
        "sql": "extract_keywords",
        "chat": "answer_general",
        "doc": "doc_query",
        "knowledge": "answer_knowledge",
        # orchestrator.enabled=true 时 classify_route 直接产出该路由，交给主 Agent
        "orchestrator": "orchestrator",
    }.get(route, "extract_keywords")


# 意图路由结果决定走哪条业务子链
graph_builder.add_conditional_edges(
    source="classify_route",
    path=_route_target,
    path_map={
        "extract_keywords": "extract_keywords",
        "answer_general": "answer_general",
        "answer_knowledge": "answer_knowledge",
        "doc_query": "doc_query",
        "hybrid_split": "hybrid_split",
        "hybrid_v2": "hybrid_v2",
        "orchestrator": "orchestrator",
    },
)
# 主 Agent 内部完成计划/分发/综合并发出终端事件，直接收口
graph_builder.add_edge("orchestrator", END)
# hybrid v2 内部自行完成计划/执行/综合并发出 hybrid 终态，直接收口
graph_builder.add_edge("hybrid_v2", END)
# hybrid 闸门：同时扇出到 SQL 链与文档链，两条子链都结束才汇入 synthesize
graph_builder.add_edge("hybrid_split", "extract_keywords")
graph_builder.add_edge("hybrid_split", "doc_query")

# 复用共享的 SQL 链路拓扑定义（边 + 条件边），保证主图与 sql_subgraph 拓扑一致；
# 主图入口已连 classify_route，因此不重复添加 START→extract_keywords 边；
# end_node=synthesize：run_sql 成功后（或修正重试终止后）统一汇入 synthesize 收口
build_sql_chain(graph_builder, with_start=False, end_node="synthesize")

# 所有业务子链统一汇入 synthesize 收口：hybrid 在此合并，
# sql/doc/chat 单路由时 synthesize 透传，保证原事件流零回归
graph_builder.add_edge("doc_query", "synthesize")
graph_builder.add_edge("answer_general", "synthesize")
graph_builder.add_edge("answer_knowledge", "synthesize")
graph_builder.add_edge("synthesize", END)


# 图编译入口：checkpointer 由外层决定。默认进程内（InMemorySaver），
# 服务启动时 lifespan 会用持久化 SQLite checkpointer（工作记忆）重建本图
def build_graph(checkpointer=None):
    """用给定 checkpointer 编译主图；checkpointer 为空时退回 InMemorySaver"""

    return graph_builder.compile(checkpointer=checkpointer or InMemorySaver())


# 以 thread_id 为会话键持久化 messages 历史，支撑"那按月呢"这类多轮省略式追问。
# 默认图仅供脚本/单测导入；服务运行时由 lifespan 调用 set_checkpointer 替换。
graph = build_graph()


# 替换主图 checkpointer（lifespan 初始化工作记忆持久化后调用）；参数 checkpointer=新的检查点存储器
def set_checkpointer(checkpointer):
    """重建主图以挂载新的 checkpointer（工作记忆持久化）"""

    global graph
    graph = build_graph(checkpointer)


# print(graph.get_graph().draw_mermaid())

if __name__ == "__main__":
    # 本地调试：初始化各客户端后跑一条完整的 SQL 分析链路并打印节点流式输出；参数：无
    async def test():
        """本地调试关键词抽取和字段 指标 取值三路召回链路"""

        # 多路召回和上下文补全会访问 Qdrant、Embedding、ES、Meta MySQL 和 DW MySQL
        qdrant_client_manager.init()
        embedding_client_manager.init()
        rerank_client_manager.init()
        es_client_manager.init()
        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()

        # Meta MySQL 用来补齐元数据，DW MySQL 用来读取数据库方言和版本
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            meta_mysql_repository = MetaMySQLRepository(meta_session)
            dw_mysql_repository = DWMySQLRepository(dw_session)

            # 字段和指标分别使用不同 Qdrant collection，取值检索使用 ES index
            column_qdrant_repository = ColumnQdrantRepository(
                qdrant_client_manager.client
            )
            metric_qdrant_repository = MetricQdrantRepository(
                qdrant_client_manager.client
            )
            value_es_repository = ValueESRepository(es_client_manager.client)
            knowledge_qdrant_repository = KnowledgeQdrantRepository(
                qdrant_client_manager.client
            )

            # 当前只需要传入原始问题，后续节点会逐步写回召回、过滤和额外上下文结果
            state = DataAgentState(query="统计华北地区的销售总额")
            context = DataAgentContext(
                column_qdrant_repository=column_qdrant_repository,
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=metric_qdrant_repository,
                value_es_repository=value_es_repository,
                meta_mysql_repository=meta_mysql_repository,
                dw_mysql_repository=dw_mysql_repository,
                rerank_client=rerank_client_manager.client,
                knowledge_mysql_repository=KnowledgeMySQLRepository(meta_session),
                knowledge_qdrant_repository=knowledge_qdrant_repository,
            )

            # stream_mode="custom" 会接收各节点通过 runtime.stream_writer 写出的进度信息
            async for chunk in graph.astream(
                input=state, context=context, stream_mode="custom"
            ):
                print(chunk)

        # 关闭显式创建的异步客户端，避免本地调试时连接资源悬挂
        await qdrant_client_manager.close()
        await embedding_client_manager.close()
        await rerank_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()

    asyncio.run(test())
