"""
跨源综合节点（hybrid 分支收口）

职责：hybrid 路由下 SQL 分支（run_sql）与文档分支（doc_query）都完成后，
本节点把 state 里的 sql_rows 与 doc_answer/doc_cited_pages 组装成一份
"数据结论 + 文档说明 + 综合判断"三段式 markdown，并通过 SSE hybrid 事件返回。

非 hybrid 路由（sql/doc/chat）也会经由本节点，此时直接透传不做任何处理，
保证 SQL 单路由的既有事件流（result 事件）零回归。
"""

from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


# 把 SQL 查询返回的行数据归一化为 markdown 表格文本（非 dict/空数据时做兜底处理）；参数 rows=SQL 执行返回的行数据(dict 列表或单个)
def _rows_to_markdown(rows) -> str:
    """把 SQL 行数据归一化为 markdown 表格文本"""

    if not rows:
        return "（数仓未返回数据）"
    if not isinstance(rows, list):
        rows = [rows]
    if len(rows) == 0 or not isinstance(rows[0], dict):
        return str(rows)
    keys = list(rows[0].keys())
    header = "| " + " | ".join(str(k) for k in keys) + " |"
    sep = "| " + " | ".join(["---"] * len(keys)) + " |"
    lines = [header, sep]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(k, "")) for k in keys) + " |")
    return "\n".join(lines)


# 综合节点：hybrid 路由在 sql/doc 双分支都完成后拼接三段式答案，其余路由透传；参数 state=含 sql_rows/doc_answer 等结果，runtime=取 stream_writer/LLM
async def synthesize(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """hybrid 路由：合并 sql_rows 与文档答案；其余路由透传"""

    # 非 hybrid 直接透传：sql 路由的 result 事件已在 run_sql 发出，这里不再重复
    if state.get("route") != "hybrid":
        return {}

    # 双分支完成栅栏：LangGraph 对不同深度的并行分支不做 join，synthesize 可能在
    # doc_query 或 run_sql 单边完成后就被提前触发。这里只在两侧都落盘后才真正产出；
    # 先到的一次静默返回，由后完成的分支负责最终综合，避免重复/空结果事件。
    # 注意用 is not None 判断：多轮会话下 QueryService 会把上轮残留字段重置为 None
    doc_done = state.get("doc_answer") is not None
    sql_done = state.get("sql_rows") is not None
    if not (doc_done and sql_done):
        return {}

    writer = runtime.stream_writer
    step = "跨源综合"
    writer({"type": "progress", "step": step, "status": "running"})

    sql_rows = state.get("sql_rows") or []
    doc_answer = state.get("doc_answer") or ""
    cited_pages = state.get("doc_cited_pages") or []

    # 两侧都没有内容时给出明确说明，避免输出空白
    if not sql_rows and not doc_answer:
        content = (
            "【数据结论】\n（数仓未检索到该口径数据）\n\n"
            "【文档说明】\n（文档中未找到相关内容）\n\n"
            "【综合判断】\n两侧均未命中，请调整问题口径后重试。"
        )
        writer({"type": "progress", "step": step, "status": "success"})
        writer({"type": "hybrid", "data": {"content": content}})
        # hybrid 综合结论写回会话历史（非 hybrid 路由由各终端节点自行写入）
        return {"hybrid_content": content, "messages": [AIMessage(content=content)]}

    data_section = _rows_to_markdown(sql_rows)
    doc_section = doc_answer or "（文档中未找到相关内容）"
    if cited_pages:
        doc_section += f"\n\n引用页码：{'、'.join(str(p) for p in cited_pages)}"

    # 用主模型（temperature 0）做一次组织型总结，稳定输出三段式；逐 token 流式
    try:
        prompt = PromptTemplate(
            template=load_prompt("synthesize_hybrid"),
            input_variables=["query", "data_section", "doc_section"],
        )
        chain = prompt | llm | StrOutputParser()

        pieces: list[str] = []
        reset_sent = False
        async for chunk in chain.astream(
            {
                "query": state["query"],
                "data_section": data_section,
                "doc_section": doc_section,
            }
        ):
            piece = chunk if isinstance(chunk, str) else str(chunk)
            if piece:
                pieces.append(piece)
                writer({"type": "delta", "content": piece, "reset": not reset_sent})
                reset_sent = True
        content = "".join(pieces).strip()

    except Exception as e:
        # LLM 兜底：失败时退化为确定性拼接，保证 hybrid 至少有一份可读答案
        logger.error(f"跨源综合 LLM 调用失败，使用确定性拼接：{e}")
        content = f"【数据结论】\n{data_section}\n\n【文档说明】\n{doc_section}"

    writer({"type": "progress", "step": step, "status": "success"})
    writer({"type": "hybrid", "data": {"content": content}})
    # hybrid 综合结论写回会话历史，支撑下一轮追问
    return {"hybrid_content": content, "messages": [AIMessage(content=content)]}
