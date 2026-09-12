"""
MCP 客户端管理器

用途：按 conf/app_config.yaml 的 mcp.servers 配置连接一个或多个 MCP Server，
把远端工具转换成 LangChain 工具列表，供各子 Agent 绑定使用。

专属工具：每个 MCP Server 通过 `agents` 字段声明归属哪些子 Agent（chat/sql/doc/
knowledge）；`get_tools(agent)` 只返回该 Agent 专属的服务工具，并按 Agent 分别缓存。
未声明 `agents` 时默认归给 chat，兼容旧配置。这样 sql/doc/knowledge 各自可以有专属的
外部工具，而不会互相串用。

设计要点：
  - 懒加载 + 进程内缓存：首次使用时才连接外部 MCP Server，避免拖慢服务启动；
  - fail-open：任一 Server 加载失败只记警告并跳过，绝不影响对应 Agent 主流程；
  - 无持久会话：复用 langchain-mcp-adapters 的无状态调用方式（每次工具调用内部
    自行建立/释放 MCP 会话），因此本管理器无需维护长连接。
"""

import asyncio
import json
import sys
from typing import Optional

from omegaconf import OmegaConf

from app.conf.app_config import app_config, project_root
from app.core.log import logger


class MCPClientManager:
    """管理 MCP 工具（LangChain BaseTool 列表）的懒加载与缓存"""

    # 构造函数：初始化分 Agent 工具缓存与并发锁，避免并发请求重复加载；参数：无
    def __init__(self):
        # agent 名 → 该 Agent 专属工具列表；键 "__all__" 表示不按 Agent 过滤
        self._tools: dict[str, list] = {}
        self._lock = asyncio.Lock()

    # 把配置里的 servers 规整成纯 dict（过滤停用项并按 agent 归属筛选），便于传给 MultiServerMCPClient；
    # 参数 agent=目标子 Agent 名（None 表示不过滤）
    def _enabled_connections(self, agent: Optional[str] = None) -> dict:
        """返回 {服务名: 连接配置} 纯字典；仅保留归属该 agent 的服务"""

        raw = app_config.mcp.servers or {}
        # app_config 已 to_object 成普通 dict；这里兼容仍是 DictConfig 的场景
        if OmegaConf.is_config(raw):
            servers = OmegaConf.to_container(raw, resolve=True) or {}
        else:
            servers = raw
        connections = {}
        for name, conn in servers.items():
            if not conn:
                continue
            if not conn.get("enabled", True):
                continue
            # agents 声明归属；缺省归 chat，兼容旧配置（旧配置只有闲聊用工具）
            owners = conn.get("agents", ["chat"])
            if isinstance(owners, str):
                owners = [owners]
            if agent is not None and agent not in owners:
                continue
            # enabled / use_current_python / agents 只是本项目的开关，不属于 MCP 连接参数
            use_current_python = conn.get("use_current_python", False)
            conn = {
                k: v
                for k, v in conn.items()
                if k not in ("enabled", "use_current_python", "agents")
            }
            # use_current_python=true 时用后端自身解释器启动子进程，避免 PATH 里
            # 的 python 不是装了 mcp 依赖的虚拟环境
            if use_current_python:
                conn["command"] = sys.executable
            # stdio 子进程默认从项目根目录启动，便于用仓库内相对路径引用脚本
            if conn.get("transport") == "stdio" and not conn.get("cwd"):
                conn["cwd"] = str(project_root)
            connections[name] = conn
        return connections

    # 逐个 MCP Server 加载工具，失败仅跳过；返回 LangChain 工具列表；参数 agent=目标子 Agent 名（None 表示全部）
    async def _load_tools(self, agent: Optional[str] = None) -> list:
        """连接归属该 Agent 的 MCP Server 并加载工具，单点失败不影响其他服务"""

        if not app_config.mcp.enabled:
            return []

        connections = self._enabled_connections(agent)
        if not connections:
            logger.info(
                f"MCP 已启用但 agent={agent or 'all'} 无配置 server，不绑定外部工具"
            )
            return []

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except Exception as e:
            logger.warning(f"langchain-mcp-adapters 不可用，跳过 MCP 工具加载：{e}")
            return []

        client = MultiServerMCPClient(
            connections,
            # 工具内部异常交由适配器转成错误文本回喂模型，而不是抛断整轮回答
            handle_tool_errors=True,
        )

        tools: list = []
        for name in connections:
            try:
                loaded = await client.get_tools(server_name=name)
                tools.extend(loaded)
                logger.info(f"MCP server [{name}] 加载工具 {len(loaded)} 个")
            except Exception as e:
                # fail-open：单个 server 连接/握手失败不影响其他 server 与闲聊
                logger.warning(f"MCP server [{name}] 工具加载失败，已跳过：{e}")
        return tools

    # 获取某子 Agent 的专属 MCP 工具（首次调用加载并缓存）；参数 agent=子 Agent 名（None 取全部）
    async def get_tools(self, agent: Optional[str] = None) -> list:
        """返回缓存中该 Agent 的专属工具；未加载时先加载一次"""

        if not app_config.mcp.enabled:
            return []
        key = agent or "__all__"
        if key in self._tools:
            return self._tools[key]
        async with self._lock:
            # 双重检查：并发进入时只有一个协程真正加载
            if key not in self._tools:
                self._tools[key] = await self._load_tools(agent)
            return self._tools[key]

    # 关闭管理器并清空缓存（工具为无状态调用，无需释放长连接）；参数：无
    async def close(self):
        """清空工具缓存，供应用关闭时调用"""
        self._tools = {}


# 模块级单例：与 llm 类似，供各子 Agent 按名称取用专属 MCP 工具
mcp_client_manager = MCPClientManager()


if __name__ == "__main__":
    # 本地调试：按 Agent 加载 MCP 工具并打印名称与描述，验证配置与归属；参数：无
    async def test():
        """加载各 Agent 的 MCP 工具并打印，便于本地验证配置"""

        for agent in ("chat", "sql", "doc", "knowledge"):
            tools = await mcp_client_manager.get_tools(agent)
            names = [tool.name for tool in tools]
            print(agent, "→", names)
            for tool in tools:
                print("   ", tool.name, "-", (tool.description or "")[:60])
        print(
            json.dumps(
                {"agents": ["chat", "sql", "doc", "knowledge"]}, ensure_ascii=False
            )
        )

    asyncio.run(test())
