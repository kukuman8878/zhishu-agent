"""
Rerank 客户端管理器

负责按配置初始化 cross-encoder 重排服务客户端，供字段 指标 取值三路召回
在粗召回之后做语义精排。重排服务复用 TEI（text-embeddings-inference），
但跑 reranker 类模型，暴露 /rerank 端点，因此这里直接走 HTTP 调用而不引入额外库。
"""

import asyncio
from typing import Optional

import aiohttp

from app.conf.app_config import RerankConfig, app_config


class RerankClient:
    """封装对 TEI /rerank 端点的异步调用，复用持久 aiohttp Session"""

    # 构造函数：保存重排配置；参数 config=含 host/port/model 的 Rerank 配置对象
    def __init__(self, config: RerankConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    # 根据配置拼接重排服务地址；返回形如 http://host:port/rerank 的端点地址；参数：无(使用 self.config)
    def _rerank_url(self) -> str:
        """拼接 TEI 重排端点地址"""
        return f"http://{self.config.host}:{self.config.port}/rerank"

    # 显式创建持久 Session，避免每次调用都创建/销毁连接池；参数：无
    def init(self):
        """创建持久 aiohttp ClientSession"""
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)

    # 对 query 与候选文本列表做语义打分排序，返回 (原始下标, 重排分数) 降序列表；
    # 参数 query=用户问题，texts=候选文本列表，top_n=要返回的最高分项数(默认全量)
    async def rerank(
        self, query: str, texts: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """调用 cross-encoder 对候选文本重新打分，按分数降序返回(索引, 分数)元组列表"""

        if not texts:
            return []

        if self._session is None:
            raise RuntimeError("RerankClient 尚未 init")

        payload: dict = {"query": query, "texts": texts}
        if top_n is not None:
            payload["top_n"] = top_n

        async with self._session.post(self._rerank_url(), json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        # TEI /rerank 返回形如 {"index": 0, "score": 0.93} 的列表，按 score 降序
        ranked = sorted(data, key=lambda item: item["score"], reverse=True)
        return [(item["index"], item["score"]) for item in ranked]

    # 关闭持久 Session，释放连接池；参数：无
    async def close(self):
        """关闭持久 Session"""
        if self._session is not None:
            await self._session.close()
            self._session = None


class RerankClientManager:
    """管理 Rerank 客户端的初始化与复用"""

    # 构造函数：保存重排配置并预留 client 字段；参数 config=含 host/port/model 的 Rerank 配置对象
    def __init__(self, config: RerankConfig):
        self.client: Optional[RerankClient] = None
        self.config = config

    # 按配置创建并初始化 Rerank 客户端；参数：无
    def init(self):
        """显式初始化客户端，避免模块导入时立即建立外部连接"""
        self.client = RerankClient(self.config)
        self.client.init()

    # 关闭 Rerank 客户端持有的连接资源；参数：无
    async def close(self):
        """释放客户端连接资源"""
        if self.client is not None:
            await self.client.close()
            self.client = None


# 模块级单例，供整个项目复用同一套重排客户端
rerank_client_manager = RerankClientManager(app_config.rerank)


if __name__ == "__main__":
    rerank_client_manager.init()
    client = rerank_client_manager.client

    # 本地调试：对一段文本做最小化重排调用，验证 Rerank 服务可用；参数：无
    async def test():
        """执行一次最小化重排调用，验证服务是否可用"""
        result = await client.rerank(
            "统计华北地区的销售总额",
            ["华北地区", "西南地区", "销售总额", "会员等级"],
        )
        print(result)

    asyncio.run(test())
