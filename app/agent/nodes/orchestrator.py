"""
多智能体编排节点（Supervisor / 主 Agent）

职责：orchestrator.enabled=true 时，入口 classify_route 直接把问题交给本节点，
由主 Agent 统一完成"知识门 → 计划拆解 → 子 Agent 分发执行 → 终端事件综合"：

  1. 知识门：先调 knowledge 子 Agent，命中沉淀问题即短路（与旧路由行为一致）；
  2. 计划：用主模型把问题拆成带子 Agent 标记的任务（sql/doc/chat，<=max_tasks）；
  3. 分发：doc 子任务并行执行；sql 子任务复用独立编译的 sql_graph，支持把依赖的
     文档答案回填进子问句（文档→SQL 桥接）；存在业务子任务时忽略 chat 子任务；
  4. 终端事件：按成功结果组合映射回前端既有事件类型，保证零回归——
     仅 sql→result；仅 doc→doc；仅 chat/knowledge→message；sql+doc→hybrid。

相比原「路由闸门 + 内联链路」，本节点把各能力收敛为子 Agent 统一契约，
主 Agent 按计划选择与组合调用；默认关闭（orchestrator.enabled=false）。
"""

import asyncio
import json
import re

from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.agents import AGENTS, PLANNABLE_AGENTS, AgentResult
from app.agent.context import DataAgentContext
from app.agent.history import render_history
from app.agent.llm import llm
from app.agent.llm_utils import retry_async
from app.agent.nodes.classify_route import (
    ROUTE_CHAT,
    ROUTE_DOC,
    ROUTE_HYBRID,
    ROUTE_SQL,
    _fast_route,
)
from app.agent.nodes.hybrid_v2 import _compose
from app.agent.nodes.synthesize import _rows_to_markdown
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 业务子 Agent：存在其一即丢弃 chat 子任务（闲聊不参与业务综合）
_BUSINESS_AGENTS = {"sql", "doc"}

# 全部未命中时的兜底文案
_FALLBACK_MESSAGE = (
    "抱歉，本次未能从数仓数据或文档中检索到可用结果。"
    "请调整问题口径后重试，例如：统计华北地区的销售总额。"
)


# 从计划模型输出中稳健解析子任务列表，非法/畸形内容返回空计划；参数 raw=模型输出原文，max_tasks=最大子任务数
def _parse_plan(raw: str, max_tasks: int) -> list[dict]:
    """解析编排计划 JSON，返回 [{task_id, agent, question, depends_on}]，失败返回 []"""

    # 贪婪匹配最外层 JSON 对象（计划含嵌套对象，不能用 [^{}]* 的非贪婪写法）
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except Exception:
        return []
    tasks_raw = payload.get("tasks")
    if not isinstance(tasks_raw, list):
        return []
    tasks: list[dict] = []
    for item in tasks_raw[:max_tasks]:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "")).strip().lower()
        question = str(item.get("question", "")).strip()
        if agent not in PLANNABLE_AGENTS or not question:
            continue
        deps = item.get("depends_on", [])
        deps = [str(d).strip() for d in deps] if isinstance(deps, list) else []
        tasks.append(
            {
                "task_id": str(item.get("task_id", f"t{len(tasks) + 1}")),
                "agent": agent,
                "question": question,
                "depends_on": deps,
            }
        )
    return tasks


# 用主模型把问题拆解为子 Agent 计划；参数 query=用户问题，history=会话历史，返回任务列表(失败为空)
async def _plan(query: str, history: str) -> list[dict]:
    """调用主模型生成编排计划，失败返回空计划（调用方回退为 sql+doc 整句）"""

    try:
        prompt = PromptTemplate(
            template=load_prompt("plan_orchestrator"),
            input_variables=["query", "history"],
        )
        chain = prompt | llm | StrOutputParser()
        raw = await retry_async(
            chain.ainvoke,
            {"query": query, "history": history},
            node_name="orchestrator_plan",
        )
        return _parse_plan(raw, app_config.orchestrator.max_tasks)
    except Exception as e:
        logger.error(f"编排计划拆解失败：{e}")
        return []


