"""
`query_eval` ORM 模型

定义元数据库中问答结果评估表的结构：一条问答一条评估记录，
综合分 + 各维度得分（JSON 文本）+ 判定理由 + 被评估答案快照。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QueryEvalMySQL(Base):
    """问答结果评估表对应的 ORM 模型"""

    __tablename__ = "query_eval"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="评估编号")
    session_id: Mapped[str | None] = mapped_column(
        String(128), comment="会话标识(多轮追问共享)"
    )
    query: Mapped[str | None] = mapped_column(Text, comment="用户问题")
    route: Mapped[str | None] = mapped_column(String(16), comment="最终路由")
    judge: Mapped[str | None] = mapped_column(String(64), comment="判定器名称")
    score: Mapped[float] = mapped_column(Float, comment="综合分0~1")
    passed: Mapped[bool] = mapped_column(Boolean, comment="是否通过")
    # 各维度得分以 JSON 文本存储，避免为不同 judge 频繁改表
    dimensions: Mapped[str | None] = mapped_column(Text, comment="各维度得分JSON")
    reason: Mapped[str | None] = mapped_column(Text, comment="判定理由")
    answer: Mapped[str | None] = mapped_column(Text, comment="被评估答案快照")
    duration_ms: Mapped[int | None] = mapped_column(Integer, comment="评估耗时(毫秒)")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
