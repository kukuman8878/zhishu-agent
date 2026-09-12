"""
意图路由节点（Agent 入口闸门）

用途：在进入任何业务链路之前，先判断用户问题属于哪一类能力，把流量分成五路：
  - knowledge → 命中知识库沉淀问题时短路复用存量答案；
  - sql     → 走数据分析主链路（召回 → SQL 生成 → 执行），并向前端展示 LangGraph 流程图；
  - doc     → 走文档问答链路（进程内文档引擎，检索 PDF/图/表内容回答）；
  - hybrid  → 同时需要数仓数据与文档内容（并行跑 sql + doc，再综合）；
  - chat    → 短路到 answer_general 节点，用轻量 chat_llm 直接回答。

设计要点：
  1. 五级路由，兼顾“快”与“稳”：
     ⓪ 知识沉淀复用：先查 LLMWiki 式知识库，命中相似沉淀问题直接复用答案；
     ① 数据/文档特征词命中 → 直接判 sql/doc（省一次模型调用）；
     ② 跨域特征词 + 数据或文档词 → 判 hybrid；
     ③ 闲聊词命中 → 判 chat；
     ④ 以上都未命中（模棱两可）→ 才调用 chat_llm 做一次语义判别。
  2. 采用 fail-open 原则：模型不可用或返回异常时，默认按 hybrid 处理，
     宁可两条链路都跑，也不要把真查询误拦在门外。
  3. 本节点是内部闸门，不向前端发送 progress 事件：前端流程图只展示
     数据分析链路本身，其余路径保持干净的纯文本/结构化回复。
"""

import json
import re

from langchain_core.messages import HumanMessage
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.history import render_history
from app.agent.llm import chat_llm
from app.agent.llm_utils import retry_async
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.core.text_utils import (
    has_future_year,
    same_metric_domain,
    same_numbers,
    same_region_scope,
)
from app.prompt.prompt_loader import load_prompt

# 路由枚举值（与 state.route、classify_route.prompt 输出、前端 mode 一一对应）
ROUTE_SQL = "sql"
ROUTE_DOC = "doc"
ROUTE_HYBRID = "hybrid"
ROUTE_CHAT = "chat"
# 知识沉淀复用路由：问题命中沉淀知识库时短路到 answer_knowledge，直接复用存量答案
ROUTE_KNOWLEDGE = "knowledge"
VALID_ROUTES = {ROUTE_SQL, ROUTE_DOC, ROUTE_HYBRID, ROUTE_CHAT}

# 判别异常时的 fail-open 默认路由：宁可两条链路都跑，也不要误拦真查询
DEFAULT_ROUTE = ROUTE_HYBRID

# 数据分析特征词：命中即“用户明确想查数”，直接放行，省一次模型调用
SQL_HINTS = [
    "销售",
    "销量",
    "销售额",
    "订单",
    "GMV",
    "AOV",
    "金额",
    "成交",
    "会员",
    "客户",
    "商品",
    "品类",
    "品牌",
    "利润",
    "营收",
    "收入",
    "地区",
    "大区",
    "省份",
    "城市",
    "同比",
    "环比",
    "占比",
    "排行",
    "统计",
    "多少",
    "哪个卖",
    "哪些卖",
    "卖得",
    "买了",
    "Top",
    "top",
    "排名",
    "增长率",
    "客单",
    "退款",
    "库存",
]

# 文档问答特征词：命中即“用户想从 PDF/文档/图/表里找内容”，直接走 doc
DOC_HINTS = [
    "文档",
    "手册",
    "说明书",
    "白皮书",
    "报告",
    "PDF",
    "pdf",
    "技术规格",
    "规格",
    "参数",
    "安装步骤",
    "操作步骤",
    "配置",
    "怎么配置",
    "怎么操作",
    "如何配置",
    "如何操作",
    "原理",
    "架构图",
    "流程图",
    "图示",
    "图中",
    "图表",
    "表格里",
    "章节",
    "条款",
    "资料",
    "文件里",
    "页面",
    "第几页",
    "引用了",
    "原文",
    "出处",
]

# 跨域特征词：单独出现不足以判 hybrid，需与数据/文档词同时命中才算
HYBRID_HINTS = ["结合", "对照", "对照着"]

