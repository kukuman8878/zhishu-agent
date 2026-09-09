"""
SQL 分析子图（可独立编译复用的数据分析 12 节点链路）

用途：hybrid v2（计划-执行）需要对每个"sql 子任务"分别跑一遍完整的数据分析链路。
LangGraph 不允许在图节点内部递归调用同一张正在执行的主图，因此把数据分析链路
单独编译成一个 sql_subgraph 对象，供 hybrid_v2 节点通过 ainvoke 复用。
主图的 sql/doc 路由仍直接内联使用这些节点函数（保持逐节点 SSE 零回归），
本模块只是额外提供一份"同拓扑、独立编译"的实例。

拓扑定义抽取为 build_sql_chain 函数，主图和子图共享同一份边定义，避免重复维护。
"""

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.filter import filter_metric, filter_table
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall import recall_column, recall_metric, recall_value
from app.agent.nodes.sql_chain import (
    MAX_SQL_RETRIES,
    correct_sql,
    generate_sql,
    run_sql,
    validate_sql,
)
from app.agent.nodes.verify_result import verify_result
from app.agent.state import DataAgentState
from app.core.metrics import timed_node


# validate_sql 的条件路由：无错误→执行；有错误且修正次数未耗尽→correct_sql；
# 修正次数耗尽仍不合语法→直接执行（执行失败由 run_sql 兜底终止），避免无限循环；参数 state=含 error/sql_attempts 的图状态
def _validate_router(state: DataAgentState) -> str:
    """校验结果路由：通过→执行，未通过→修正，修正次数耗尽→放行执行由 run_sql 收口"""
    if state["error"] is None:
        return "run_sql"
    if state.get("sql_attempts", 0) >= MAX_SQL_RETRIES:
        return "run_sql"
    return "correct_sql"


# run_sql 的条件路由：执行失败(run_error 非空)回到 correct_sql 看错误自愈，成功则进结果自检；参数：无
def _make_run_sql_router():
    """生成 run_sql 的条件边回调：失败→correct_sql 自愈环，成功→verify_result 自检"""

    def _router(state: DataAgentState) -> str:
        if state.get("run_error"):
            return "correct_sql"
        return "verify_result"

    return _router, {"correct_sql": "correct_sql", "verify_result": "verify_result"}


def build_sql_chain(
    builder: StateGraph, with_start: bool = True, end_node: str = END
) -> None:
    """向 StateGraph builder 注册 SQL 分析链路的 12 个节点和所有边。

    主图和子图共享此函数，新增/删除节点只需改这一处。
    参数 builder=已创建但尚未编译的 StateGraph 实例，要求已注册 extract_keywords 等节点；
    with_start=是否同时添加 START→extract_keywords 入口边。独立子图需要该入口，
    主图 START 已连 classify_route，调用时应传 with_start=False 避免产生并行双入口；
    end_node=run_sql 正常收口节点（主图 synthesize，独立子图默认 END）。
    """
    if with_start:
        builder.add_edge(START, "extract_keywords")
    builder.add_edge("extract_keywords", "recall_column")
    builder.add_edge("extract_keywords", "recall_value")
    builder.add_edge("extract_keywords", "recall_metric")
    builder.add_edge("recall_column", "merge_retrieved_info")
    builder.add_edge("recall_value", "merge_retrieved_info")
    builder.add_edge("recall_metric", "merge_retrieved_info")
    builder.add_edge("merge_retrieved_info", "filter_table")
    builder.add_edge("merge_retrieved_info", "filter_metric")
    builder.add_edge("filter_table", "add_extra_context")
    builder.add_edge("filter_metric", "add_extra_context")
    builder.add_edge("add_extra_context", "generate_sql")
    builder.add_edge("generate_sql", "validate_sql")
    builder.add_conditional_edges(
        source="validate_sql",
        path=_validate_router,
        path_map={"run_sql": "run_sql", "correct_sql": "correct_sql"},
    )
    # 修正后必须重新过校验，形成 generate→validate→(correct→validate)* 的修正闭环
    builder.add_edge("correct_sql", "validate_sql")
    # run_sql 条件路由：执行失败时回 correct_sql 看错误自愈，成功进 verify_result 自检
    router, path_map = _make_run_sql_router()
    builder.add_conditional_edges(source="run_sql", path=router, path_map=path_map)
    # 结果自检（sql 路由质检/其他路由透传）后统一收口
    builder.add_edge("verify_result", end_node)


def _register_sql_nodes(builder: StateGraph) -> None:
    """向 builder 注册 SQL 链路涉及的所有节点函数（统一包一层耗时统计）"""
    builder.add_node(
        "extract_keywords", timed_node("extract_keywords")(extract_keywords)
    )
    builder.add_node("recall_column", timed_node("recall_column")(recall_column))
    builder.add_node("recall_value", timed_node("recall_value")(recall_value))
    builder.add_node("recall_metric", timed_node("recall_metric")(recall_metric))
    builder.add_node(
        "merge_retrieved_info", timed_node("merge_retrieved_info")(merge_retrieved_info)
    )
    builder.add_node("filter_metric", timed_node("filter_metric")(filter_metric))
    builder.add_node("filter_table", timed_node("filter_table")(filter_table))
    builder.add_node(
        "add_extra_context", timed_node("add_extra_context")(add_extra_context)
    )
    builder.add_node("generate_sql", timed_node("generate_sql")(generate_sql))
    builder.add_node("validate_sql", timed_node("validate_sql")(validate_sql))
    builder.add_node("correct_sql", timed_node("correct_sql")(correct_sql))
    builder.add_node("run_sql", timed_node("run_sql")(run_sql))
    builder.add_node("verify_result", timed_node("verify_result")(verify_result))


# 构建并编译一份独立于主图的 SQL 分析子图（12 节点同拓扑），供 hybrid v2 对每个 sql 子任务 ainvoke
def build_sql_graph():
    """构建一份独立编译的 SQL 分析子图，状态/上下文模式与主图一致"""

    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    _register_sql_nodes(builder)
    build_sql_chain(builder)
    return builder.compile()


# 模块级单例：整个进程只需编译一次
sql_graph = build_sql_graph()
