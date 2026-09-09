"""
Qdrant 客户端管理器

统一创建和管理 Qdrant 异步客户端
主要用于保存字段和指标的向量索引，支撑后续问数流程中的语义召回
"""

import asyncio
import random
from typing import Optional

from qdrant_client import AsyncQdrantClient, models

from app.conf.app_config import QdrantConfig, app_config


class QdrantClientManager:
    """管理 Qdrant 客户端的初始化与关闭"""

    # 构造函数：保存 Qdrant 连接配置并预留 client 字段；参数 qdrant_config=包含 host/port 的 Qdrant 配置对象
    def __init__(self, qdrant_config: QdrantConfig):
        # 保存配置对象，后面初始化客户端时要从这里读取 host 和 port
        self.qdrant_config = qdrant_config
        # 先把 client 声明出来，真正初始化放到 init() 中进行
        self.client: Optional[AsyncQdrantClient] = None

    # 根据配置拼接 Qdrant HTTP 服务地址并返回；参数：无(使用 self.qdrant_config)
    def _get_url(self) -> str:
        """拼接 Qdrant 服务地址"""
        return f"http://{self.qdrant_config.host}:{self.qdrant_config.port}"

    # 显式创建并初始化 Qdrant 异步客户端，避免导入模块即建连；参数：无
    def init(self):
        """
        显式初始化 Qdrant 客户端
        这里不在 __init__ 中直接初始化，是为了和项目的生命周期管理保持一致
        """
        self.client = AsyncQdrantClient(url=self._get_url())

    # 关闭 Qdrant 异步客户端连接，供应用关闭阶段释放资源调用；参数：无
    async def close(self):
        """关闭 Qdrant 客户端连接"""
        await self.client.close()


# 创建一个全局的管理器对象
# 后续项目中的其他模块都通过它来获取同一套 Qdrant 客户端
qdrant_client_manager = QdrantClientManager(app_config.qdrant)


if __name__ == "__main__":
    # 先初始化客户端，后面的测试逻辑才能真正访问 Qdrant
    qdrant_client_manager.init()

    # 本地调试：执行一次集合创建/写入/向量检索以验证 Qdrant 接入链路；参数：无
    async def test():
        """执行一次集合创建、写入和查询，验证 Qdrant 接入链路"""
        client = qdrant_client_manager.client
        # 如果集合不存在，就先创建一个集合
        if not await client.collection_exists("my_collection"):
            await client.create_collection(
                collection_name="my_collection",
                vectors_config=models.VectorParams(
                    # 当前集合中的向量维度是 10
                    size=10,
                    # 使用余弦相似度作为距离计算方式
                    distance=models.Distance.COSINE,
                ),
            )

        # 向集合中写入 100 个随机 point
        # 每个 point 都有一个 id 和一个 10 维向量
        await client.upsert(
            collection_name="my_collection",
            points=[
                models.PointStruct(
                    id=i,
                    vector=[random.random() for _ in range(10)],
                )
                for i in range(100)
            ],
        )

        # 用一个随机生成的查询向量做相似度检索
        # limit=10 表示最多返回 10 条结果
        # score_threshold=0.8 表示只保留分数不低于 0.8 的结果
        res = await client.query_points(
            collection_name="my_collection",
            query=[random.random() for _ in range(10)],  # type: ignore
            limit=10,
            score_threshold=0.8,
        )

        print(res)

    asyncio.run(test())
