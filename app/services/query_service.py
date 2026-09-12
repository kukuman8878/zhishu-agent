"""
问数查询服务

负责把 API 层传入的自然语言问题转换成一次 LangGraph 工作流执行：
创建初始 State、组装 Runtime Context、消费 graph.astream 的流式输出，
并统一包装成 SSE 文本返回给路由层。

astream 同时开启 custom + values 两种流模式：
  custom  → 各节点 writer(...) 写出的进度/结果事件，逐条包装成 SSE 返回；
  values  → 每次节点完成后的全量状态，取最后一次作为图执行终态，
            供流结束后把有价值的问答结论沉淀进知识库（LLMWiki 式）。
"""

import json
import time
import uuid

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.agent import graph as graph_module
from app.agent.context import DataAgentContext
from app.agent.memory.manager import MemoryManager
from app.agent.state import DataAgentState
from app.clients.doc_engine_client_manager import DocEngineClient
from app.clients.rerank_client_manager import RerankClient
from app.conf.app_config import app_config
from app.core.log import logger
from app.core.metrics import log_metrics_summary
from app.entities.query_trace import QueryTrace
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.eval_mysql_repository import EvalMySQLRepository
from app.repositories.mysql.meta.knowledge_mysql_repository import (
    KnowledgeMySQLRepository,
)
from app.repositories.mysql.meta.memory_mysql_repository import MemoryMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.mysql.meta.trace_mysql_repository import TraceMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.knowledge_qdrant_repository import (
    KnowledgeQdrantRepository,
)
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.services.evaluation_service import EvaluationService
from app.services.knowledge_service import KnowledgeService

# 沉淀内容判定常量：只有这三类业务路由的结论才值得沉淀，闲聊/复用不再入库
_DEPOSITABLE_ROUTES = {"sql", "doc", "hybrid"}