# 闲聊/问候特征词：命中即“不涉及业务查询”，直接短路到通用问答，省一次模型调用
CHAT_HINTS = [
    "你好",
    "您好",
    "在吗",
    "谢谢",
    "感谢",
    "再见",
    "拜拜",
    "你是谁",
    "你会什么",
    "能做什么",
    "会干什么",
    "介绍一下自己",
    "自我介绍",
    "你叫什么",
    "叫什么名字",
    "会写代码",
    "会编程",
    "翻译",
    "写首诗",
    "讲个笑话",
    "天气",
    "天气预报",
    "气温",
    "下雨",
    "新闻",
    "热搜",
    "搜索",
    "搜一下",
    "查一下",
    "汇率",
    "股价",
    "航班",
    "聊天",
    "你是谁家的",
]

# 需要忽略的标点/空白：用于把问题里的标点去掉后再做关键词匹配
_OFF_TOPIC_SYMBOLS = {"。", "？", "?", "！", "!", "，", ",", " ", "　"}


# 本地关键词零成本预判路由，命中返回 sql/doc/hybrid/chat，无法确定返回 None；参数 query=用户原始问题文本
def _fast_route(query: str) -> str | None:
    """零成本本地预判路由

    返回值语义：
      ROUTE_SQL / ROUTE_DOC / ROUTE_CHAT / ROUTE_HYBRID → 本地规则已确定去向
      None → 本地规则无法确定，需要交给 chat_llm 做语义判别
    """

    text = query.strip().lower()
    if not text:
        # 空输入视为闲聊，避免进入业务链路
        return ROUTE_CHAT

    # 先做数据/文档/跨域判定，再做闲聊判定：避免“统计一下销售额，谢谢”
    # 这类混合输入因含闲聊词（谢谢）被误判成闲聊。
    has_sql = any(hint in text for hint in SQL_HINTS)
    has_doc = any(hint in text for hint in DOC_HINTS)
    has_hyb = any(hint in text for hint in HYBRID_HINTS)

    # 跨域判定优先级最高：数据与文档词共存，或跨域词与任一业务词共存
    if has_hyb and (has_sql or has_doc):
        return ROUTE_HYBRID
    if has_sql and has_doc:
        return ROUTE_HYBRID
    if has_sql:
        return ROUTE_SQL
    if has_doc:
        return ROUTE_DOC

    # 去掉标点后再匹配闲聊词，避免“统计一下销售额，谢谢”被误判成闲聊
    compact = "".join(ch for ch in text if ch not in _OFF_TOPIC_SYMBOLS)
    if compact and any(hint in compact for hint in CHAT_HINTS):
        return ROUTE_CHAT
    # 无法确定，交还调用方走模型判别
    return None


# 从模型返回的原始文本中稳健解析 route 字段，解析失败默认 fail-open 为 hybrid；参数 raw=模型输出的 JSON 原文
def _parse_route(raw: str) -> str:
    """从模型输出中稳健解析 route 字段，默认 fail-open 为 DEFAULT_ROUTE"""

    # 兼容模型在 JSON 外包了代码块或前后有杂讯的情况
    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not match:
        logger.warning(
            f"意图路由输出无法解析，默认 fail-open={DEFAULT_ROUTE}: {raw[:200]}"
        )
        return DEFAULT_ROUTE
    try:
        payload = json.loads(match.group(0))
        route = str(payload.get("route", "")).strip().lower()
        if route in VALID_ROUTES:
            return route
        logger.warning(f"意图路由返回未知值 route={route!r}，默认 {DEFAULT_ROUTE}")
        return DEFAULT_ROUTE
    except Exception as e:
        logger.warning(f"意图路由 JSON 解析失败，默认 {DEFAULT_ROUTE}: {e}")
        return DEFAULT_ROUTE


