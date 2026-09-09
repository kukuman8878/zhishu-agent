"""
知识复用应答节点（knowledge 分支）

用途：classify_route 知识库召回命中沉淀问题时，短路到本节点直接复用存量答案，
以 message 事件（纯文本）返回前端，不再进入召回/SQL 等业务链路。

与 answer_general 保持一致：不发 progress 事件（前端不展示数据分析流程图），
只返回一条干净的 message 文本；同时累加该知识条目的命中次数（hit_count），
失败只记日志，不影响回答。
"""

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger

# 知识条目答案缺失时的兜底文案：理论上不会出现（召回时答案已存在）
_FALLBACK_MESSAGE = "抱歉，这条沉淀知识暂时无法复用，请换个问法试试。"


# 知识复用应答节点：发出 message 终态并异步累加命中次数；参数 state=含 knowledge_answer/item_id，runtime=携带知识 MySQL 仓储/stream_writer
async def answer_knowledge(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """直接复用知识库沉淀答案，并统计该条目的复用命中次数"""

    writer = runtime.stream_writer
    answer = state.get("knowledge_answer") or ""
    item_id = state.get("knowledge_item_id")
    score = state.get("knowledge_score")

    # 命中计数是旁路统计：失败不能影响知识复用本身的回答
    if item_id:
        try:
            await runtime.context["knowledge_mysql_repository"].increment_hit_count(
                item_id
            )
        except Exception as e:
            logger.warning(f"知识命中计数失败 item_id={item_id}: {e}")

    if not answer:
        logger.warning(f"知识复用命中但答案为空：item_id={item_id} score={score}")
        answer = _FALLBACK_MESSAGE

    writer({"type": "message", "content": answer})
    logger.info(f"知识复用应答完成：item_id={item_id} score={score}")
    # 复用答案同样写回会话历史，保证多轮追问链条不中断
    return {"messages": [AIMessage(content=answer)]}
