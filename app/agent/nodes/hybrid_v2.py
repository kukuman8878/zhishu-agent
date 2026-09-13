"""
跨源综合 v2 节点（计划-执行模式）

职责：hybrid 路由且配置 hybrid.v2_enabled=true 时，把用户问题拆成带引擎标记的
子任务（sql/doc，<=3 个），逐任务在对应引擎上执行：
  - doc 子任务   → 调进程内文档问答引擎（多个并行）；
  - sql 子任务   → 复用独立编译的 sql_graph（app/agent/sql_subgraph.py）跑整条
    数据分析链路取 sql_rows；
  - 文档→SQL 桥接：sql 子任务 depends_on 文档子任务时，把文档答案前段回填进
    sql 子任务的自然语言，让 SQL 链自带取值召回去命中真实枚举；
  - 失败互转：sql 子任务失败且语义像文档 → 尝试 doc；doc 子任务 NOT_FOUND 且语义
    像取数 → 尝试 sql；都不行则如实标注，绝不编造。
所有子任务结果收齐后，综合成"数据结论 + 文档说明 + 综合判断"三段式，发一条 hybrid 终态事件。

相比 v1（整句并行-拼接），v2 用子任务隔离：单个子任务失败不会拖垮整条链路，
且支持"按文档口径统计"这类依赖型跨域问题。
"""

import asyncio
import json
import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.llm_utils import retry_async
from app.agent.nodes.synthesize import _rows_to_markdown
from app.agent.sql_subgraph import sql_graph
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

_NOT_FOUND = "NOT_FOUND"

# 子任务数量/长度守卫（避免 plan 输出畸形任务拖垮链路）
_MAX_TASKS = 3
_MIN_QUESTION_WORDS = 3


# 从模型输出中稳健解析计划 JSON，非法/畸形内容返回空计划；参数 raw=plan 模型输出的原始文本
def _parse_plan(raw: str) -> dict:
    """从模型输出稳健解析计划 JSON，失败返回空计划（调用方回退 v1 行为）"""

    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except Exception:
        return {}
    degrade = str(payload.get("degrade", "none")).strip().lower()
    tasks_raw = payload.get("tasks")
    if not isinstance(tasks_raw, list):
        return {"degrade": degrade, "tasks": []}
    tasks = []
    for item in tasks_raw[:_MAX_TASKS]:
        if not isinstance(item, dict):
            continue
        engine = str(item.get("engine", "")).strip().lower()
        question = str(item.get("question", "")).strip()
        if engine not in ("sql", "doc") or not question:
            continue
        deps = item.get("depends_on", [])
        deps = [str(d).strip() for d in deps] if isinstance(deps, list) else []
        tasks.append(
            {
                "task_id": str(item.get("task_id", f"t{len(tasks) + 1}")),
                "engine": engine,
                "question": question,
                "depends_on": deps,
            }
        )
    return {"degrade": degrade if degrade in ("sql", "doc") else "none", "tasks": tasks}


# 任务列表守卫：校验任务数量(1~3)与每个子问句的字数下限是否合法；参数 tasks=解析出的子任务列表
def _tasks_valid(tasks) -> bool:
    """守卫：至少 1 个、至多 3 个实质任务，与文档引擎的子问题守卫同理"""
    if not tasks:
        return False
    if not (1 <= len(tasks) <= _MAX_TASKS):
        return False
    for t in tasks:
        words = len(re.findall(r"[a-z0-9\u4e00-\u9fff]+", t["question"]))
        if words < _MIN_QUESTION_WORDS:
            return False
    return True


# 用主模型把跨源问句拆解为带引擎标记的子任务计划；参数 query=用户原始问题，返回 dict 计划(失败为空)
async def _plan(query: str) -> dict:
    """用主模型把跨源问句拆成子任务；失败返回空计划"""
    try:
        prompt = PromptTemplate(
            template=load_prompt("plan_hybrid"),
            input_variables=["query"],
        )
        chain = prompt | llm | StrOutputParser()
        raw = await retry_async(
            chain.ainvoke, {"query": query}, node_name="hybrid_plan"
        )
        return _parse_plan(raw)
    except Exception as e:
        logger.error(f"跨源计划拆解失败：{e}")
        return {}