# 执行计划：doc 子任务并行先跑，sql 子任务可桥接依赖的文档答案；参数 tasks=计划任务列表，runtime=运行上下文，writer=SSE 写器
async def _execute(tasks: list[dict], runtime, writer) -> dict[str, AgentResult]:
    """按计划执行子 Agent，返回 {task_id: AgentResult}"""

    results: dict[str, AgentResult] = {}
    doc_tasks = [t for t in tasks if t["agent"] == "doc"]
    sql_tasks = [t for t in tasks if t["agent"] == "sql"]

    # 文档子任务并行执行
    if doc_tasks:
        for t in doc_tasks:
            writer(
                {
                    "type": "progress",
                    "step": f"文档子任务：{t['question'][:20]}",
                    "status": "running",
                }
            )
        done = await asyncio.gather(
            *[AGENTS["doc"].run(t["question"], runtime) for t in doc_tasks]
        )
        for t, res in zip(doc_tasks, done):
            results[t["task_id"]] = res
            writer(
                {
                    "type": "progress",
                    "step": f"文档子任务：{t['question'][:20]}",
                    "status": "success" if res.ok else "error",
                }
            )

    doc_by_id = {
        t["task_id"]: results[t["task_id"]].payload.get("answer", "")
        for t in doc_tasks
        if results[t["task_id"]].ok
    }

    # SQL 子任务：把依赖的文档答案前段拼进子问句，让取值召回命中真实枚举
    for t in sql_tasks:
        step = f"数据子任务：{t['question'][:20]}"
        writer({"type": "progress", "step": step, "status": "running"})
        question = t["question"]
        bridges = [doc_by_id[d] for d in t.get("depends_on", []) if d in doc_by_id]
        if bridges:
            question = f"{question}\n参考口径（来自文档）：{('；'.join(bridges))[:600]}"
        res = await AGENTS["sql"].run(question, runtime)
        results[t["task_id"]] = res
        writer(
            {
                "type": "progress",
                "step": step,
                "status": "success" if res.ok else "error",
            }
        )

    return results


# 仅 SQL 成功：发 result 事件并写回 sql 字段；参数 res=SQL 子 Agent 结果，writer=SSE 写器
def _terminal_sql(res: AgentResult, writer):
    """SQL 单终端：result 事件（与 run_sql 行为一致）"""
    rows = res.payload.get("rows") or []
    sql = res.payload.get("sql") or ""
    writer({"type": "result", "data": rows})
    return {
        "route": "sql",
        "sql_rows": rows,
        "sql": sql,
        "messages": [AIMessage(content=str(rows))],
    }


# 仅文档成功：发 doc 事件并写回 doc_* 字段；参数 res=文档子 Agent 结果，writer=SSE 写器
def _terminal_doc(res: AgentResult, writer):
    """文档单终端：doc 事件（与 doc_query 行为一致）"""
    payload = res.payload
    data = payload.get("data") or {
        "answer": payload.get("answer", ""),
        "cited_pages": payload.get("cited_pages", []),
        "rounds": payload.get("rounds", 0),
        "route": payload.get("route") or {},
    }
    writer({"type": "doc", "data": data})
    return {
        "route": "doc",
        "doc_answer": payload.get("answer", ""),
        "doc_cited_pages": payload.get("cited_pages", []),
        "doc_rounds": payload.get("rounds", 0),
        "doc_route": payload.get("route") or {},
        "messages": [AIMessage(content=payload.get("answer", ""))],
    }


# SQL+文档都成功：组织三段式并流式产出 hybrid 终态，同时写回 sql/doc 字段供沉淀；参数 query=原问题，tasks/results=任务与结果，writer=SSE 写器
async def _terminal_hybrid(query: str, tasks: list[dict], results: dict, writer):
    """SQL+文档双终端：hybrid 三段式（与 synthesize 行为一致）"""

    data_sections: list[str] = []
    doc_sections: list[str] = []
    for t in tasks:
        res = results.get(t["task_id"])
        if res is None or not res.ok:
            continue
        if t["agent"] == "sql":
            data_sections.append(
                f"- 查询：{t['question'][:60]}\n{_rows_to_markdown(res.payload.get('rows') or [])}"
            )
        elif t["agent"] == "doc":
            pages = res.payload.get("cited_pages") or []
            doc_sections.append(
                f"- 来源：{t['question'][:60]}"
                + (f"（第{'、'.join(pages)}页）" if pages else "")
            )
            doc_sections.append(res.payload.get("answer", ""))

    content = await _compose(query, data_sections, doc_sections, writer)
    writer({"type": "hybrid", "data": {"content": content}})

    # 取首份成功的 sql/doc 落 state，兼容 QueryService 的知识沉淀与轨迹
    updates: dict = {
        "route": "hybrid",
        "hybrid_content": content,
        "messages": [AIMessage(content=content)],
    }
    first_sql = next(
        (
            results[t["task_id"]]
            for t in tasks
            if t["agent"] == "sql"
            and results.get(t["task_id"])
            and results[t["task_id"]].ok
        ),
        None,
    )
    first_doc = next(
        (
            results[t["task_id"]]
            for t in tasks
            if t["agent"] == "doc"
            and results.get(t["task_id"])
            and results[t["task_id"]].ok
        ),
        None,
    )
    if first_sql:
        updates["sql_rows"] = first_sql.payload.get("rows") or []
        updates["sql"] = first_sql.payload.get("sql") or ""
    if first_doc:
        updates["doc_answer"] = first_doc.payload.get("answer", "")
        updates["doc_cited_pages"] = first_doc.payload.get("cited_pages", [])
        updates["doc_rounds"] = first_doc.payload.get("rounds", 0)
        updates["doc_route"] = first_doc.payload.get("route") or {}
    return updates


