"""
查询轨迹业务实体

每次问答结束后把结构化执行轨迹（路由、SQL、修正次数、耗时、结果行数、状态）
沉淀到 query_trace 表，用于事后复盘、问题排查与评估集构建。
"""

from dataclasses import dataclass


@dataclass
class QueryTrace:
    """一次问答执行轨迹的业务表达"""

    id: str
    session_id: str | None
    query: str
    route: str
    status: str  # success / error
    # 命中复用的知识条目 id（knowledge 路由复用沉淀答案时写入）
    knowledge_item_id: str | None = None
    # 最终执行的 SQL（仅 sql 路由）
    sql: str | None = None
    # SQL 修正次数（自愈环触发次数）
    sql_attempts: int = 0
    # SQL 返回行数（仅 sql 路由）
    row_count: int | None = None
    # 整体耗时（毫秒）
    duration_ms: int | None = None
    # 失败原因（成功为空）
    error: str | None = None
