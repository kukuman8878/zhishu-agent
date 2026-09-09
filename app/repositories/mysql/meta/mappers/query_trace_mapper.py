"""
QueryTrace 映射器

负责在查询轨迹业务实体和 ORM 模型之间做双向转换，让 Service/Repository
不需要直接操作 SQLAlchemy 模型
"""

from app.entities.query_trace import QueryTrace
from app.models.query_trace import QueryTraceMySQL


class QueryTraceMapper:
    """负责 `QueryTrace` 与 `QueryTraceMySQL` 之间的双向转换"""

    # 把查询轨迹 ORM 模型还原为业务实体供上层流转；参数 query_trace_mysql=轨迹 ORM 模型对象
    @staticmethod
    def to_entity(query_trace_mysql: QueryTraceMySQL) -> QueryTrace:
        """把 ORM 模型还原成业务实体，便于上层业务逻辑继续流转"""
        return QueryTrace(
            id=query_trace_mysql.id,
            session_id=query_trace_mysql.session_id,
            query=query_trace_mysql.query,
            route=query_trace_mysql.route,
            status=query_trace_mysql.status,
            knowledge_item_id=query_trace_mysql.knowledge_item_id,
            sql=query_trace_mysql.trace_sql,
            sql_attempts=query_trace_mysql.sql_attempts,
            row_count=query_trace_mysql.row_count,
            duration_ms=query_trace_mysql.duration_ms,
            error=query_trace_mysql.error,
        )

    # 把查询轨迹业务实体转成 ORM 模型交给 SQLAlchemy 托管；参数 query_trace=轨迹业务实体
    @staticmethod
    def to_model(query_trace: QueryTrace) -> QueryTraceMySQL:
        """把业务实体转换成 ORM 模型，交给 SQLAlchemy 托管"""
        return QueryTraceMySQL(
            id=query_trace.id,
            session_id=query_trace.session_id,
            query=query_trace.query,
            route=query_trace.route,
            knowledge_item_id=query_trace.knowledge_item_id,
            trace_sql=query_trace.sql,
            sql_attempts=query_trace.sql_attempts,
            row_count=query_trace.row_count,
            duration_ms=query_trace.duration_ms,
            status=query_trace.status,
            error=query_trace.error,
        )
