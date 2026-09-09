"""
KnowledgeItem 映射器

负责在知识条目业务实体和 ORM 模型之间做双向转换，让 Service/Repository
不需要直接操作 SQLAlchemy 模型
"""

from app.entities.knowledge_item import KnowledgeItem
from app.models.knowledge_item import KnowledgeItemMySQL


class KnowledgeItemMapper:
    """负责 `KnowledgeItem` 与 `KnowledgeItemMySQL` 之间的双向转换"""

    # 把知识条目 ORM 模型还原为业务实体供上层流转；参数 knowledge_item_mysql=知识条目 ORM 模型对象
    @staticmethod
    def to_entity(knowledge_item_mysql: KnowledgeItemMySQL) -> KnowledgeItem:
        """把 ORM 模型还原成业务实体，便于上层业务逻辑继续流转"""
        return KnowledgeItem(
            id=knowledge_item_mysql.id,
            question=knowledge_item_mysql.question,
            answer=knowledge_item_mysql.answer,
            route=knowledge_item_mysql.route,
            sql=knowledge_item_mysql.knowledge_sql,
            hit_count=knowledge_item_mysql.hit_count,
        )

    # 把知识条目业务实体转成 ORM 模型交给 SQLAlchemy 托管；参数 knowledge_item=知识条目业务实体
    @staticmethod
    def to_model(knowledge_item: KnowledgeItem) -> KnowledgeItemMySQL:
        """把业务实体转换成 ORM 模型，交给 SQLAlchemy 托管"""
        return KnowledgeItemMySQL(
            id=knowledge_item.id,
            question=knowledge_item.question,
            answer=knowledge_item.answer,
            knowledge_sql=knowledge_item.sql,
            route=knowledge_item.route,
            hit_count=knowledge_item.hit_count,
        )
