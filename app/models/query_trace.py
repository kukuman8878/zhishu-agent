"""
`query_trace` ORM 模型

负责定义元数据库中查询轨迹表的结构，保存每次问答的执行轨迹快照
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QueryTraceMySQL(Base):
    """查询轨迹表对应的 ORM 模型"""

    __tablename__ = "query_trace"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="轨迹编号")
    session_id: Mapped[str | None] = mapped_column(
        String(128), comment="会话标识(多轮追问共享)"
    )
    query: Mapped[str | None] = mapped_column(Text, comment="用户问题")
    route: Mapped[str | None] = mapped_column(String(16), comment="最终路由")
    knowledge_item_id: Mapped[str | None] = mapped_column(
        String(64), comment="命中复用的知识条目id(未命中为空)"
    )
    # sql 是 MySQL 保留字，表名属性用 trace_sql 映射到 `sql` 列
    trace_sql: Mapped[str | None] = mapped_column(
        "sql", Text, comment="最终执行的SQL(仅sql路由)"
    )
    sql_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", comment="SQL修正次数"
    )
    row_count: Mapped[int | None] = mapped_column(
        Integer, comment="返回行数(仅sql路由)"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, comment="整体耗时(毫秒)")
    status: Mapped[str | None] = mapped_column(String(16), comment="执行状态")
    error: Mapped[str | None] = mapped_column(Text, comment="失败原因(成功为空)")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
