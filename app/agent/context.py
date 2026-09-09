"""
智数 Agent 运行上下文

Context 用来保存一次图执行过程中不参与状态合并的外部依赖或配置
本章放入多路召回需要的 Embedding 客户端 Qdrant 仓储和 ES 仓储
召回信息合并阶段还会访问 Meta MySQL，用于按 id 补齐字段和表结构元数据
额外上下文补全阶段会访问 DW MySQL，用于读取数据库方言和版本
这样节点可以通过 runtime.context 复用外部工具，而不需要把连接类对象塞进 State
"""

from typing import TypedDict

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.clients.doc_engine_client_manager import DocEngineClient
from app.clients.rerank_client_manager import RerankClient
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.knowledge_mysql_repository import (
    KnowledgeMySQLRepository,
)
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.knowledge_qdrant_repository import (
    KnowledgeQdrantRepository,
)
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository


class DataAgentContext(TypedDict):
    """LangGraph Runtime 中传递的上下文对象"""

    # 字段向量仓储，负责根据向量从 Qdrant 检索候选字段
    column_qdrant_repository: ColumnQdrantRepository
    # Embedding 客户端，负责把关键词转换成向量检索所需的 query vector
    embedding_client: HuggingFaceEndpointEmbeddings
    # 指标向量仓储，负责根据向量从 Qdrant 检索候选指标
    metric_qdrant_repository: MetricQdrantRepository
    # 字段取值全文检索仓储，负责从 Elasticsearch 检索真实字段值
    value_es_repository: ValueESRepository
    # 元数据仓储，负责在召回结果合并时补齐字段 表 主外键等结构信息
    meta_mysql_repository: MetaMySQLRepository
    # 数仓仓储，负责在额外上下文补全时读取数据库方言 版本等执行环境信息
    dw_mysql_repository: DWMySQLRepository
    # 文档问答客户端，负责把问题交给进程内文档问答引擎（app/rag_engine/）
    rag_client: DocEngineClient
    # 跨编码器语义重排客户端，负责在三路召回去重后对候选做精排截取 top_k
    rerank_client: RerankClient
    # 知识沉淀 MySQL 仓储：classify_route 召回复用与 answer_knowledge 命中计数使用
    knowledge_mysql_repository: KnowledgeMySQLRepository
    # 知识沉淀向量仓储：classify_route 按问题语义检索沉淀知识条目
    knowledge_qdrant_repository: KnowledgeQdrantRepository
