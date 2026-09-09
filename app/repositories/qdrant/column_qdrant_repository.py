"""
字段向量仓储

管理字段向量集合并把已经准备好的 point 批量写入 Qdrant

Service 层负责决定一个字段要拆成哪些 point
Repository 只关心集合存在和向量点如何稳定落库
"""

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import PointStruct
from qdrant_client.models import Distance, VectorParams

from app.conf.app_config import app_config
from app.entities.column_info import ColumnInfo


class ColumnQdrantRepository:
    """负责字段向量集合的创建 写入和基础检索"""

    collection_name = "column_info_collection"

    # 构造函数：保存 Qdrant 异步客户端；参数 client=AsyncQdrantClient 实例
    def __init__(self, client: AsyncQdrantClient):
        self.client = client

    # 确保字段向量集合存在，不存在则按配置维度创建余弦集合；参数：无
    async def ensure_collection(self):
        """确保字段向量集合存在，并按配置中的维度初始化"""
        if not await self.client.collection_exists(self.collection_name):
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=app_config.qdrant.embedding_size, distance=Distance.COSINE
                ),
            )

    # 把字段向量点批量(按 batch_size)写入 Qdrant；参数 ids=点 id 列表，embeddings=对应向量列表，payloads=对应元数据字典列表，batch_size=每批写入数量
    async def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        payloads: list[dict],
        batch_size: int = 10,
    ):
        """分批 upsert 字段向量点，避免一次提交过多 point"""
        points: list[PointStruct] = [
            PointStruct(id=id, vector=embedding, payload=payload)
            for id, embedding, payload in zip(ids, embeddings, payloads)
        ]
        for i in range(0, len(points), batch_size):
            await self.client.upsert(
                collection_name=self.collection_name, points=points[i : i + batch_size]
            )

    # 按向量相似度召回字段元数据并保留相似度分，返回 (ColumnInfo, score) 列表供后续语义重排；
    # 参数 embedding=查询向量，score_threshold=最低相似度分，limit=返回条数上限(粗召回阶段可放大以便重排)
    async def search(
        self, embedding: list[float], score_threshold: float = 0.4, limit: int = 30
    ) -> list[tuple[ColumnInfo, float]]:
        """按向量相似度检索字段元数据，并还原为 ColumnInfo 实体，同时返回相似度分"""
        result = await self.client.query_points(
            collection_name=self.collection_name,
            query=embedding,
            limit=limit,
            score_threshold=score_threshold,
        )
        # Qdrant 只保存字段元数据 payload，业务层继续使用 ColumnInfo
        return [(ColumnInfo(**point.payload), point.score) for point in result.points]
