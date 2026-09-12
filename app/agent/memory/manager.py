"""
记忆管理器

统一编排四类记忆的读写：
  - 工作记忆：由 SQLite checkpointer 自动持久化，本管理器不直接介入（见 working.py）；
  - 摘要记忆：消息条数超阈值时用 chat_llm 滚动生成会话摘要并落库；
  - 用户记忆：从对话抽取稳定偏好/画像事实并落库，跨会话复用；
  - 知识记忆：复用现有知识沉淀（knowledge_item + Qdrant），由知识门/知识子 Agent 处理。

对外只暴露 load_context（注入提示词）与 update_after_turn（更新摘要/用户事实），
全部旁路：异常只记日志，绝不影响问答主流程。
"""

import re

from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
from langchain_core.prompts import PromptTemplate

from app.agent.history import render_history
from app.agent.llm import chat_llm
from app.agent.llm_utils import retry_async
from app.agent.memory import MemoryContext
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 偏好线索词：问题含这些词才值得调模型抽取用户画像（成本节流）
_PREFERENCE_CUE = re.compile(
    r"(我|我们|以后|默认|习惯|偏好|通常|一般|总是|记得|记住|别再|不用|帮我记住)"
)


class MemoryManager:
    """负责长期记忆（摘要 + 用户偏好）的加载与更新"""

    # 构造函数：绑定长期记忆仓储与知识召回能力；参数 repository=长期记忆 MySQL 仓储
    def __init__(self, repository):
        self.repository = repository

    # 加载指定会话/用户的长期记忆上下文；参数 session_id=会话标识，user_id=用户标识，返回 MemoryContext
    async def load_context(
        self, session_id: str | None, user_id: str | None
    ) -> MemoryContext:
        """加载会话摘要与用户偏好（失败退化为空上下文）"""

        if not app_config.memory.enabled:
            return MemoryContext()
        summary = ""
        facts: list[str] = []
        try:
            if session_id:
                summary = await self.repository.get_summary(session_id) or ""
            if app_config.memory.user_memory_enabled and user_id:
                facts = await self.repository.list_user_facts(
                    user_id, app_config.memory.user_memory_max_facts
                )
        except Exception as e:
            # 记忆加载失败不影响问答，退化为无记忆
            logger.warning(f"记忆加载失败（忽略）：{e}")
        return MemoryContext(summary=summary, user_facts=facts)

    # 一次问答结束后更新摘要与用户偏好；参数 session_id/user_id=作用域，messages=完整消息列表，query=本轮问题
    async def update_after_turn(
        self,
        session_id: str | None,
        user_id: str | None,
        messages: list | None,
        query: str,
    ):
        """问答结束后滚动摘要并按需抽取用户偏好（失败只记日志）"""

        if not app_config.memory.enabled or not messages:
            return
        try:
            await self.repository.ensure_table()
            await self._update_summary(session_id, messages)
            if app_config.memory.user_memory_enabled and user_id:
                await self._update_user_facts(user_id, messages, query)
            await self.repository.session.commit()
        except Exception as e:
            logger.warning(f"记忆更新失败（不影响问答）：{e}")

    # 消息达到阈值时滚动生成会话摘要；参数 session_id=会话标识，messages=完整消息列表
    async def _update_summary(self, session_id: str | None, messages: list):
        """达到阈值才摘要，否则跳过（省模型调用）"""

        if not session_id:
            return
        if len(messages) < app_config.memory.summary_trigger_messages:
            return
        try:
            prompt = PromptTemplate(
                template=load_prompt("memory_summarize"),
                input_variables=["history"],
            )
            chain = prompt | chat_llm | StrOutputParser()
            summary = await retry_async(
                chain.ainvoke,
                {"history": render_history(messages, limit=40)},
                node_name="memory_summarize",
            )
            summary = (summary or "").strip()
            if summary:
                await self.repository.upsert_summary(session_id, summary)
                logger.info(f"会话摘要已更新：session={session_id} len={len(summary)}")
        except Exception as e:
            logger.warning(f"会话摘要生成失败（忽略）：{e}")

    # 从对话抽取用户偏好事实并落库；参数 user_id=用户标识，messages=消息列表，query=本轮问题
    async def _update_user_facts(self, user_id: str, messages: list, query: str):
        """用 LLM 抽取稳定偏好事实并追加去重入库"""

        # 成本节流：问题无偏好线索时跳过本次抽取，省一次模型调用
        if app_config.memory.user_memory_cue_only and not _PREFERENCE_CUE.search(
            query or ""
        ):
            return
        try:
            prompt = PromptTemplate(
                template=load_prompt("memory_extract_user"),
                input_variables=["history", "query"],
            )
            chain = prompt | chat_llm | JsonOutputParser()
            raw = await retry_async(
                chain.ainvoke,
                {"history": render_history(messages, limit=20), "query": query},
                node_name="memory_extract_user",
            )
            if not isinstance(raw, list):
                return
            facts = [str(item).strip() for item in raw if str(item).strip()]
            if facts:
                await self.repository.add_user_facts(user_id, facts[:5])
                logger.info(f"用户偏好已更新：user={user_id} facts={facts[:5]}")
        except Exception as e:
            # 抽取失败（如模型未按 JSON 输出）不影响主流程
            logger.warning(f"用户偏好抽取失败（忽略）：{e}")
