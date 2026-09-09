"""
应用主配置

定义 conf/app_config.yaml 在程序中的结构化配置对象
项目启动后会在这里一次性完成配置文件加载和类型化转换，其他模块只需要导入 app_config
就可以按属性方式读取日志 MySQL Qdrant Embedding Elasticsearch 和 LLM 配置
"""

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv  # noqa: F401 保留导入以兼容直接运行 app_config.py 的场景
from omegaconf import OmegaConf


@dataclass
class File:
    """文件日志配置"""

    enable: bool
    level: str
    path: str
    rotation: str
    retention: str


@dataclass
class Console:
    """控制台日志配置"""

    enable: bool
    level: str


@dataclass
class LoggingConfig:
    """日志总配置"""

    file: File
    console: Console


@dataclass
class DBConfig:
    """MySQL 连接配置"""

    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass
class QdrantConfig:
    """Qdrant 连接与向量维度配置"""

    host: str
    port: int
    embedding_size: int


@dataclass
class EmbeddingConfig:
    """Embedding 服务配置"""

    host: str
    port: int
    model: str


@dataclass
class ESConfig:
    """Elasticsearch 配置"""

    host: str
    port: int
    index_name: str


@dataclass
class RerankConfig:
    """cross-encoder 语义重排配置（复用 TEI 服务，独立端口跑 reranker 模型）"""

    host: str
    port: int
    model: str
    # 召回阶段先从 Qdrant/ES 捞回的候选条数，交给 reranker 精排前需要更多候选
    recall_limit: int
    # 重排后截取并写回 state 的最终条数
    top_k: int


@dataclass
class LLMConfig:
    """大模型调用配置"""

    model_name: str
    api_key: str
    base_url: str
    chat_model_name: str  # 域外闲聊等场景使用的轻量廉价模型


@dataclass
class HybridConfig:
    """跨源综合（hybrid）配置"""

    # v2 计划-执行模式开关：开启后 hybrid 走 plan→execute(子任务复用 SQL 子图/文档引擎)→synthesize；
    # 关闭则走 v1 并行-拼接（整句喂 SQL 链 + 文档链，再 synthesize）
    v2_enabled: bool


@dataclass
class DocEngineConfig:
    """文档问答引擎配置（引擎已融入本进程，代码在 app/rag_engine/）"""

    base_url: str  # 兼容保留字段（进程内调用后不再使用，留空即可）
    timeout: float  # 单次文档问答超时（秒），引擎含多轮校验较慢
    enabled: bool  # 是否启用文档问答链路；false 时跳过索引装载并走降级


@dataclass
class KnowledgeConfig:
    """LLMWiki 式知识沉淀配置"""

    enabled: bool  # 总开关：关闭后不做召回复用也不做沉淀
    deposit_enabled: bool  # 沉淀开关：问答结束后是否把结论写入知识库
    # 召回复用阈值：新问题与沉淀问题向量相似度达到该值即直接复用沉淀答案
    recall_score_threshold: float
    recall_limit: int  # 召回候选条数（取最高分一条复用）
    # 去重阈值：沉淀时与已有问题相似度达到该值则合并刷新，而不是新增重复条目
    dedup_score_threshold: float


@dataclass
class TraceConfig:
    """查询轨迹持久化配置"""

    enabled: bool  # 是否在每次问答结束后把执行轨迹写入 query_trace 表


@dataclass
class APIConfig:
    """API 鉴权配置"""

    # API Key：前端请求头 X-API-Key 需携带此值；为空时跳过鉴权（开发模式）
    api_key: str


@dataclass
class AppConfig:
    """项目级总配置入口"""

    logging: LoggingConfig
    db_meta: DBConfig
    db_dw: DBConfig
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    rerank: RerankConfig
    es: ESConfig
    llm: LLMConfig
    hybrid: HybridConfig
    doc_engine: DocEngineConfig
    knowledge: KnowledgeConfig
    trace: TraceConfig
    api: APIConfig


# 从当前文件位置回到项目根目录，再定位到 conf/app_config.yaml
project_root = Path(__file__).parents[2]
config_file = project_root / "conf" / "app_config.yaml"

# 读取本地 .env 供 YAML 中的 ${oc.env:...} 解析；load_dotenv 幂等且不覆盖已存在环境变量，
# 放在模块级可保证脚本入口（如 build_meta_knowledge）与 HTTP 入口都能拿到敏感配置
load_dotenv(project_root / ".env")

# 读取 YAML 配置内容
context = OmegaConf.load(config_file)

# 根据 AppConfig 生成结构化配置 schema
schema = OmegaConf.structured(AppConfig)

# 把配置结构和配置值合并，再转换成可以直接按属性访问的对象
app_config: AppConfig = OmegaConf.to_object(OmegaConf.merge(schema, context))

if __name__ == "__main__":
    # 简单测试：验证配置是否能正常读取
    print(app_config.es.host)
