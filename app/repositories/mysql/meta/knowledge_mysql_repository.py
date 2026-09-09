"""
知识沉淀 MySQL 仓储

负责知识条目在 Meta MySQL 中的写入与查询：
  - 写入：新条目落库（事务提交由 Service 层控制）
  - 复用命中：hit_count + 1
  - 去重刷新：相似问题已存在时更新答案/SQL 而不是新增
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.entities.knowledge_item import KnowledgeItem
from app.models.knowledge_item import KnowledgeItemMySQL
from app.repositories.mysql.meta.mappers.knowledge_item_mapper import (
    KnowledgeItemMapper,
)


class KnowledgeMySQLRepository:
    """负责知识条目的落库、命中统计与查询"""

    # 构造函数：绑定元数据库会话供持久化与查询；参数 session=SQLAlchemy 异步会话
    def __init__(self, session: AsyncSession):
        self.session = session

    # 把知识条目业务实体加入会话待提交(ORM 转换走 KnowledgeItemMapper)；参数 item=知识条目实体
    def save(self, item: KnowledgeItem):
        """保存单条知识条目。输入是业务实体，转换统一通过 Mapper 完成"""
        self.session.add(KnowledgeItemMapper.to_model(item))

    # 知识复用命中时把对应条目 hit_count 加一；参数 item_id=知识条目主键 id
    async def increment_hit_count(self, item_id: str):
        """知识被复用命中时累加命中次数"""
        await self.session.execute(
            text(
                "UPDATE knowledge_item SET hit_count = hit_count + 1 WHERE id = :item_id"
            ),
            {"item_id": item_id},
        )
        await self.session.commit()

    # 相似问题去重时刷新已有条目的答案与 SQL（保持 id/问题不变）；参数 item_id=条目 id，answer=新答案，sql=新 SQL(可空)
    async def refresh(self, item_id: str, answer: str, sql: str | None):
        """更新已有知识条目的答案与 SQL，用于相似问题去重合并"""
        await self.session.execute(
            text(
                "UPDATE knowledge_item SET answer = :answer, `sql` = :sql WHERE id = :item_id"
            ),
            {"item_id": item_id, "answer": answer, "sql": sql},
        )
        await self.session.commit()

    # 按 id 查询单条知识条目并转成实体返回；参数 item_id=知识条目主键 id
    async def get_by_id(self, item_id: str) -> KnowledgeItem | None:
        """按条目 id 查询知识条目，供浏览与验证使用"""
        item: KnowledgeItemMySQL | None = await self.session.get(
            KnowledgeItemMySQL, item_id
        )
        return KnowledgeItemMapper.to_entity(item) if item else None

    # 分页列出知识条目（按更新时间倒序），供知识库浏览使用；参数 limit=条数上限，offset=偏移量
    async def list_items(self, limit: int = 20, offset: int = 0) -> list[KnowledgeItem]:
        """分页列出知识条目（按更新时间倒序）"""
        result = await self.session.execute(
            text(
                "SELECT * FROM knowledge_item ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        items: list[KnowledgeItem] = []
        for row in result.mappings().fetchall():
            # 原始行里的 sql 列要换成 ORM 属性名 knowledge_sql 再构造模型
            data = dict(row)
            data["knowledge_sql"] = data.pop("sql", None)
            items.append(KnowledgeItemMapper.to_entity(KnowledgeItemMySQL(**data)))
        return items
