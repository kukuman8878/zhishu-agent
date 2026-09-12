"""
字段/指标/取值三路召回节点

把三路"关键词召回"节点聚到一个文件里。三者出来的都是 LLM 扩展关键词→检索→去重，
再交给 cross-encoder 语义精排截取 top_k 的同一套流程，只是数据源各有不同：
  recall_column  字段召回，走 Embedding→Qdrant 向量库（ColumnInfo）
  recall_metric  指标召回，走 Embedding→Qdrant 向量库（MetricInfo）
  recall_value   取值召回，走 Elasticsearch 全文索引（ValueInfo）

三路召回在 graph 中并行执行，互不依赖，靠 keywords/query 统一入口。
"""

import asyncio

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import aux_llm
from app.agent.llm_utils import retry_async
from app.agent.rerank import rerank_candidates
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo
from app.prompt.prompt_loader import load_prompt


# 字段召回节点：LLM 扩展关键词→批量 Embedding→并行 Qdrant 检索→按字段 id 去重后写回 retrieved_column_infos；参数 state=含 keywords/query，runtime=携带字段仓储与 Embedding 客户端
async def recall_column(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """召回和用户问题语义相关的字段元数据"""

    writer = runtime.stream_writer
    step = "召回字段信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        keywords = state["keywords"]
        query = state["query"]
        column_qdrant_repository = runtime.context["column_qdrant_repository"]
        embedding_client = runtime.context["embedding_client"]

        # 用 LLM 把用户问法扩展成"字段语义"列表
        prompt = PromptTemplate(
            template=load_prompt("extend_keywords_for_column_recall"),
            input_variables=["query"],
        )
        output_parser = JsonOutputParser()
        # 辅助任务下放辅助模型（aux_llm），失败退回原关键词（fail-open）
        chain = prompt | aux_llm | output_parser

        try:
            result = await retry_async(
                chain.ainvoke, {"query": query}, node_name="recall_column"
            )
        except Exception as e:
            logger.warning(f"字段关键词扩展失败，回退原关键词：{e}")
            result = []
        if not isinstance(result, list):
            result = []

        # 去重后的关键词列表
        all_keywords = list(set(keywords + [str(k) for k in result]))

        # 批量 Embedding：一次性向量化所有关键词，避免逐条串行调用
        embeddings = await embedding_client.aembed_documents(all_keywords)

        # 并行 Qdrant 检索：每个关键词的向量同时发起检索
        async def _search_one(idx: int, kw: str, emb: list[float]):
            return await column_qdrant_repository.search(
                emb, limit=app_config.rerank.recall_limit
            )

        search_tasks = [
            _search_one(i, kw, emb)
            for i, (kw, emb) in enumerate(zip(all_keywords, embeddings))
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # 按字段 id 去重
        column_info_map: dict[str, ColumnInfo] = {}
        for result_item in search_results:
            if isinstance(result_item, Exception):
                logger.warning(f"字段召回检索异常，跳过: {result_item}")
                continue
            for column_info, _score in result_item:
                if column_info.id not in column_info_map:
                    column_info_map[column_info.id] = column_info

        # 去重后的字段候选，再交给 cross-encoder 做一次语义精排
        deduped_column_infos: list[ColumnInfo] = list(column_info_map.values())
        retrieved_column_infos = await rerank_candidates(
            query=query,
            candidates=deduped_column_infos,
            render=lambda c: f"{c.name}（别名：{'、'.join(c.alias)}）{c.description}",
            runtime=runtime,
        )

        writer({"type": "progress", "step": step, "status": "success"})
        return {"retrieved_column_infos": retrieved_column_infos}
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


# 指标召回节点：LLM 扩展指标词→批量 Embedding→并行 Qdrant 检索→按指标 id 去重后写回 retrieved_metric_infos；参数 state=含 query/keywords，runtime=携带指标仓储与 Embedding 客户端
async def recall_metric(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """召回和用户问题语义相关的业务指标"""

    writer = runtime.stream_writer
    step = "召回指标信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]
        keywords = state["keywords"]
        embedding_client = runtime.context["embedding_client"]
        metric_qdrant_repository = runtime.context["metric_qdrant_repository"]

        # 用 LLM 把用户问法扩展成"指标概念"列表
        prompt = PromptTemplate(
            template=load_prompt("extend_keywords_for_metric_recall"),
            input_variables=["query"],
        )
        output_parser = JsonOutputParser()
        chain = prompt | aux_llm | output_parser

        try:
            result = await retry_async(
                chain.ainvoke, {"query": query}, node_name="recall_metric"
            )
        except Exception as e:
            logger.warning(f"指标关键词扩展失败，回退原关键词：{e}")
            result = []
        if not isinstance(result, list):
            result = []

        all_keywords = list(set(keywords + [str(k) for k in result]))

        # 批量 Embedding
        embeddings = await embedding_client.aembed_documents(all_keywords)

        # 并行 Qdrant 检索
        async def _search_one(idx: int, kw: str, emb: list[float]):
            return await metric_qdrant_repository.search(
                emb, limit=app_config.rerank.recall_limit
            )

        search_tasks = [
            _search_one(i, kw, emb)
            for i, (kw, emb) in enumerate(zip(all_keywords, embeddings))
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        metric_info_map: dict[str, MetricInfo] = {}
        for result_item in search_results:
            if isinstance(result_item, Exception):
                logger.warning(f"指标召回检索异常，跳过: {result_item}")
                continue
            for metric_info, _score in result_item:
                if metric_info.id not in metric_info_map:
                    metric_info_map[metric_info.id] = metric_info

        deduped_metric_infos: list[MetricInfo] = list(metric_info_map.values())
        retrieved_metric_infos = await rerank_candidates(
            query=query,
            candidates=deduped_metric_infos,
            render=lambda m: f"{m.name}（别名：{'、'.join(m.alias)}）{m.description}",
            runtime=runtime,
        )

        logger.info(f"检索到指标信息：{list(metric_info_map.keys())}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"retrieved_metric_infos": retrieved_metric_infos}
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


# 字段取值召回节点：LLM 扩展取值词→并行 ES 全文检索→按取值 id 去重后写回 retrieved_value_infos；参数 state=含 query/keywords，runtime=提供 ES 取值仓储
async def recall_value(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """召回和用户问题相关的字段取值"""

    writer = runtime.stream_writer
    step = "召回字段取值"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]
        keywords = state["keywords"]
        value_es_repository = runtime.context["value_es_repository"]

        # 用 LLM 把用户问法扩展成"可能出现在字段值里的词"
        prompt = PromptTemplate(
            template=load_prompt("extend_keywords_for_value_recall"),
            input_variables=["query"],
        )
        output_parser = JsonOutputParser()
        chain = prompt | aux_llm | output_parser

        try:
            result = await retry_async(
                chain.ainvoke, {"query": query}, node_name="recall_value"
            )
        except Exception as e:
            logger.warning(f"取值关键词扩展失败，回退原关键词：{e}")
            result = []
        if not isinstance(result, list):
            result = []

        all_keywords = list(set(keywords + [str(k) for k in result]))

        # 并行 ES 检索：每个关键词同时发起全文检索
        async def _search_one(kw: str):
            return await value_es_repository.search(
                kw, limit=app_config.rerank.recall_limit
            )

        search_tasks = [_search_one(kw) for kw in all_keywords]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        value_infos_map: dict[str, ValueInfo] = {}
        for result_item in search_results:
            if isinstance(result_item, Exception):
                logger.warning(f"取值召回检索异常，跳过: {result_item}")
                continue
            for current_value_info, _score in result_item:
                if current_value_info.id not in value_infos_map:
                    value_infos_map[current_value_info.id] = current_value_info

        deduped_value_infos: list[ValueInfo] = list(value_infos_map.values())
        retrieved_value_infos = await rerank_candidates(
            query=query,
            candidates=deduped_value_infos,
            render=lambda v: f"{v.column_id}：{v.value}",
            runtime=runtime,
        )

        logger.info(f"检索到字段取值：{list(value_infos_map.keys())}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"retrieved_value_infos": retrieved_value_infos}
    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
