"""
结果自检节点（SQL 链路收口前的质检）

职责：run_sql 执行成功后、收口前，对查询结果做一次轻量合理性自检：
  - 结果为空 → 确定性提示口径/条件可能不匹配（不消耗 LLM）；
  - 结果非空 → 用廉价 chat_llm 对照问题做一次质检，可疑时发 note 事件提醒用户。
自检只提醒、不阻断：正常结果静默通过，保持既有事件流零回归。

触发范围：仅主图 sql 路由（state.route=="sql"）自检；hybrid v1 的 SQL 分支由
synthesize 统一综合、hybrid v2 子任务无 route 标记，均跳过，避免重复自检。
"""

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import chat_llm
from app.agent.llm_utils import retry_async
from app.agent.nodes.synthesize import _rows_to_markdown
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 空结果的确定性提示文案（不消耗 LLM）
_EMPTY_RESULT_NOTE = (
    "未查询到数据。可能统计口径不匹配或筛选条件过严，建议调整条件后重试。"
)


# 结果自检节点：空结果发确定性提示，非空结果让廉价模型质检后发 note 提醒；参数 state=含 query/sql_rows/route，runtime=携带流式写器
async def verify_result(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """对 SQL 执行结果做轻量自检，可疑时发 note 事件提醒（不阻断结果返回）"""

    # 仅主图 sql 路由自检：hybrid 分支/子图任务跳过，避免重复与拖慢
    if state.get("route") != "sql":
        return {}

    writer = runtime.stream_writer
    step = "结果自检"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        rows = state.get("sql_rows") or []
        query = state["query"]

        # 空结果：确定性提示，不消耗模型调用
        if not rows:
            logger.info(f"结果自检：空结果提示 query={query[:40]}")
            writer({"type": "progress", "step": step, "status": "success"})
            writer({"type": "note", "content": _EMPTY_RESULT_NOTE})
            return {"verify_note": _EMPTY_RESULT_NOTE}

        # 非空结果：用廉价 chat_llm 对照问题做一次质检（截断行数避免长上下文）
        markdown = _rows_to_markdown(rows[:20])
        prompt = PromptTemplate(
            template=load_prompt("verify_result"),
            input_variables=["query", "rows"],
        )
        chain = prompt | chat_llm | JsonOutputParser()
        raw = await retry_async(
            chain.ainvoke,
            {"query": query, "rows": markdown},
            node_name="verify_result",
        )

        ok = bool(raw.get("ok", True))
        note = str(raw.get("note", "")).strip()
        if not ok or note:
            # 质检发现可疑：发 note 提醒（附在结果旁，不覆盖结果）
            writer(
                {
                    "type": "note",
                    "content": note or "结果可能未完整回答该问题，请核对。",
                }
            )
            writer({"type": "progress", "step": step, "status": "success"})
            return {"verify_note": note}
        writer({"type": "progress", "step": step, "status": "success"})
        return {}
    except Exception as e:
        # fail-open：自检异常静默跳过，不影响已产出的结果
        logger.warning(f"结果自检失败，跳过：{e}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {}
