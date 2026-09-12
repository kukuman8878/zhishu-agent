"""
问答评估 MySQL 仓储

负责结果评估在 Meta MySQL 中的写入与查询：
  - ensure_table：惰性建表（CREATE TABLE IF NOT EXISTS），兼容未重建 volume 的旧库；
  - save：落一条评估记录；
  - list_evals：按时间倒序分页浏览，供质量复盘使用。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.entities.query_eval import QueryEval
from app.models.query_eval import QueryEvalMySQL
from app.repositories.mysql.meta.mappers.query_eval_mapper import QueryEvalMapper

# query_eval 建表语句：与 docker/mysql/meta.sql 保持一致；运行时 IF NOT EXISTS 兜底
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS query_eval
(
    id          VARCHAR(64) PRIMARY KEY COMMENT '评估编号',
    session_id  VARCHAR(128) COMMENT '会话标识',
    query       TEXT COMMENT '用户问题',
    route       VARCHAR(16) COMMENT '最终路由',
    judge       VARCHAR(64) COMMENT '判定器名称',
    score       FLOAT COMMENT '综合分0~1',
    passed      TINYINT(1) COMMENT '是否通过',
    dimensions  TEXT COMMENT '各维度得分JSON',
    reason      TEXT COMMENT '判定理由',
    answer      TEXT COMMENT '被评估答案快照',
    duration_ms INT COMMENT '评估耗时(毫秒)',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
)
"""


class EvalMySQLRepository:
    """负责问答评估记录的落库、建表与查询"""

    # 类级建表标记：一个进程内只需确保一次
    _table_ready = False

    # 构造函数：绑定元数据库会话；参数 session=SQLAlchemy 异步会话
    def __init__(self, session: AsyncSession):
        self.session = session

    # 惰性确保评估表存在（旧 volume 也能直接启用评估）；参数：无
    async def ensure_table(self):
        """确保 query_eval 表存在（幂等）"""
        if EvalMySQLRepository._table_ready:
            return
        await self.session.execute(text(_CREATE_TABLE_SQL))
        await self.session.commit()
        EvalMySQLRepository._table_ready = True

    # 把评估业务实体加入会话待提交（ORM 转换走 QueryEvalMapper）；参数 query_eval=评估业务实体
    def save(self, query_eval: QueryEval):
        """保存单条评估记录"""
        self.session.add(QueryEvalMapper.to_model(query_eval))

    # 分页列出评估记录（按创建时间倒序）；参数 limit=条数上限，offset=偏移量
    async def list_evals(self, limit: int = 20, offset: int = 0) -> list[QueryEval]:
        """分页列出评估记录（按创建时间倒序）"""
        result = await self.session.execute(
            text(
                "SELECT * FROM query_eval ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        evals: list[QueryEval] = []
        for row in result.mappings().fetchall():
            evals.append(QueryEvalMapper.to_entity(QueryEvalMySQL(**dict(row))))
        return evals
