"""
FastAPI 依赖组装

集中声明 API 层需要的依赖函数，把 Session、Repository、Client 和 Service
按职责组装起来。路由层只通过 Depends 声明自己需要什么对象，具体创建细节
都收敛在这里，避免 HTTP 处理函数直接感知底层基础设施。
"""

from typing import Annotated

from fastapi import Depends
from langchain_huggingface import HuggingFaceEndpointEmbeddings
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.doc_engine_client_manager import (
    DocEngineClient,
    doc_engine_client_manager,
)
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.clients.rerank_client_manager import RerankClient, rerank_client_manager
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
from app.services.query_service import QueryService


# FastAPI 依赖：创建本次请求使用的元数据库 AsyncSession，请求结束后自动关闭并清理连接；参数：无
async def get_meta_session():
    """创建一次请求内使用的元数据库 Session"""

    # yield 之后的清理逻辑由 async with 负责，FastAPI 会在请求结束后继续执行退出流程
    async with meta_mysql_client_manager.session_factory() as meta_session:
        yield meta_session


# FastAPI 依赖：用请求级 Session 组装元数据仓储注入给业务层；参数 session=上一步注入的元数据库会话
async def get_meta_mysql_repository(
    session: Annotated[AsyncSession, Depends(get_meta_session)],
) -> MetaMySQLRepository:
    """基于请求级 Session 创建元数据仓储"""

    return MetaMySQLRepository(session)


# FastAPI 依赖：返回启动阶段已初始化好的 Embedding 向量化客户端供召回使用；参数：无
async def get_embedding_client() -> HuggingFaceEndpointEmbeddings:
    """获取应用启动阶段初始化好的 Embedding 客户端"""

    return embedding_client_manager.client


# FastAPI 依赖：创建本次请求使用的数仓(dw) AsyncSession，请求结束后自动关闭；参数：无
async def get_dw_session():
    """创建一次请求内使用的数仓 Session"""

    async with dw_mysql_client_manager.session_factory() as dw_session:
        yield dw_session


# FastAPI 依赖：用请求级 Session 组装数仓仓储注入给业务层；参数 session=上一步注入的数仓会话
async def get_dw_mysql_repository(
    session: Annotated[AsyncSession, Depends(get_dw_session)],
) -> DWMySQLRepository:
    """基于请求级 Session 创建数仓仓储"""

    return DWMySQLRepository(session)


# FastAPI 依赖：基于启动时初始化的 Qdrant 客户端创建字段向量检索仓储；参数：无
async def get_column_qdrant_repository() -> ColumnQdrantRepository:
    """创建字段向量检索仓储"""

    return ColumnQdrantRepository(qdrant_client_manager.client)


# FastAPI 依赖：基于启动时初始化的 Qdrant 客户端创建指标向量检索仓储；参数：无
async def get_metric_qdrant_repository() -> MetricQdrantRepository:
    """创建指标向量检索仓储"""

    return MetricQdrantRepository(qdrant_client_manager.client)


# FastAPI 依赖：基于启动时初始化的 ES 客户端创建字段取值全文检索仓储；参数：无
async def get_value_es_repository() -> ValueESRepository:
    """创建字段取值全文检索仓储"""

    return ValueESRepository(es_client_manager.client)


# FastAPI 依赖：基于请求级元数据库 Session 创建知识沉淀仓储（与元数据仓储共享同一会话）；参数 session=请求级元数据库会话
async def get_knowledge_mysql_repository(
    session: Annotated[AsyncSession, Depends(get_meta_session)],
) -> KnowledgeMySQLRepository:
    """基于请求级 Session 创建知识沉淀 MySQL 仓储"""

    return KnowledgeMySQLRepository(session)


# FastAPI 依赖：基于启动时初始化的 Qdrant 客户端创建知识沉淀向量检索仓储；参数：无
async def get_knowledge_qdrant_repository() -> KnowledgeQdrantRepository:
    """创建知识沉淀向量检索仓储"""

    return KnowledgeQdrantRepository(qdrant_client_manager.client)


# FastAPI 依赖：基于请求级元数据库 Session 创建查询轨迹仓储（与元数据仓储共享同一会话）；参数 session=请求级元数据库会话
async def get_trace_mysql_repository(
    session: Annotated[AsyncSession, Depends(get_meta_session)],
) -> TraceMySQLRepository:
    """基于请求级 Session 创建查询轨迹 MySQL 仓储"""

    return TraceMySQLRepository(session)


