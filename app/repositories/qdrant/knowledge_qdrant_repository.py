"""
知识沉淀向量仓储

管理知识向量集合并把知识条目的语义向量点批量写入 Qdrant：
问答沉淀后，问题文本会生成一个向量点（payload 携带完整知识条目），
后续相似提问通过向量检索直接复用沉淀答案。
"""

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import PointStruct
from qdrant_client.models import Distance, VectorParams

from app.conf.app_config import app_config
from app.entities.knowledge_item import KnowledgeItem


class KnowledgeQdrantRepository:
    """负责知识向量集合的创建 写入和语义检索"""

    collection_name = "knowledge_collection"

    # 构造函数：保存 Qdrant 异步客户端；参数 client=AsyncQdrantClient 实例
    def __init__(self, client: AsyncQdrantClient):
        self.client = client

    # 确保知识向量集合存在，不存在则按配置维度创建余弦集合；参数：无
    async def ensure_collection(self):
        """确保知识向量集合存在，并按配置中的维度初始化"""
        if not await self.client.collection_exists(self.collection_name):
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=app_config.qdrant.embedding_size, distance=Distance.COSINE
                ),
            )

    # 把知识向量点批量写入 Qdrant；参数 ids=点 id 列表，embeddings=对应向量列表，payloads=对应元数据字典列表，batch_size=每批写入数量
    async def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        payloads: list[dict],
        batch_size: int = 10,
    ):
        """分批 upsert 知识向量点，避免一次提交过多 point"""
        points: list[PointStruct] = [
            PointStruct(id=id, vector=embedding, payload=payload)
            for id, embedding, payload in zip(ids, embeddings, payloads)
        ]
        for i in range(0, len(points), batch_size):
            await self.client.upsert(
                collection_name=self.collection_name, points=points[i : i + batch_size]
            )

    # 按向量相似度召回知识条目并保留相似度分，返回 (KnowledgeItem, score) 列表；
    # 参数 embedding=查询向量，score_threshold=最低相似度分，limit=返回条数上限
    async def search(
        self, embedding: list[float], score_threshold: float = 0.4, limit: int = 1
    ) -> list[tuple[KnowledgeItem, float]]:
        """按向量相似度检索知识条目，并还原为 KnowledgeItem 实体，同时返回相似度分"""
        result = await self.client.query_points(
            collection_name=self.collection_name,
            query=embedding,
            limit=limit,
            score_threshold=score_threshold,
        )
        # Qdrant 只保存知识条目 payload，业务层继续使用 KnowledgeItem
        return [
            (KnowledgeItem(**point.payload), point.score) for point in result.points
        ]
