"""
文档问答节点（doc 分支）

职责：当入口路由判定问题属于"从文档/PDF/图/表里找内容"时，本节点把问题
交给进程内文档问答引擎（代码在 app/rag_engine/），拿回答案与引用页码，
并通过 SSE doc 事件返回前端。

降级策略：
  - 配置禁用（doc_engine.enabled=false）或客户端未初始化：直接回复 message
    提示文档问答未启用，避免抛出 500；
  - 引擎返回 answer=NOT_FOUND：回复 message 说明文档中未找到答案；
  - 引擎内部异常：回复 message 说明文档问答引擎暂不可用，并建议改问数据类问题；
  - 引擎可用但仍在查：先发 progress 事件（粗粒度），结束再发 doc 终态事件。
"""

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger

# 引擎在自身异常时返回的兜底答案标记
_NOT_FOUND = "NOT_FOUND"

# 文档问答链路在引擎异常时回复用户的兜底文案
_DOC_UNAVAILABLE = (
    "文档问答引擎暂不可用。你可以试试数据类问题（例如：统计华北地区的销售总额），"
    "或稍后再询问文档内容。"
)


# 文档问答节点：把问题交给进程内文档问答引擎并回写 doc_answer/引用页码等状态，异常或未启用时降级为 message；参数 state=含 query 的图状态，runtime=运行时(取 rag_client/stream_writer)
async def doc_query(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """把问题交给进程内文档问答引擎，产出 doc 终态事件"""

    writer = runtime.stream_writer
    query = state["query"]
    step = "文档检索与问答"
    writer({"type": "progress", "step": step, "status": "running"})

    # 引擎未启用时直接降级，不让文档引擎不可用拖垮整个请求
    rag_client = runtime.context["rag_client"]
    if rag_client is None:
        logger.warning("文档问答引擎未启用，doc 路由降级为 message")
        writer({"type": "progress", "step": step, "status": "error"})
        writer({"type": "message", "content": _DOC_UNAVAILABLE})
        # 仍需落空 doc 标记，让 hybrid 栅栏能感知 doc 分支已完成
        return {
            "doc_answer": "",
            "doc_cited_pages": [],
            "doc_rounds": 0,
            "doc_route": {},
            "messages": [AIMessage(content=_DOC_UNAVAILABLE)],
        }

    try:
        result = await rag_client.answer(query)
        answer = result.get("answer", "")
        if not answer or answer == _NOT_FOUND:
            # 记录拒答原因（重排分过低 / 无可溯源引用 / 语料确实未命中）
            reason = (result.get("reason") or "").strip()
            logger.info(
                f"文档未作答（未找到或校验拒绝）：query={query} reason={reason}"
            )
            fallback = (
                "在已索引的文档中未找到足够依据回答该问题"
                "（已做相关性过滤与可溯源校验）。你可以换个说法，或询问数据类问题。"
            )
            writer({"type": "progress", "step": step, "status": "success"})
            writer({"type": "message", "content": fallback})
            return {
                "doc_answer": answer,
                "doc_cited_pages": [],
                "doc_rounds": int(result.get("rounds", 0)),
                "doc_route": result.get("route") or {},
                "messages": [AIMessage(content=fallback)],
            }

        # 正常返回：写回 state 供 hybrid 综合使用，并向前端发 doc 终态事件
        cited_pages = result.get("cited_pages", [])
        logger.info(f"文档问答完成：rounds={result.get('rounds')} pages={cited_pages}")
        writer({"type": "progress", "step": step, "status": "success"})
        writer({"type": "doc", "data": result})
        return {
            "doc_answer": answer,
            "doc_cited_pages": [str(p) for p in cited_pages],
            "doc_rounds": int(result.get("rounds", 0)),
            "doc_route": result.get("route") or {},
            "messages": [AIMessage(content=answer)],
        }
    except Exception as e:
        # 网络/HTTP 异常：引擎不可用不应中断整个会话，降级为 message
        logger.error(f"文档问答失败：{e}")
        writer({"type": "progress", "step": step, "status": "error"})
        writer({"type": "message", "content": _DOC_UNAVAILABLE})
        # 仍需落空 doc 标记，让 hybrid 栅栏能感知 doc 分支已完成
        return {
            "doc_answer": "",
            "doc_cited_pages": [],
            "doc_rounds": 0,
            "doc_route": {},
            "messages": [AIMessage(content=_DOC_UNAVAILABLE)],
        }
