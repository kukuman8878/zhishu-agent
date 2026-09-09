"""
`knowledge_item` ORM 模型

负责定义元数据库中知识沉淀表的结构，保存问答沉淀的答案、SQL 与来源路由
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeItemMySQL(Base):
    """知识沉淀表对应的 ORM 模型"""

    __tablename__ = "knowledge_item"

    # id 使用 uuid，与 Qdrant 向量点 id 一一对应
    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="知识条目编号"
    )
    question: Mapped[str | None] = mapped_column(Text, comment="用户原始问题")
    answer: Mapped[str | None] = mapped_column(Text, comment="沉淀答案")
    # sql 是 MySQL 保留字，表名属性用 knowledge_sql 映射到 `sql` 列
    knowledge_sql: Mapped[str | None] = mapped_column(
        "sql", Text, comment="生成SQL(仅sql路由)"
    )
    route: Mapped[str | None] = mapped_column(String(16), comment="来源路由")
    hit_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", comment="知识复用命中次数"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