# 执行单个文档子任务：调用文档问答客户端并返回 ok/answer/cited_pages；参数 rag_client=文档问答客户端，question=子问句
async def _run_doc_task(rag_client, question: str) -> dict:
    """执行单个文档子任务"""
    try:
        if rag_client is None:
            return {
                "ok": False,
                "answer": "",
                "cited_pages": [],
                "error": "engine disabled",
            }
        result = await rag_client.answer(question)
        answer = str(result.get("answer", "")).strip()
        if not answer or answer == _NOT_FOUND:
            return {
                "ok": False,
                "answer": answer,
                "cited_pages": [],
                "error": "NOT_FOUND",
            }
        return {
            "ok": True,
            "answer": answer,
            "cited_pages": [str(p) for p in result.get("cited_pages", [])],
            "rounds": int(result.get("rounds", 0)),
        }
    except Exception as e:
        return {"ok": False, "answer": "", "cited_pages": [], "error": str(e)[:200]}


# 执行单个 sql 子任务：用独立编译的 sql_graph 跑完整数据分析链路并返回行数据；参数 context=运行时上下文(含各仓储/客户端)，question=子问句
async def _run_sql_task(context, question: str) -> dict:
    """执行单个 sql 子任务：复用独立编译的 sql_graph 跑完整数据分析链路"""
    try:
        out = await sql_graph.ainvoke(DataAgentState(query=question), context=context)
        rows = out.get("sql_rows") or []
        return {"ok": bool(rows), "rows": rows, "error": ""}
    except Exception as e:
        logger.error(f"sql 子任务失败：{e}")
        return {"ok": False, "rows": [], "error": str(e)[:200]}


# 粗略判断问句是否偏文档语料，用于 sql/doc 失败互转的触发条件；参数 text=子问句文本，命中特征词返回 True
def _docspeak(text: str) -> bool:
    """粗略判断子问句是否偏文档语料（用于失败互转的触发条件）"""
    return any(
        k in text
        for k in (
            "文档",
            "报告",
            "手册",
            "规格",
            "配置",
            "图中",
            "表中",
            "说明",
            "原理",
        )
    )


