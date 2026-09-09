"""
问数接口请求体定义

集中声明 API 层输入输出的数据结构，让路由函数只处理业务流程，
字段校验和 OpenAPI 文档生成交给 Pydantic 与 FastAPI 完成。
"""

from pydantic import BaseModel, Field


class QuerySchema(BaseModel):
    """`/api/query` 请求体，承载用户输入的自然语言问题与会话标识"""

    # 前端请求体中的 query 字段，例如 {"query": "统计华北地区销售额"}
    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="用户自然语言问题，不超过 500 字符",
    )
    # 可选会话标识：同一 session_id 的多轮提问共享对话历史（支持"那按月呢"这类追问）；
    # 不传时每次提问视为独立会话
    session_id: str | None = Field(
        None,
        max_length=128,
        description="会话标识，同一会话的多轮提问共享上下文",
    )