class QueryService:
    """封装一次问数查询所需的业务编排逻辑"""

    # 构造函数：保存 Meta/DW 仓储、Embedding/Qdrant/ES 客户端、知识沉淀服务与文档问答客户端等外部依赖，供每次查询组装运行上下文；参数为各依赖注入对象
    def __init__(
        self,
        meta_mysql_repository: MetaMySQLRepository,
        embedding_client: HuggingFaceEndpointEmbeddings,
        dw_mysql_repository: DWMySQLRepository,
        column_qdrant_repository: ColumnQdrantRepository,
        metric_qdrant_repository: MetricQdrantRepository,
        value_es_repository: ValueESRepository,
        rag_client: DocEngineClient,
        rerank_client: RerankClient,
        knowledge_mysql_repository: KnowledgeMySQLRepository,
        knowledge_qdrant_repository: KnowledgeQdrantRepository,
        trace_mysql_repository: TraceMySQLRepository,
        eval_mysql_repository: EvalMySQLRepository,
        memory_mysql_repository: MemoryMySQLRepository,
    ):
        # MySQL 仓储分别负责元数据补全和真实数仓环境信息读取
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository

        # 召回链路依赖的向量检索、Embedding 和全文检索能力由依赖层注入
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_es_repository = value_es_repository

        # 文档问答客户端：doc/hybrid 路由下把问题交给进程内文档问答引擎
        self.rag_client = rag_client

        # 跨编码器语义重排客户端：三路召回去重后对候选精排截取 top_k
        self.rerank_client = rerank_client

        # 知识沉淀服务：问答结束后把有价值结论写入知识库，供后续相似问题复用
        self.knowledge_service = KnowledgeService(
            knowledge_mysql_repository=knowledge_mysql_repository,
            knowledge_qdrant_repository=knowledge_qdrant_repository,
            embedding_client=embedding_client,
        )

        # 轨迹仓储：每次问答结束后落一条结构化执行轨迹，供复盘与评估
        self.trace_mysql_repository = trace_mysql_repository

        # 结果评估服务：问答结束后对终端答案做在线评估并落库（eval.enabled 开关）
        self.evaluation_service = EvaluationService(eval_mysql_repository)

        # 记忆管理器：加载/更新摘要记忆与用户记忆（工作记忆由 checkpointer 负责）
        self.memory_manager = MemoryManager(memory_mysql_repository)

    # 把 SQL 行数据归一化为 markdown 表格文本，作为 sql 路由的沉淀答案；参数 rows=SQL 执行返回的行数据(dict 列表或单个)
    def _rows_to_markdown(self, rows) -> str:
        """把 SQL 行数据归一化为 markdown 表格文本，供知识沉淀复用"""
        if not rows:
            return ""
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

    # 从图执行终态提取本次问答的沉淀答案，返回 (answer, sql) 或 None；参数 final_state=图执行终态
    def _extract_deposit_content(self, final_state) -> tuple[str, str | None] | None:
        """按路由从终态提取沉淀内容：sql→结果行markdown+SQL，doc→文档答案，hybrid→综合文本"""
        route = final_state.get("route")
        if route == "sql":
            rows = final_state.get("sql_rows") or []
            answer = self._rows_to_markdown(rows)
            if not answer:
                return None
            return answer, final_state.get("sql")
        if route == "doc":
            answer = final_state.get("doc_answer") or ""
            return (answer, None) if answer else None
        if route == "hybrid":
            answer = final_state.get("hybrid_content") or ""
            return (answer, None) if answer else None
        return None

    # 流结束后把有价值问答沉淀进知识库（失败不影响已返回的 SSE 流）；参数 final_state=图执行终态
    async def _deposit_knowledge(self, final_state):
        """图执行结束后沉淀知识：仅 sql/doc/hybrid 路由且结论非空时入库"""
        if not final_state or not app_config.knowledge.enabled:
            return
        if not app_config.knowledge.deposit_enabled:
            return
        route = final_state.get("route")
        # 知识复用命中和闲聊都不再沉淀，避免知识库自我膨胀
        if route not in _DEPOSITABLE_ROUTES:
            return
        content = self._extract_deposit_content(final_state)
        if content is None:
            return
        answer, sql = content
        try:
            await self.knowledge_service.deposit(
                question=final_state["query"],
                route=route,
                answer=answer,
                sql=sql,
            )
        except Exception as e:
            # 沉淀失败只记日志：不影响本次问答已经产出的结果
            logger.warning(f"知识沉淀失败（不影响本次问答）：{e}")

    # 把一次问答的执行轨迹落库（失败只记日志，不影响问答）；参数 query=用户问题，session_id=会话标识，final_state=图终态(可空)，error=整体异常(可空)，elapsed_ms=整体耗时
    async def _save_trace(
        self,
        query: str,
        session_id: str,
        final_state,
        error: str | None,
        elapsed_ms: int,
    ):
        """问答结束后把结构化执行轨迹写入 query_trace 表"""
        if not app_config.trace.enabled:
            return
        route = (final_state or {}).get("route") or "unknown"
        sql = (final_state or {}).get("sql")
        rows = (final_state or {}).get("sql_rows")
        # 整体异常优先；无整体异常时回看 SQL 自愈环最终是否仍带执行错误
        error_text = error or (final_state or {}).get("run_error")
        trace = QueryTrace(
            id=uuid.uuid4().hex,
            session_id=session_id,
            query=query,
            route=route,
            status="error" if error_text else "success",
            knowledge_item_id=(final_state or {}).get("knowledge_item_id"),
            sql=sql,
            sql_attempts=int((final_state or {}).get("sql_attempts", 0) or 0),
            row_count=len(rows) if isinstance(rows, list) else None,
            duration_ms=elapsed_ms,
            error=error_text[:500] if error_text else None,
        )
        try:
            self.trace_mysql_repository.save(trace)
            await self.trace_mysql_repository.session.commit()
        except Exception as e:
            logger.warning(f"查询轨迹落库失败（不影响本次问答）：{e}")

    # 执行一次问数工作流：创建初始 State/Context 并消费 graph.astream 输出，yield 逐条 SSE 文本(异常也包装为 error 事件)；参数 query=用户自然语言问题，session_id=会话标识(多轮追问共享历史)
    async def query(
        self,
        query: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ):
        """执行一次问数工作流，并逐段产出 SSE 消息"""

        # 会话键：前端传 session_id 时共享历史；未传则用随机 thread_id 隔离（单轮语义不变）
        thread_id = session_id or f"anon-{uuid.uuid4().hex}"
        # 用户记忆作用域：优先用稳定 user_id，缺省退化到 session_id（至少会话内生效）
        effective_user_id = user_id or session_id
        # 加载长期记忆（会话摘要 + 用户偏好），渲染成提示词片段注入本轮
        memory_context = await self.memory_manager.load_context(
            thread_id, effective_user_id
        )

        # State 只放会被图节点读写和合并的业务数据，外部工具对象不塞进 State。
        # 多轮会话下 checkpointer 会保留上一轮的普通字段，因此除 messages 外全部显式
        # 重置为 None，避免旧轮 sql/sql_rows/route 等残留影响本轮路由、轨迹与综合栅栏
        state = DataAgentState(
            query=query,
            route=None,
            keywords=None,
            retrieved_column_infos=None,
            retrieved_metric_infos=None,
            retrieved_value_infos=None,
            table_infos=None,
            metric_infos=None,
            date_info=None,
            db_info=None,
            sql=None,
            error=None,
            run_error=None,
            sql_attempts=None,
            verify_note=None,
            sql_rows=None,
            doc_answer=None,
            doc_cited_pages=None,
            doc_rounds=None,
            doc_route=None,
            hybrid_content=None,
            knowledge_answer=None,
            knowledge_item_id=None,
            knowledge_score=None,
            # 长期记忆片段（摘要+用户偏好），供各节点渲染进提示词
            memory_text=memory_context.render(),
        )
        # Context 保存本次图执行需要复用的外部依赖，节点通过 runtime.context 读取
        context = DataAgentContext(
            column_qdrant_repository=self.column_qdrant_repository,
            embedding_client=self.embedding_client,
            metric_qdrant_repository=self.metric_qdrant_repository,
            value_es_repository=self.value_es_repository,
            meta_mysql_repository=self.meta_mysql_repository,
            dw_mysql_repository=self.dw_mysql_repository,
            rag_client=self.rag_client,
            rerank_client=self.rerank_client,
            knowledge_mysql_repository=self.knowledge_service.knowledge_mysql_repository,
            knowledge_qdrant_repository=self.knowledge_service.knowledge_qdrant_repository,
        )
        config = {"configurable": {"thread_id": thread_id}}
        start = time.monotonic()
        try:
            # stream_mode=["custom","values"]：custom 收进度/结果事件，values 捕获图执行终态
            final_state = None
            async for chunk in graph_module.graph.astream(
                input=state,
                context=context,
                config=config,
                stream_mode=["custom", "values"],
            ):
                mode, data = chunk
                if mode == "custom":
                    # SSE 要求每条消息以 data: 开头，并以两个换行符结束
                    # ensure_ascii=False 保留中文进度文案，default=str 兜底处理日期等非 JSON 类型
                    yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                else:
                    # values 模式的最后一次数据即图执行终态
                    final_state = data
            # 图执行收尾：打印本次请求的 SQL 链路节点耗时摘要并清空统计
            log_metrics_summary()
            # 知识沉淀：SSE 已全部返回后才入库，避免沉淀耗时拖慢前端展示
            await self._deposit_knowledge(final_state)
            # 轨迹落库：沉淀完成后记录本次执行的结构化快照
            await self._save_trace(
                query,
                thread_id,
                final_state,
                None,
                int((time.monotonic() - start) * 1000),
            )
            # 记忆更新：滚动摘要 + 抽取用户偏好（工作记忆由 checkpointer 自动落盘）
            await self.memory_manager.update_after_turn(
                thread_id,
                effective_user_id,
                (final_state or {}).get("messages"),
                query,
            )
            # 结果评估：在线打分并落库；低分且开启 emit_note 时补发一条 note（不覆盖答案）
            eval_result = await self.evaluation_service.evaluate(final_state, thread_id)
            if (
                eval_result is not None
                and not eval_result.passed
                and app_config.eval.emit_note
            ):
                note = {
                    "type": "note",
                    "content": (
                        f"结果质量自动评估得分较低（{eval_result.score:.2f}），"
                        "建议核对口径后再使用。"
                    ),
                }
                yield f"data: {json.dumps(note, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:
            # 流式接口已经开始返回后不能再改 HTTP 状态码，因此把异常也包装成一条 SSE 消息
            error = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error, ensure_ascii=False, default=str)}\n\n"
            # 失败也要留轨迹：错误状态 + 异常信息
            await self._save_trace(
                query, thread_id, None, str(e), int((time.monotonic() - start) * 1000)
            )