# hybrid v2 主逻辑：计划拆解→执行 sql/doc 子任务(含桥接与失败互转)→汇总综合，发出 hybrid 终态；参数 state=含用户 query，runtime=含 rag_client 等上下文与 stream_writer
async def hybrid_v2(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """hybrid v2：计划 → 执行（sql/doc 子任务）→ 综合"""

    writer = runtime.stream_writer
    query = state["query"]
    rag_client = runtime.context.get("rag_client")

    # 第 1 步：计划拆解（失败/无效则退化为 v1 的两个整句任务）
    writer({"type": "progress", "step": "跨源计划拆解", "status": "running"})
    plan = await _plan(query)
    tasks = plan.get("tasks", []) if _tasks_valid(plan.get("tasks")) else []
    degrade = plan.get("degrade", "none")
    if not tasks:
        # 回退：sql/doc 各跑整句（等价 v1，但保留子任务级失败隔离）
        tasks = [
            {"task_id": "t1", "engine": "sql", "question": query, "depends_on": []},
            {"task_id": "t2", "engine": "doc", "question": query, "depends_on": []},
        ]
    if degrade == "sql":
        tasks = [
            {"task_id": "t1", "engine": "sql", "question": query, "depends_on": []}
        ]
    elif degrade == "doc":
        tasks = [
            {"task_id": "t1", "engine": "doc", "question": query, "depends_on": []}
        ]
    writer({"type": "progress", "step": "跨源计划拆解", "status": "success"})

    # 第 2 步：执行（doc 任务并行先跑，产出桥接文本供依赖它的 sql 任务使用）
    results: dict[str, dict] = {}
    doc_tasks = [t for t in tasks if t["engine"] == "doc"]
    sql_tasks = [t for t in tasks if t["engine"] == "sql"]

    for t in doc_tasks:
        writer(
            {
                "type": "progress",
                "step": f"文档子任务：{t['question'][:20]}",
                "status": "running",
            }
        )
    if doc_tasks:
        done_docs = await asyncio.gather(
            *[_run_doc_task(rag_client, t["question"]) for t in doc_tasks]
        )
        for t, res in zip(doc_tasks, done_docs):
            results[t["task_id"]] = res
            writer(
                {
                    "type": "progress",
                    "step": f"文档子任务：{t['question'][:20]}",
                    "status": "success" if res["ok"] else "error",
                }
            )
    else:
        done_docs = []

    doc_by_id = {
        t["task_id"]: results[t["task_id"]]["answer"]
        for t in doc_tasks
        if results[t["task_id"]]["ok"] and results[t["task_id"]].get("answer")
    }

    for t in sql_tasks:
        step_name = f"数据子任务：{t['question'][:20]}"
        writer({"type": "progress", "step": step_name, "status": "running"})
        question = t["question"]
        # 文档→SQL 桥接：把依赖的文档答案前段拼进自然语言子问，让 SQL 链取值召回命中
        bridge_parts = [doc_by_id[d] for d in t.get("depends_on", []) if d in doc_by_id]
        if bridge_parts:
            question = (
                f"{question}\n参考口径（来自文档）：{('；'.join(bridge_parts))[:600]}"
            )
        res = await _run_sql_task(runtime.context, question)
        # 失败互转 ①：sql 子任务失败且问句偏文档语义 → 尝试 doc 兜底
        if not res["ok"] and _docspeak(t["question"]):
            doc_fb = await _run_doc_task(rag_client, t["question"])
            if doc_fb["ok"]:
                res = {"ok": True, "doc": doc_fb, "rows": []}
        results[t["task_id"]] = res
        writer(
            {
                "type": "progress",
                "step": step_name,
                "status": "success" if res["ok"] else "error",
            }
        )

    # 失败互转 ②：doc 子任务 NOT_FOUND 且问句偏取数 → 尝试 sql 兜底
    for t in doc_tasks:
        res = results[t["task_id"]]
        if not res["ok"] and not _docspeak(t["question"]):
            sql_fb = await _run_sql_task(runtime.context, t["question"])
            if sql_fb["ok"]:
                results[t["task_id"]] = {"ok": True, "sql": sql_fb, "answer": ""}

    # 第 3 步：汇总 + 综合（复用 synthesize 的三段式组织）
    data_sections = []
    doc_sections = []
    for t in tasks:
        res = results.get(t["task_id"], {"ok": False})
        if t["engine"] == "sql":
            rows = res.get("rows") if res.get("ok") else []
            data_sections.append(
                f"- 查询：{t['question'][:60]}\n{_rows_to_markdown(rows)}"
            )
            # sql 失败但 doc 兜底成功时，把文档结果归入文档小节
            doc_fb = res.get("doc")
            if doc_fb and doc_fb.get("ok"):
                doc_sections.append(f"- 文档兜底：{t['question'][:60]}")
                doc_sections.append(doc_fb.get("answer", ""))
        else:
            sql_fb = res.get("sql")
            if sql_fb and sql_fb.get("ok"):
                data_sections.append(
                    f"- SQL 兜底：{t['question'][:60]}\n{_rows_to_markdown(sql_fb.get('rows') or [])}"
                )
                continue
            if res.get("ok"):
                answer = (res.get("answer") or "").strip()
                pages = res.get("cited_pages") or []
                doc_sections.append(
                    f"- 来源：{t['question'][:60]}"
                    + (f"（第{'、'.join(pages)}页）" if pages else "")
                )
                doc_sections.append(answer)
            else:
                doc_sections.append(f"- 未命中：{t['question'][:60]}")

    content = await _compose(query, data_sections, doc_sections, writer)
    writer({"type": "hybrid", "data": {"content": content}})
    return {"hybrid_content": content}


# 把各子任务的数据/文档小节组织成三段式：优先主模型流式组织，失败退化为确定性拼接；参数 query=原问题，data_sections/doc_sections=数据与文档片段列表，writer=流式写器
async def _compose(
    query: str,
    data_sections: list[str],
    doc_sections: list[str],
    writer,
) -> str:
    """把子任务结果组织成三段式；优先走主模型组织（逐 token 流式），失败退化为确定性拼接"""

    data_text = "\n".join(data_sections) or "（数仓未检索到该口径数据）"
    doc_text = "\n".join(doc_sections) or "（文档中未找到相关内容）"
    try:
        prompt = PromptTemplate(
            template=load_prompt("synthesize_hybrid"),
            input_variables=["query", "data_section", "doc_section"],
        )
        chain = prompt | llm | StrOutputParser()

        pieces: list[str] = []
        reset_sent = False
        async for chunk in chain.astream(
            {"query": query, "data_section": data_text, "doc_section": doc_text}
        ):
            piece = chunk if isinstance(chunk, str) else str(chunk)
            if piece:
                pieces.append(piece)
                writer({"type": "delta", "content": piece, "reset": not reset_sent})
                reset_sent = True
        return "".join(pieces).strip()
    except Exception as e:
        logger.error(f"v2 综合失败，使用确定性拼接：{e}")
        return f"【数据结论】\n{data_text}\n\n【文档说明】\n{doc_text}"