# 编排主逻辑：知识门→计划→分发→终端事件；参数 state=含用户 query，runtime=运行上下文与 stream_writer
async def orchestrator(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """主 Agent：选择并组合调用各子 Agent，产出终端事件"""

    writer = runtime.stream_writer
    query = state["query"]
    history = render_history(
        state.get("messages", []), memory=state.get("memory_text", "")
    )

    # 1. 知识门：命中沉淀问题即短路复用（命中计数旁路累加）
    knowledge = await AGENTS["knowledge"].run(query, runtime)
    if knowledge.ok:
        answer = knowledge.payload.get("answer", "")
        item_id = knowledge.payload.get("item_id")
        if item_id:
            try:
                await runtime.context["knowledge_mysql_repository"].increment_hit_count(
                    item_id
                )
            except Exception as e:
                logger.warning(f"知识命中计数失败 item_id={item_id}: {e}")
        writer({"type": "message", "content": answer})
        return {
            "route": "knowledge",
            "knowledge_answer": answer,
            "knowledge_item_id": item_id,
            "knowledge_score": knowledge.payload.get("score"),
            "messages": [AIMessage(content=answer)],
        }

    # 2. 静态快判免 planner（降本、质量不变）：命中特征词时直接确定子 Agent 组合，
    #    只有模棱两可（None）才调主模型做计划拆解。规划阶段不发 progress，
    #    闲聊路径保持纯文本，业务路径进度由各子任务 progress 事件体现。
    fast = _fast_route(query)
    if fast == ROUTE_CHAT:
        # 纯闲聊：跳过 planner，直接 chat 子 Agent
        chat = await AGENTS["chat"].run(query, runtime, history=history)
        text = chat.payload.get("text") or _FALLBACK_MESSAGE
        writer({"type": "message", "content": text})
        return {"route": "chat", "messages": [AIMessage(content=text)]}
    if fast in (ROUTE_SQL, ROUTE_DOC):
        tasks = [{"task_id": "t1", "agent": fast, "question": query, "depends_on": []}]
    elif fast == ROUTE_HYBRID:
        # 跨源特征词命中：sql+doc 各跑整句（与 v1 等价，保留子任务级失败隔离）
        tasks = [
            {"task_id": "t1", "agent": "sql", "question": query, "depends_on": []},
            {"task_id": "t2", "agent": "doc", "question": query, "depends_on": []},
        ]
    else:
        tasks = await _plan(query, history)
        if not tasks:
            tasks = [
                {"task_id": "t1", "agent": "sql", "question": query, "depends_on": []},
                {"task_id": "t2", "agent": "doc", "question": query, "depends_on": []},
            ]
    # 存在业务子任务时丢弃 chat，避免闲聊文本混入业务综合
    business = [t for t in tasks if t["agent"] in _BUSINESS_AGENTS]
    if business:
        tasks = business

    # 3. 纯闲聊终端（planner 输出 chat 时）
    if tasks and all(t["agent"] == "chat" for t in tasks):
        chat = await AGENTS["chat"].run(tasks[0]["question"], runtime, history=history)
        text = chat.payload.get("text") or _FALLBACK_MESSAGE
        writer({"type": "message", "content": text})
        return {"route": "chat", "messages": [AIMessage(content=text)]}

    # 4. 分发执行
    results = await _execute(tasks, runtime, writer)

    has_sql = any(
        results.get(t["task_id"]) and results[t["task_id"]].ok
        for t in tasks
        if t["agent"] == "sql"
    )
    has_doc = any(
        results.get(t["task_id"]) and results[t["task_id"]].ok
        for t in tasks
        if t["agent"] == "doc"
    )

    # 5. 终端事件映射
    if has_sql and has_doc:
        return await _terminal_hybrid(query, tasks, results, writer)
    if has_sql:
        res = next(
            results[t["task_id"]]
            for t in tasks
            if t["agent"] == "sql"
            and results.get(t["task_id"])
            and results[t["task_id"]].ok
        )
        return _terminal_sql(res, writer)
    if has_doc:
        res = next(
            results[t["task_id"]]
            for t in tasks
            if t["agent"] == "doc"
            and results.get(t["task_id"])
            and results[t["task_id"]].ok
        )
        return _terminal_doc(res, writer)

    # 全部未命中
    logger.info(f"编排全部未命中：query={query[:40]}")
    writer({"type": "message", "content": _FALLBACK_MESSAGE})
    return {"route": "hybrid", "messages": [AIMessage(content=_FALLBACK_MESSAGE)]}
