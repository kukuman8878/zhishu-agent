"""
健康检查路由

提供 /health 端点，供 Docker Compose healthcheck 和负载均衡器探活使用。
返回各外部依赖（MySQL/Qdrant/ES/Embedding）的连通状态。
"""

import asyncio

from fastapi import APIRouter

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager

health_router = APIRouter()


async def _check_mysql(manager) -> bool:
    """检查 MySQL 连接是否可用"""
    try:
        if manager.engine is None:
            return False
        async with manager.session_factory() as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_qdrant() -> bool:
    """检查 Qdrant 连接是否可用"""
    try:
        if qdrant_client_manager.client is None:
            return False
        await qdrant_client_manager.client.get_collections()
        return True
    except Exception:
        return False


async def _check_es() -> bool:
    """检查 Elasticsearch 连接是否可用"""
    try:
        if es_client_manager.client is None:
            return False
        await es_client_manager.client.info()
        return True
    except Exception:
        return False


async def _check_embedding() -> bool:
    """检查 Embedding 服务是否可用"""
    try:
        if embedding_client_manager.client is None:
            return False
        await embedding_client_manager.client.aembed_query("test")
        return True
    except Exception:
        return False


@health_router.get("/health")
async def health_check():
    """健康检查端点：并发探测各外部依赖连通性"""
    results = await asyncio.gather(
        _check_mysql(meta_mysql_client_manager),
        _check_mysql(dw_mysql_client_manager),
        _check_qdrant(),
        _check_es(),
        _check_embedding(),
        return_exceptions=True,
    )

    services = {
        "meta_mysql": bool(results[0]),
        "dw_mysql": bool(results[1]),
        "qdrant": bool(results[2]),
        "elasticsearch": bool(results[3]),
        "embedding": bool(results[4]),
    }
    healthy = all(services.values())

    return {
        "status": "healthy" if healthy else "degraded",
        "services": services,
    }
