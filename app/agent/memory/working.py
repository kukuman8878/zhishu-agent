"""
工作记忆（短期）：会话消息持久化

把 LangGraph 检查点从进程内 InMemorySaver 换成 SQLite 文件持久化：
同一 thread_id（前端 session_id）的多轮消息写入磁盘，进程重启后仍可续聊。
memory.enabled=false 时退回 InMemorySaver（行为与旧版一致）。
"""

from pathlib import Path

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.conf.app_config import app_config, project_root
from app.core.log import logger

# 进程内持有 sqlite 连接与 saver，供 lifespan 初始化/关闭
_connection: aiosqlite.Connection | None = None
_saver: BaseCheckpointSaver | None = None


# 初始化工作记忆 checkpointer；参数：无，返回可挂到图上的 checkpointer
async def init_checkpointer() -> BaseCheckpointSaver:
    """按配置创建持久化 checkpointer（SQLite），未启用则退回内存版"""

    global _connection, _saver

    if not app_config.memory.enabled:
        logger.info("记忆总开关关闭：工作记忆退回 InMemorySaver（重启清空）")
        _saver = InMemorySaver()
        return _saver

    path = Path(app_config.memory.sqlite_path)
    if not path.is_absolute():
        path = project_root / path
    path.parent.mkdir(parents=True, exist_ok=True)

    _connection = await aiosqlite.connect(str(path))
    _saver = AsyncSqliteSaver(_connection)
    # 建表（幂等），确保会话状态表就绪
    await _saver.setup()
    logger.info(f"工作记忆已持久化到 SQLite：{path}")
    return _saver


# 关闭工作记忆连接；参数：无
async def close_checkpointer():
    """释放 SQLite 连接（应用关闭时调用）"""

    global _connection, _saver
    if _connection is not None:
        await _connection.close()
    _connection = None
    _saver = None
