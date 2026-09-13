"""
MySQL 客户端管理器

统一创建和管理项目中的异步 MySQL 客户端，当前项目会同时连接两套 MySQL
一套是保存结构化元数据的 meta 数据库，一套是模拟教学数仓的 dw 数据库
模块对外提供可复用的客户端管理器和 session 工厂
方便脚本入口 服务层和仓储层按统一方式访问数据库
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.conf.app_config import DBConfig, app_config


class MySQLClientManager:
    """管理 MySQL Engine 和 Session 工厂"""

    # 构造函数：保存数据库配置并预留 Engine 与 Session 工厂字段；参数 config=含 host/port/账号/库名的 DBConfig
    def __init__(self, config: DBConfig):
        # Engine 是数据库连接层核心对象，底层会维护连接池
        self.engine: AsyncEngine | None = None
        # session_factory 用来按需创建新的 AsyncSession
        self.session_factory = None
        # 保存数据库配置，后面拼接连接地址要用
        self.config = config

    # 按配置拼接 MySQL 异步连接 URL（asyncmy 驱动）并返回；参数：无(使用 self.config)
    def _get_url(self) -> str:
        """
        拼接 MySQL 异步连接地址
        mysql+asyncmy 表示：连接 MySQL，并使用 asyncmy 作为异步驱动
        """
        return f"mysql+asyncmy://{self.config.user}:{self.config.password}@{self.config.host}:{self.config.port}/{self.config.database}?charset=utf8mb4"

    # 创建异步 Engine 与 Session 工厂，供仓储层按需取会话；参数：无
    def init(self):
        """初始化 Engine 和 Session 工厂"""
        # 创建异步 Engine，相当于先把“数据库连接能力”准备好
        self.engine = create_async_engine(
            self._get_url(), pool_size=10, pool_pre_ping=True
        )
        # 基于 Engine 创建 Session 工厂，后面真正查库时再拿 session
        self.session_factory = async_sessionmaker(
            self.engine, autoflush=True, expire_on_commit=False
        )

    # 释放 Engine 持有的连接池资源，供应用关闭阶段调用；参数：无
    async def close(self):
        """释放连接池资源"""
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            # Engine 已释放，同步清空基于它的 Session 工厂，保证幂等
            self.session_factory = None


# 一套连元数据库，一套连数仓模拟库
# 后续由不同 repository 按职责分别使用
meta_mysql_client_manager = MySQLClientManager(app_config.db_meta)
dw_mysql_client_manager = MySQLClientManager(app_config.db_dw)

if __name__ == "__main__":
    dw_mysql_client_manager.init()

    # 本地调试：执行一次简单 SQL 查询，验证 MySQL 连接与结果行结构；参数：无
    async def test():
        """执行一次简单查询，验证 MySQL 连接与结果结构"""
        async with dw_mysql_client_manager.session_factory() as session:
            sql = "select * from fact_order limit 10"
            result = await session.execute(text(sql))
            # mappings().fetchall() 会把结果转成“按列名访问”的行对象列表
            rows = result.mappings().fetchall()
            print(type(rows))
            print(type(rows[0]))
            print(rows[0]["order_id"])

    asyncio.run(test())
