"""
应用主配置

定义 conf/app_config.yaml 在程序中的结构化配置对象
项目启动后会在这里一次性完成配置文件加载和类型化转换，其他模块只需要导入 app_config
就可以按属性方式读取日志 MySQL Qdrant Embedding Elasticsearch 和 LLM 配置
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

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
    # 单次重排请求最大文档数：TEI /rerank 有服务端 batch 上限（默认 8），
    # 超出返回 413，故候选按此值分批调用后合并分数
    max_batch: int


@dataclass
class LLMConfig:
    """大模型调用配置（任务分级：主模型/廉价闲聊模型/辅助任务模型）"""

    model_name: str
    api_key: str
    base_url: str
    chat_model_name: str  # 域外闲聊等场景使用的轻量廉价模型
    # 辅助任务模型开关与名称：召回关键词扩展、表/指标过滤选择等结构化辅助任务
    # 下放到该模型以省成本；aux_enabled=false 时辅助任务回退主模型（质量零变化）
    aux_enabled: bool
    aux_model_name: str


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
class MemoryConfig:
    """四类记忆配置（工作/摘要/用户/知识）"""

    enabled: bool  # 总开关：false 时工作记忆退回 InMemorySaver，摘要/用户记忆不读写
    # 工作记忆（短期会话消息）持久化 SQLite 文件路径（相对项目根）
    sqlite_path: str
    # 摘要记忆（中期）：消息条数达到该阈值时滚动生成会话摘要
    summary_trigger_messages: int
    # 用户记忆（长期画像/偏好）开关与注入上限
    user_memory_enabled: bool
    user_memory_max_facts: int
    # 成本节流：仅当本轮问题含"偏好线索"（我/以后/默认/习惯…）才调模型抽取用户事实，
    # 避免每轮都多一次 LLM 调用；false 时每轮都抽取
    user_memory_cue_only: bool


@dataclass
class EvalConfig:
    """问答结果评估配置（LLM-as-judge + 规则检查）"""

    enabled: bool  # 总开关：false 时完全跳过评估
    pass_threshold: float  # 综合分达标阈值，低于则 passed=false
    emit_note: bool  # 未达标时是否向前端发 note 提醒（不覆盖答案）
    llm_weight: float  # LLM 判定器在综合分中的权重（其余归规则判定器）


@dataclass
class OrchestratorConfig:
    """多智能体编排（Supervisor）配置"""

    # 总开关：true 时入口 classify_route 直接转 orchestrator 主 Agent，
    # 由它决定知识复用/子 Agent 计划分发与终端事件；false 时完全走原四级路由
    enabled: bool
    # 计划拆解最多允许的子任务数（防止畸形计划拖垮链路）
    max_tasks: int


@dataclass
class MCPConfig:
    """MCP（Model Context Protocol）外部工具配置

    仅服务于闲聊/域外问答节点（answer_general）：给廉价 chat_llm 绑定外部工具，
    回答"天气/联网搜索"这类简单外部问题；不进入数据分析与文档链路。
    """

    enabled: bool  # 总开关：false 时 answer_general 行为与旧版完全一致
    timeout: float  # 单次工具调用超时（秒）
    max_tool_rounds: int  # 单轮问答内最多工具调用轮数，防止模型反复调用死循环
    # MCP Server 列表：键为服务名，值为连接配置（transport/command/args/env/url/headers 等），
    # 结构随 transport 而定，交由 langchain-mcp-adapters 消费，故用 Dict[str, Any] 宽松承载
    servers: Dict[str, Any]


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
    eval: EvalConfig
    memory: MemoryConfig
    orchestrator: OrchestratorConfig
    mcp: MCPConfig
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
