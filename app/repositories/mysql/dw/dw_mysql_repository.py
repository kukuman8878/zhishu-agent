"""
数仓 MySQL 仓储

这一层对应文档里的 DW Repository，职责是到真实数仓中补齐配置文件里
没有显式维护的信息，例如字段类型和字段示例值。Service 层只关心
"需要哪些信息"，具体怎样查数仓由仓储层统一封装
SQL 生成闭环中的数据库环境读取 SQL 校验和最终查询执行也集中放在这里
"""

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 表名/字段名白名单正则：只允许字母、数字、下划线和点号（用于 table.column 组合 id）
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_.]+$")


def _assert_safe_identifier(name: str, label: str = "identifier") -> None:
    """校验表名/字段名是否安全，防止 SQL 注入；参数 name=待校验名称, label=报错标签"""
    if not name or not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"不安全的{label}：{name!r}（仅允许字母、数字、下划线和点号）")


class DWMySQLRepository:
    """负责查询数仓真实表结构和字段样例值"""

    # 构造函数：绑定数仓(dw)数据库会话供后续查询使用；参数 session=SQLAlchemy 异步会话
    def __init__(self, session: AsyncSession):
        self.session = session

    # 查询整张表的字段类型映射(字段名->类型)，作为字段实体 type 的真实来源；参数 table_name=目标表名
    async def get_column_types(self, table_name: str) -> dict[str, str]:
        """查询整张表的字段类型，作为 ColumnInfo.type 的真实来源"""
        _assert_safe_identifier(table_name, "表名")
        # SHOW COLUMNS 不支持参数化占位符，使用白名单校验后安全拼接
        sql = text(f"SHOW COLUMNS FROM `{table_name}`")
        result = await self.session.execute(sql)
        result_dict = result.mappings().fetchall()
        return {row["Field"]: row["Type"] for row in result_dict}

    # 抽样查询某字段的去重示例值列表，供元数据入库与检索链路使用；参数 table_name=表名，column_name=字段名，limit=取样条数上限
    async def get_column_values(
        self, table_name: str, column_name: str, limit: int = 10
    ) -> list:
        """抽样查询字段示例值，供元数据入库和后续检索链路复用"""
        _assert_safe_identifier(table_name, "表名")
        _assert_safe_identifier(column_name, "字段名")
        # 列名不支持参数化占位符，白名单校验后安全拼接；limit 使用参数化
        sql = text(f"SELECT DISTINCT `{column_name}` FROM `{table_name}` LIMIT :limit")
        result = await self.session.execute(sql, {"limit": limit})
        return [row[0] for row in result.fetchall()]

    # 读取当前数仓的数据库方言与版本并返回字典，供 SQL 生成提示词使用；参数：无
    async def get_db_info(self):
        """读取当前数仓数据库的方言和版本，供 SQL 生成提示词使用"""

        sql = "select version()"
        result = await self.session.execute(text(sql))
        version = result.scalar()

        # dialect 来自 SQLAlchemy 当前绑定的数据库方言，例如 mysql
        dialect = self.session.bind.dialect.name
        return {"dialect": dialect, "version": version}

    # 用 EXPLAIN 让数据库预解析 SQL 以校验语法/表名/字段名，不返回结果；参数 sql=待校验的 SQL 语句
    async def validate(self, sql: str):
        """用 EXPLAIN 让数据库提前解析 SQL，发现语法 表名 字段名等错误"""
        # validate 的输入本身就是 LLM 生成的完整 SQL，无法参数化；此处做基本格式校验
        normalized = sql.strip().rstrip(";")
        if not normalized:
            raise ValueError("空 SQL 语句")
        await self.session.execute(text(f"EXPLAIN {normalized}"))

    # 执行最终 SQL 并把行对象转为字典列表返回，供节点发 result 事件与 hybrid 综合；参数 sql=待执行 SQL
    async def run(self, sql: str) -> list[dict]:
        """执行最终 SQL，并把 SQLAlchemy 行对象转换成前端更易消费的字典列表"""
        normalized = sql.strip().rstrip(";")
        if not normalized:
            raise ValueError("空 SQL 语句")
        result = await self.session.execute(text(normalized))
        return [dict(row) for row in result.mappings().fetchall()]
