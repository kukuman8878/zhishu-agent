"""
知识沉淀服务（LLMWiki 式）

负责组织问答知识沉淀的业务流程：
  - recall  新问题先查知识库，命中相似沉淀问题时直接复用答案（省去全链路）
  - hit     复用命中后累加该条目的命中次数
  - deposit 问答结束后把有价值结论（sql/doc/hybrid 路由）沉淀入库：
            问题向量写入 Qdrant，条目本体写入 Meta MySQL；
            与已有问题高度相似时合并刷新，避免知识库无限膨胀

沉淀内容按路由区分：
  sql     → 结果行 markdown + 最终 SQL（供后续参考复用）
  doc     → 文档答案
  hybrid  → 三段式综合文本
"""

import uuid
from dataclasses import asdict

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.conf.app_config import app_config
from app.core.log import logger
from app.core.text_utils import (
    is_context_shortcut,
    same_metric_domain,
    same_numbers,
)
from app.entities.knowledge_item import KnowledgeItem
from app.repositories.mysql.meta.knowledge_mysql_repository import (
    KnowledgeMySQLRepository,
)
from app.repositories.qdrant.knowledge_qdrant_repository import (
    KnowledgeQdrantRepository,
)


class KnowledgeService:
    """负责知识召回、命中统计与问答沉淀的应用服务"""

    # 构造函数：保存知识 MySQL/Qdrant 仓储与 Embedding 客户端；参数为各仓储与客户端对象
    def __init__(
        self,
        knowledge_mysql_repository: KnowledgeMySQLRepository,
        knowledge_qdrant_repository: KnowledgeQdrantRepository,
        embedding_client: HuggingFaceEndpointEmbeddings,
    ):
        # 知识条目本体（答案/SQL/命中统计）落 Meta MySQL
        self.knowledge_mysql_repository = knowledge_mysql_repository
        # 问题向量索引走 Qdrant，支撑相似问题语义复用
        self.knowledge_qdrant_repository = knowledge_qdrant_repository
        # 向量化动作放在 Service 层，与元数据构建服务保持一致
        self.embedding_client = embedding_client

    # 新问题先查知识库：向量检索相似沉淀问题，返回 (KnowledgeItem, score) 列表；
    # 参数 query=用户问题，limit=返回条数上限（默认 1 取最高分复用）
    async def recall(
        self, query: str, limit: int | None = None
    ) -> list[tuple[KnowledgeItem, float]]:
        """召回与当前问题语义相似的沉淀知识条目"""
        await self.knowledge_qdrant_repository.ensure_collection()
        vector = await self.embedding_client.aembed_query(query)
        return await self.knowledge_qdrant_repository.search(
            vector,
            score_threshold=app_config.knowledge.recall_score_threshold,
            limit=limit or app_config.knowledge.recall_limit,
        )

    # 复用命中：把对应知识条目的命中次数加一（失败只记日志，不影响回答）；参数 item_id=知识条目主键 id
    async def hit(self, item_id: str):
        """知识复用命中后累加该条目的命中次数"""
        try:
            await self.knowledge_mysql_repository.increment_hit_count(item_id)
        except Exception as e:
            logger.warning(f"知识命中计数失败 item_id={item_id}: {e}")

    # 沉淀一条问答知识：先向量去重（高相似则合并刷新），否则新写入 MySQL + Qdrant；
    # 参数 question=用户问题，route=来源路由，answer=沉淀答案，sql=最终 SQL(可空)
    async def deposit(
        self, question: str, route: str, answer: str, sql: str | None = None
    ):
        """把一次有价值的问答结论沉淀进知识库（相似问题合并，不重复新增）"""

        # 省略式追问（"那按月呢"）依赖对话上下文才有意义，脱离上下文复用会答错，跳过沉淀
        if is_context_shortcut(question):
            logger.info(f"省略式追问不沉淀：question={question}")
            return

        vector = await self.embedding_client.aembed_query(question)

        # 去重：与已有问题高度相似且口径一致（数字+指标词）时刷新答案/SQL，
        # 保持知识库不膨胀；口径不同（季度变化/指标词冲突）按新条目沉淀
        try:
            await self.knowledge_qdrant_repository.ensure_collection()
            results = await self.knowledge_qdrant_repository.search(
                vector,
                score_threshold=app_config.knowledge.dedup_score_threshold,
                limit=3,
            )
            for existing, score in results:
                if not same_numbers(
                    question, existing.question
                ) or not same_metric_domain(question, existing.question):
                    continue
                await self.knowledge_mysql_repository.refresh(
                    existing.id, answer=answer, sql=sql
                )
                logger.info(
                    f"知识沉淀合并刷新：item_id={existing.id} score={score:.3f} question={question[:40]}"
                )
                return
        except Exception as e:
            # 去重检索失败不阻断沉淀，退化为直接新增
            logger.warning(f"知识去重检索失败，退化为新增：{e}")

        item = KnowledgeItem(
            id=uuid.uuid4().hex,
            question=question,
            answer=answer,
            route=route,
            sql=sql,
            hit_count=0,
        )

        # 条目本体落库与向量写入：会话与元数据仓储共享，可能已有进行中的事务，
        # 因此不用 session.begin()，直接 add + commit 提交（与 increment_hit_count 同模式）
        self.knowledge_mysql_repository.save(item)
        await self.knowledge_mysql_repository.session.commit()
        await self.knowledge_qdrant_repository.upsert(
            [item.id], [vector], [asdict(item)]
        )
        logger.info(
            f"知识沉淀入库：item_id={item.id} route={route} question={question[:40]}"
        )
