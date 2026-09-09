"""
跨源（hybrid）并行扇出节点

用途：LangGraph 的 conditional_edges 一次只能返回一个目标节点，无法从
classify_route 直接同时进入 SQL 分支与文档分支。本节点作为一个"扇出闸门"：
classify_route 判定 route=hybrid 时先进到本节点，再由本节点的两条普通出边
分别并行触发 extract_keywords（SQL 链）与 doc_query（文档链），
最终两条子链在 synthesize 节点汇合（LangGraph 会等待本轮实际启动的两条入边
都完成后才进入 synthesize）。
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState


# hybrid 扇出闸门：空操作不修改状态，仅作为同时并行触发 SQL 链与文档链两条出边的汇合入口；参数 state=图状态，runtime=运行时
async def hybrid_split(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """不修改任何状态，仅作为并行扇出的汇合点入口"""

    # 空操作：真正的分发由 graph.py 中本节点的两条静态出边完成
    return {}