# FastAPI 依赖：基于请求级元数据库 Session 创建问答评估仓储（与元数据仓储共享同一会话）；参数 session=请求级元数据库会话
async def get_eval_mysql_repository(
    session: Annotated[AsyncSession, Depends(get_meta_session)],
) -> EvalMySQLRepository:
    """基于请求级 Session 创建问答评估 MySQL 仓储"""

    return EvalMySQLRepository(session)


# FastAPI 依赖：基于请求级元数据库 Session 创建长期记忆仓储（摘要+用户偏好）；参数 session=请求级元数据库会话
async def get_memory_mysql_repository(
    session: Annotated[AsyncSession, Depends(get_meta_session)],
) -> MemoryMySQLRepository:
    """基于请求级 Session 创建长期记忆 MySQL 仓储"""

    return MemoryMySQLRepository(session)


# FastAPI 依赖：返回启动阶段初始化好的文档问答(agentic RAG)客户端供 doc/hybrid 链路使用；参数：无
async def get_rag_client() -> DocEngineClient:
    """获取应用启动阶段初始化好的文档问答客户端"""

    return doc_engine_client_manager.client


# FastAPI 依赖：返回启动阶段初始化好的语义重排客户端供三路召回精排使用；参数：无
async def get_rerank_client() -> RerankClient:
    """获取应用启动阶段初始化好的 cross-encoder 语义重排客户端"""

    return rerank_client_manager.client


# FastAPI 依赖：把元数据/数仓仓储、Embedding/Qdrant/ES 客户端、知识沉淀仓储与文档问答客户端组装成 QueryService；参数为各依赖的注入对象
async def get_query_service(
    meta_mysql_repository: Annotated[
        MetaMySQLRepository, Depends(get_meta_mysql_repository)
    ],
    embedding_client: Annotated[
        HuggingFaceEndpointEmbeddings, Depends(get_embedding_client)
    ],
    dw_mysql_repository: Annotated[DWMySQLRepository, Depends(get_dw_mysql_repository)],
    column_qdrant_repository: Annotated[
        ColumnQdrantRepository, Depends(get_column_qdrant_repository)
    ],
    metric_qdrant_repository: Annotated[
        MetricQdrantRepository, Depends(get_metric_qdrant_repository)
    ],
    value_es_repository: Annotated[ValueESRepository, Depends(get_value_es_repository)],
    rag_client: Annotated[DocEngineClient, Depends(get_rag_client)],
    rerank_client: Annotated[RerankClient, Depends(get_rerank_client)],
    knowledge_mysql_repository: Annotated[
        KnowledgeMySQLRepository, Depends(get_knowledge_mysql_repository)
    ],
    knowledge_qdrant_repository: Annotated[
        KnowledgeQdrantRepository, Depends(get_knowledge_qdrant_repository)
    ],
    trace_mysql_repository: Annotated[
        TraceMySQLRepository, Depends(get_trace_mysql_repository)
    ],
    eval_mysql_repository: Annotated[
        EvalMySQLRepository, Depends(get_eval_mysql_repository)
    ],
    memory_mysql_repository: Annotated[
        MemoryMySQLRepository, Depends(get_memory_mysql_repository)
    ],
) -> QueryService:
    """组装一次查询所需的业务服务"""

    # QueryService 只接收已经创建好的依赖对象，本身不关心这些对象来自 MySQL、Qdrant 还是 ES
    return QueryService(
        meta_mysql_repository=meta_mysql_repository,
        embedding_client=embedding_client,
        dw_mysql_repository=dw_mysql_repository,
        column_qdrant_repository=column_qdrant_repository,
        metric_qdrant_repository=metric_qdrant_repository,
        value_es_repository=value_es_repository,
        rag_client=rag_client,
        rerank_client=rerank_client,
        knowledge_mysql_repository=knowledge_mysql_repository,
        knowledge_qdrant_repository=knowledge_qdrant_repository,
        trace_mysql_repository=trace_mysql_repository,
        eval_mysql_repository=eval_mysql_repository,
        memory_mysql_repository=memory_mysql_repository,
    )
