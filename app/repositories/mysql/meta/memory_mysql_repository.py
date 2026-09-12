"""
长期记忆 MySQL 仓储（摘要记忆 + 用户记忆）

用一张通用表 agent_memory 承载两类记忆，避免为每种记忆建表：
  - memory_type='summary'，scope_id=session_id：每个会话一条滚动摘要；
  - memory_type='user'，scope_id=user_id：用户偏好/画像事实，多条。

工作记忆（会话消息）由 SQLite checkpointer 负责，知识记忆复用 knowledge_item，均不在本表。
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# agent_memory 建表语句：与 docker/mysql/meta.sql 保持一致；运行时 IF NOT EXISTS 兜底
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_memory
(
    id          VARCHAR(64) PRIMARY KEY COMMENT '记忆编号',
    memory_type VARCHAR(16) NOT NULL COMMENT '记忆类型(summary/user)',
    scope_id    VARCHAR(128) NOT NULL COMMENT '作用域(session_id/user_id)',
    content     TEXT COMMENT '记忆内容(摘要正文/用户偏好事实)',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_memory_type_scope (memory_type, scope_id)
)
"""


class MemoryMySQLRepository:
    """负责摘要记忆与用户记忆在 Meta MySQL 中的读写"""

    # 类级建表标记：一个进程内只需确保一次
    _table_ready = False

    # 构造函数：绑定元数据库会话；参数 session=SQLAlchemy 异步会话
    def __init__(self, session: AsyncSession):
        self.session = session

    # 惰性确保记忆表存在（旧 volume 也能直接启用）；参数：无
    async def ensure_table(self):
        """确保 agent_memory 表存在（幂等）"""
        if MemoryMySQLRepository._table_ready:
            return
        await self.session.execute(text(_CREATE_TABLE_SQL))
        await self.session.commit()
        MemoryMySQLRepository._table_ready = True

    # 读取会话摘要；参数 session_id=会话标识，返回摘要文本或 None
    async def get_summary(self, session_id: str) -> str | None:
        """读取指定会话最近一条摘要"""
        result = await self.session.execute(
            text(
                "SELECT content FROM agent_memory "
                "WHERE memory_type='summary' AND scope_id=:scope "
                "ORDER BY updated_at DESC LIMIT 1"
            ),
            {"scope": session_id},
        )
        row = result.fetchone()
        return row[0] if row and row[0] else None

    # 写入/刷新会话摘要（存在则更新，否则插入）；参数 session_id=会话标识，content=摘要正文
    async def upsert_summary(self, session_id: str, content: str):
        """按会话写入或刷新摘要（每个会话只保留一条）"""
        result = await self.session.execute(
            text(
                "SELECT id FROM agent_memory "
                "WHERE memory_type='summary' AND scope_id=:scope LIMIT 1"
            ),
            {"scope": session_id},
        )
        row = result.fetchone()
        if row:
            await self.session.execute(
                text(
                    "UPDATE agent_memory SET content=:content, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=:id"
                ),
                {"content": content, "id": row[0]},
            )
        else:
            await self.session.execute(
                text(
                    "INSERT INTO agent_memory (id, memory_type, scope_id, content) "
                    "VALUES (:id, 'summary', :scope, :content)"
                ),
                {"id": uuid.uuid4().hex, "scope": session_id, "content": content},
            )

    # 列出用户偏好事实（按更新时间倒序，最多 limit 条）；参数 user_id=用户标识，limit=条数上限
    async def list_user_facts(self, user_id: str, limit: int = 8) -> list[str]:
        """读取用户最近的事实/偏好列表"""
        result = await self.session.execute(
            text(
                "SELECT content FROM agent_memory "
                "WHERE memory_type='user' AND scope_id=:scope "
                "ORDER BY updated_at DESC LIMIT :limit"
            ),
            {"scope": user_id, "limit": limit},
        )
        return [row[0] for row in result.fetchall() if row[0]]

    # 追加用户偏好事实（完全相同的内容去重）；参数 user_id=用户标识，facts=事实文本列表
    async def add_user_facts(self, user_id: str, facts: list[str]):
        """追加用户偏好事实，已存在的完全重复内容不重复写入"""
        if not facts:
            return
        existing = set(await self.list_user_facts(user_id, limit=200))
        for fact in facts:
            fact = fact.strip()
            if not fact or fact in existing:
                continue
            await self.session.execute(
                text(
                    "INSERT INTO agent_memory (id, memory_type, scope_id, content) "
                    "VALUES (:id, 'user', :scope, :content)"
                ),
                {"id": uuid.uuid4().hex, "scope": user_id, "content": fact},
            )
            existing.add(fact)
