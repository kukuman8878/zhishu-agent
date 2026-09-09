"""
指标向量仓储

管理指标向量集合，并把 Service 层准备好的指标 point 批量写入 Qdrant

字段和指标虽然都用向量检索，但它们是两类不同对象
所以指标单独使用 metric_info_collection，避免后续召回时和字段结果混在一起
"""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.conf.app_config import app_config
from app.entities.metric_info import MetricInfo


class MetricQdrantRepository:
    """负责指标向量集合的创建 写入和基础检索"""

    collection_name = "metric_info_collection"

    # 构造函数：保存 Qdrant 异步客户端；参数 client=AsyncQdrantClient 实例
    def __init__(self, client: AsyncQdrantClient):
        self.client = client

    # 确保指标向量集合存在，不存在则按 Embedding 维度创建余弦集合；参数：无
    async def ensure_collection(self):
        """确保指标向量集合存在，并按当前 Embedding 维度初始化"""
        if not await self.client.collection_exists(self.collection_name):
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    # 向量维度必须和 Embedding 模型输出一致，否则写入时会失败
                    size=app_config.qdrant.embedding_size,
                    distance=Distance.COSINE,
                ),
            )

    # 把指标向量点批量(按 batch_size)写入 Qdrant；参数 ids=点 id 列表，embeddings=对应向量列表，payloads=对应元数据字典列表，batch_size=每批写入数量
    async def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        payloads: list[dict],
        batch_size: int = 10,
    ):
        """分批 upsert 指标向量点，避免一次提交过多 point"""
        # ids embeddings payloads 三个列表按相同下标组成一条完整的 Qdrant point
        points: list[PointStruct] = [
            PointStruct(id=id, vector=embedding, payload=payload)
            for id, embedding, payload in zip(ids, embeddings, payloads)
        ]
        for i in range(0, len(points), batch_size):
            await self.client.upsert(
                collection_name=self.collection_name, points=points[i : i + batch_size]
            )

    # 按向量相似度召回指标元数据并保留相似度分，返回 (MetricInfo, score) 列表供后续语义重排；
    # 参数 embedding=查询向量，score_threshold=最低相似度分，limit=返回条数上限(粗召回阶段可放大以便重排)
    async def search(
        self, embedding: list[float], score_threshold: float = 0.4, limit: int = 30
    ) -> list[tuple[MetricInfo, float]]:
        """按向量相似度检索指标元数据，并还原为 MetricInfo 实体，同时返回相似度分"""

        result = await self.client.query_points(
            collection_name=self.collection_name,
            query=embedding,
            limit=limit,
            score_threshold=score_threshold,
        )
        # Qdrant point 的 payload 中保存的是指标元数据，业务层继续使用 MetricInfo
        return [(MetricInfo(**point.payload), point.score) for point in result.points]
