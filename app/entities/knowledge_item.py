"""
知识条目业务实体

LLMWiki 式知识沉淀链路中，一次有价值的问答（sql/doc/hybrid 路由的最终结论）
会被沉淀成一条知识条目：问题文本进入 Qdrant 向量索引供语义复用，
答案/SQL/来源路由进入 Meta MySQL 供追溯与命中统计。
"""

from dataclasses import dataclass


@dataclass
class KnowledgeItem:
    """问答沉淀产生的知识条目业务表达"""

    id: str
    question: str
    answer: str
    route: str
    # 仅 sql 路由沉淀：生成/校正后的最终 SQL，供后续参考复用
    sql: str | None = None
    # 该知识被后续提问复用命中的次数
    hit_count: int = 0
