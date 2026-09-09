"""
进程内文档问答引擎管理器

文档问答引擎源码位于本仓库 app/rag_engine/（源自外部比赛方案的 agentic RAG 代码，
现已整体融入本进程运行），不再作为独立服务部署、也不再经 HTTP 外接。这里负责
**进程内直接装载索引 + 创建 Agent**，供 doc/hybrid 路由的节点调用。

职责：
  - init()：按 INDEX_DIR 装载 402M 检索索引，创建 Agent（供 lifespan 启动时调用）
  - answer(question)：把问题直接交给进程内 Agent，返回归一化 dict
    {answer, query_time_s, rounds, route, cited_pages, error}，
    保证 doc_query/hybrid 分支消费逻辑稳定
  - close()：释放进程内资源（当前 Agent 无独立连接池，置空即可）
  - enabled 属性保留：配置开关 + 已初始化双重判定

注意：装载索引约 0.5s，Agent 内嵌 VLM/embedding 客户端均为惰性 HTTP 调用，
真正联网发生在首次文档问答时，因此启动快、不常驻外连。
"""

import time
from typing import Optional

from app.conf.app_config import DocEngineConfig, app_config
from app.core.log import logger
from app.rag_engine.agent.agent import Agent
from app.rag_engine.config import settings
from app.rag_engine.retrieval.recall import IndexStore


class DocEngineClient:
    """封装一次进程内文档问答调用"""

    # 构造函数：保存文档问答配置并预留引擎字段；参数 config=含 enabled 的 DocEngineConfig
    def __init__(self, config: DocEngineConfig):
        self._config = config
        self._store: Optional[IndexStore] = None
        self._agent: Optional[Agent] = None

    # 装载索引并创建 Agent（幂等），供 lifespan 启动阶段调用；参数：无
    def init(self):
        """进程内装载检索索引并创建文档问答 Agent（幂等）"""
        if self._agent is not None:
            return
        logger.info(f"装载文档问答索引：{settings.index_dir}")
        self._store = IndexStore(settings.index_dir)
        self._agent = Agent(self._store, use_vlm_router=True)
        logger.info("文档问答 Agent 初始化完成")

    # 把单个问题交给进程内 Agent，返回归一化载荷；参数 question=用户问句
    async def answer(self, question: str) -> dict:
        """进程内文档问答，返回归一化 dict

        引擎内部异常不抛出，返回 answer=NOT_FOUND + error 字段，
        由调用方决定降级文案，避免单题异常中断会话。
        """
        if self._agent is None:
            raise RuntimeError("DocEngineClient 尚未 init")

        start = time.perf_counter()
        try:
            answer, trace = await self._agent.answer(question)
            elapsed = time.perf_counter() - start
            return {
                "answer": answer,
                "query_time_s": round(elapsed, 3),
                "rounds": trace.rounds,
                "route": {
                    "question_type": trace.route.question_type if trace.route else None,
                    "answer_type": trace.route.answer_type if trace.route else None,
                },
                "cited_pages": [e.page_idx + 1 for e in trace.evidence],
                "error": "",
            }
        except Exception as e:
            logger.warning(f"文档问答引擎异常，返回 NOT_FOUND：{e}")
            elapsed = time.perf_counter() - start
            return {
                "answer": "NOT_FOUND",
                "query_time_s": round(elapsed, 3),
                "rounds": 0,
                "route": {},
                "cited_pages": [],
                "error": str(e)[:300],
            }

    # 释放进程内引擎引用，供应用关闭阶段调用；参数：无
    async def close(self):
        """释放引擎（Agent 无独立连接池，置空即可）"""
        self._agent = None
        self._store = None


class DocEngineClientManager:
    """管理进程内文档问答引擎的初始化与复用"""

    # 构造函数：保存文档问答配置并预留 engine 字段；参数 config=DocEngineConfig 配置对象
    def __init__(self, config: DocEngineConfig):
        self.client: Optional[DocEngineClient] = None
        self.config = config

    # 只读属性：是否启用文档问答(配置开关 + 引擎已初始化双重判定)；参数：无
    @property
    def enabled(self) -> bool:
        """是否启用文档问答（配置开关 + 已完成初始化双重判定）"""
        return self.config.enabled and self.client is not None

    # 按配置创建并初始化进程内引擎(幂等)，供生命周期启动阶段调用；参数：无
    def init(self):
        """创建进程内文档问答引擎（幂等）"""
        if not self.config.enabled:
            logger.info("doc_engine.enabled=false，跳过文档引擎装载")
            return
        self.client = DocEngineClient(self.config)
        self.client.init()

    # 关闭引擎并置空，供应用关闭阶段释放资源；参数：无
    async def close(self):
        """释放进程内文档问答引擎"""
        if self.client is not None:
            await self.client.close()
            self.client = None


# 模块级单例，供图节点和依赖注入复用同一套进程内文档问答引擎
doc_engine_client_manager = DocEngineClientManager(app_config.doc_engine)


if __name__ == "__main__":
    # 本地最小化验证：装载索引后直接问进程内引擎一个问题
    doc_engine_client_manager.init()

    # 本地最小化验证：直接问文档引擎一个问题以检查是否可用；参数：无
    async def test():
        try:
            result = await doc_engine_client_manager.client.answer(
                "统计华北地区销售总额"
            )
            print(result)
        except Exception as e:
            print(f"调用失败：{e}")

    import asyncio

    asyncio.run(test())
