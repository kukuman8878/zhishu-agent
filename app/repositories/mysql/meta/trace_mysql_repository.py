"""
查询轨迹 MySQL 仓储

负责查询轨迹在 Meta MySQL 中的写入与查询：
  - 写入：每次问答结束后把执行轨迹快照落库（供复盘与评估集构建）
  - 查询：按时间倒序分页浏览轨迹
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.entities.query_trace import QueryTrace
from app.models.query_trace import QueryTraceMySQL
from app.repositories.mysql.meta.mappers.query_trace_mapper import QueryTraceMapper


class TraceMySQLRepository:
    """负责查询轨迹的落库与查询"""

    # 构造函数：绑定元数据库会话供持久化与查询；参数 session=SQLAlchemy 异步会话
    def __init__(self, session: AsyncSession):
        self.session = session

    # 把轨迹业务实体加入会话待提交(ORM 转换走 QueryTraceMapper)；参数 trace=轨迹业务实体
    def save(self, trace: QueryTrace):
        """保存单条查询轨迹。输入是业务实体，转换统一通过 Mapper 完成"""
        self.session.add(QueryTraceMapper.to_model(trace))

    # 分页列出查询轨迹（按创建时间倒序），供复盘与评估使用；参数 limit=条数上限，offset=偏移量
    async def list_traces(self, limit: int = 20, offset: int = 0) -> list[QueryTrace]:
        """分页列出查询轨迹（按创建时间倒序）"""
        result = await self.session.execute(
            text(
                "SELECT * FROM query_trace ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        traces: list[QueryTrace] = []
        for row in result.mappings().fetchall():
            # 原始行里的 sql 列要换成 ORM 属性名 trace_sql 再构造模型
            data = dict(row)
            data["trace_sql"] = data.pop("sql", None)
            traces.append(QueryTraceMapper.to_entity(QueryTraceMySQL(**data)))
        return traces
