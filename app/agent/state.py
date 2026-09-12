"""
智数 Agent 状态定义

State 是 LangGraph 各节点之间传递和更新的共享数据
本章在用户原始问题之外，新增关键词列表和三路召回结果
并把召回到的实体整理成后续提示词更容易消费的表信息和指标信息
SQL 生成闭环会继续写入候选 SQL 以及校验错误信息，用于控制校正或执行分支

字段约束说明：TypedDict 默认 total=True，即所有字段在构造时都必须给值；
但本图只有 query 是外部输入，其余字段都由上游节点按链路顺序写入。
因此除 query 外全部标记为 NotRequired，否则用 `DataAgentState(query=...)`
初始化会在 LangGraph 输入校验阶段因缺字段报错。各节点读取 key 时，
其前驱节点保证已写入（见 graph.py 拓扑顺序），无需额外 .get() 兜底。
"""

from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo


class MetricInfoState(TypedDict):
    """面向 SQL 生成提示词的指标信息"""

    name: str
    description: str
    # 指标依赖的字段 id，用来提示模型不要脱离业务口径随意计算
    relevant_columns: list[str]
    alias: list[str]


class ColumnInfoState(TypedDict):
    """表上下文中的字段信息"""

    name: str
    type: str
    role: str
    # 字段真实样例值，尤其用于辅助 where 条件里的枚举值选择
    examples: list
    description: str
    alias: list[str]


class TableInfoState(TypedDict):
    """SQL 生成阶段真正传给模型的表结构上下文"""

    name: str
    role: str
    description: str
    columns: list[ColumnInfoState]


class DateInfoState(TypedDict):
    """SQL 生成阶段使用的当前日期上下文"""

    date: str
    weekday: str
    quarter: str


class DBInfoState(TypedDict):
    """SQL 生成阶段使用的数据库环境信息"""

    dialect: str
    version: str


class DataAgentState(TypedDict):
    """一次问数链路中的核心状态"""

    query: str  # 用户输入的查询
    # ==== 以下字段均由上游节点写入，构造状态时不需要提供（见模块 docstring）====
    # 会话多轮对话历史：classify_route 写入 HumanMessage，各终端节点写入 AIMessage；
    # add_messages 归并 + checkpointer(thread_id) 持久化，支撑"那按月呢"这类省略式追问
    messages: NotRequired[Annotated[list[AnyMessage], add_messages]]
    route: NotRequired[
        str
    ]  # 意图路由结果（classify_route 写入）：sql 数据分析 / doc 文档问答 / hybrid 跨源综合 / chat 闲聊
    keywords: NotRequired[list[str]]  # 抽取的关键词
    retrieved_column_infos: NotRequired[list[ColumnInfo]]  # 检索到的字段信息
    retrieved_metric_infos: NotRequired[list[MetricInfo]]  # 检索到的指标信息
    retrieved_value_infos: NotRequired[list[ValueInfo]]  # 检索到的取值信息

    table_infos: NotRequired[list[TableInfoState]]  # 合并和补齐后的表结构上下文
    metric_infos: NotRequired[list[MetricInfoState]]  # 合并后的指标上下文
    date_info: NotRequired[DateInfoState]  # 当前日期 星期和季度信息
    db_info: NotRequired[DBInfoState]  # 数据库方言和版本信息

    sql: NotRequired[str]  # 生成或校正后的SQL

    error: NotRequired[str]  # 校验SQL时出现的错误信息

    # ==== 以下为 SQL 执行失败自愈字段（run_sql 写入）====
    run_error: NotRequired[
        str
    ]  # SQL 执行失败时的数据库报错，非空时条件边路由回 correct_sql
    sql_attempts: NotRequired[int]  # 已发生的修正重试次数（从 0 起，超过上限则终止）

    # ==== 以下为结果自检字段（verify_result 写入）====
    verify_note: NotRequired[str]  # 结果自检发现的疑点提醒（正常为空，不发 note 事件）

    # ===== 以下为 doc / hybrid 分支新增字段（P2/P3 使用）=====
    # SQL 执行返回的行数据：sql 路由供前端 result 事件；hybrid 路由供综合节点消费
    sql_rows: NotRequired[list]
    # 文档问答引擎返回结果（doc_query 写入），hybrid 综合与前端 doc 事件复用
    doc_answer: NotRequired[str]
    doc_cited_pages: NotRequired[list[str]]
    doc_rounds: NotRequired[int]
    doc_route: NotRequired[dict]
    # hybrid 综合产物（synthesize 写入），前端按 mode 渲染
    hybrid_content: NotRequired[str]

    # ===== 以下为知识沉淀（LLMWiki 式）新增字段 =====
    # 知识库召回复用命中的沉淀答案（classify_route 写入，answer_knowledge 消费）
    knowledge_answer: NotRequired[str]
    # 命中知识条目的主键 id，供 answer_knowledge 累加命中次数
    knowledge_item_id: NotRequired[str]
    # 命中时的向量相似度分，供日志与调参参考
    knowledge_score: NotRequired[float]

    # ===== 以下为四类记忆的长期记忆片段 =====
    # 会话摘要 + 用户偏好渲染成的文本（QueryService 加载，各节点注入提示词）
    memory_text: NotRequired[str]
