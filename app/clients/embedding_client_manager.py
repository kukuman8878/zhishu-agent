"""
Embedding 客户端管理器

负责按配置初始化 Embedding 服务客户端，并为字段、指标和用户问题的向量化
提供统一访问入口
"""

import asyncio
from typing import Optional

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.conf.app_config import EmbeddingConfig, app_config


class EmbeddingClientManager:
    """管理 Embedding 服务客户端的初始化与复用"""

    # 构造函数：保存 Embedding 配置并预留 client 字段；参数 config=含 host/port 的 Embedding 配置对象
    def __init__(self, config: EmbeddingConfig):
        self.client: Optional[HuggingFaceEndpointEmbeddings] = None
        self.config = config

    # 根据配置拼接 Embedding 服务地址并返回；参数：无(使用 self.config)
    def _get_url(self) -> str:
        """拼接 Embedding 服务地址"""
        return f"http://{self.config.host}:{self.config.port}"

    # 按配置创建并初始化 Embedding 客户端；参数：无
    def init(self):
        """显式初始化客户端，避免模块导入时立即建立外部连接"""
        self.client = HuggingFaceEndpointEmbeddings(model=self._get_url())

    # 关闭客户端（HuggingFaceEndpointEmbeddings 无连接池，置空即可）；参数：无
    async def close(self):
        """释放客户端资源（当前无长连接，保留方法以统一生命周期）"""
        self.client = None


# 模块级单例，供整个项目复用同一套 Embedding 客户端管理器
embedding_client_manager = EmbeddingClientManager(app_config.embedding)


if __name__ == "__main__":
    embedding_client_manager.init()
    client = embedding_client_manager.client

    # 本地调试：对一段文本做最小化向量化调用，验证 Embedding 服务可用；参数：无
    async def test():
        """执行一次最小化向量化调用，验证服务是否可用"""
        text = "What is deep learning?"
        query_result = await client.aembed_query(text)
        print(query_result[:3])

    asyncio.run(test())