# 知识召回：问题向量化后在知识库检索相似沉淀问题，返回命中的 (KnowledgeItem, score) 或 None；
# 参数 state=含用户 query 的图状态，runtime=携带知识向量仓储与 Embedding 客户端
async def _recall_knowledge(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """知识库召回复用：命中沉淀问题时直接复用答案，省去全链路"""

    try:
        query = state["query"]
        knowledge_qdrant_repository = runtime.context["knowledge_qdrant_repository"]
        embedding_client = runtime.context["embedding_client"]

        # 知识集合由沉淀服务按需创建；首问时集合不存在，保证检索不抛异常
        await knowledge_qdrant_repository.ensure_collection()
        vector = await embedding_client.aembed_query(query)
        # 多取几个候选：数字口径守卫可能过滤掉最高分的那条（如年份/季度不同）
        results = await knowledge_qdrant_repository.search(
            vector,
            score_threshold=app_config.knowledge.recall_score_threshold,
            limit=max(app_config.knowledge.recall_limit, 3),
        )
        for item, score in results:
            # 口径守卫：① 数字不一致（年份/季度/TopN 变化）不复用（bge 对数字不敏感）；
            # ② 指标词冲突（"会员数量" vs "订单数量"）不复用；
            # ③ 地区不一致（"华中" vs "华南"）不复用（bge 对地区词不敏感）
            if (
                not same_numbers(query, item.question)
                or not same_metric_domain(query, item.question)
                or not same_region_scope(query, item.question)
            ):
                logger.info(
                    f"知识复用跳过（口径不一致）：query={query[:30]} hit={item.question[:30]} score={score:.3f}"
                )
                continue
            logger.info(
                f"知识库命中复用：item_id={item.id} score={score:.3f} query={query[:40]}"
            )
            return item, score
        return None
    except Exception as e:
        # fail-open：知识召回异常不阻断正常路由，退化为重新跑业务链路
        logger.warning(f"知识库召回失败，退化为正常路由：{e}")
        return None


# 意图路由节点主逻辑：先知识库召回复用，未命中再本地特征词预判，仍未命中再用 chat_llm(带历史)语义判别，产出 route；参数 state=含用户 query 的图状态，runtime=携带上下文/流式写器的运行时
async def classify_route(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """意图路由节点主逻辑：产出 route 供图的条件边做路由"""

    query = state["query"]
    # 本轮用户输入写入会话历史（add_messages 归并 + checkpointer 持久化）
    user_message = [HumanMessage(content=query)]

    # 多智能体编排模式（orchestrator.enabled）：入口不再做规则/模型路由，
    # 统一交给 orchestrator 节点（主 Agent）自行做知识复用判定 + 计划分发。
    # 默认关闭时完全走原四级路由，零回归。
    if app_config.orchestrator.enabled:
        return {"route": "orchestrator", "messages": user_message}

    # 第 0 步：知识沉淀复用（LLMWiki 式）。相似问题命中沉淀知识时短路到
    # answer_knowledge 直接复用存量答案，不再消耗召回/SQL 全链路；
    # 问句带未来年份时跳过复用，避免历史答案的时间口径错配
    if app_config.knowledge.enabled and not has_future_year(query):
        hit = await _recall_knowledge(state, runtime)
        if hit is not None:
            item, score = hit
            return {
                "route": ROUTE_KNOWLEDGE,
                "knowledge_answer": item.answer,
                "knowledge_item_id": item.id,
                "knowledge_score": score,
                "messages": user_message,
            }

    # 第 1 步：本地规则能确定去向就直接返回，避免高频入口每次都消耗模型调用
    fast = _fast_route(query)
    if fast is not None:
        logger.info(f"意图规则命中：route={fast} query={query}")
        return {"route": fast, "messages": user_message}

    # 第 2 步：规则无法确定时，用快速廉价的 chat_llm 做一次语义判别；
    # 注入会话历史，让"那按月呢"这类省略式追问能结合上下文还原真实意图
    try:
        history = render_history(
            state.get("messages", []), memory=state.get("memory_text", "")
        )
        prompt = PromptTemplate(
            template=load_prompt("classify_route"),
            input_variables=["query", "history"],
        )
        # 意图判别属于高频入口，使用快速廉价的 chat_llm，而不是贵且慢的主模型
        chain = prompt | chat_llm
        raw = await retry_async(
            chain.ainvoke,
            {"query": query, "history": history},
            node_name="classify_route",
        )

        route = _parse_route(raw.content)
        logger.info(f"意图路由结果：route={route} query={query}")
        return {"route": route, "messages": user_message}

    except Exception as e:
        # fail-open：判别环节异常（如模型超时）不应阻断主流程，默认走 hybrid
        logger.error(f"意图路由失败，默认 fail-open={DEFAULT_ROUTE}: {e}")
        return {"route": DEFAULT_ROUTE, "messages": user_message}
