"""
知识复用子 Agent（LLMWiki 式）

封装知识库召回：把问题向量化后在 Qdrant knowledge_collection 检索相似沉淀问题，
命中且通过口径守卫时直接返回存量答案。orchestrator 把它作为"知识门"最先调用，
命中即短路，不再下发 sql/doc 子任务。
"""

from langgraph.runtime import Runtime

from app.agent.agents.base import AgentResult
from app.agent.context import DataAgentContext
from app.agent.tool_loop import collect_tool_supplement
from app.clients.mcp_client_manager import mcp_client_manager
from app.conf.app_config import app_config
from app.core.log import logger
from app.core.text_utils import (
    has_future_year,
    same_metric_domain,
    same_numbers,
    same_region_scope,
)


class KnowledgeAgent:
    """知识复用子 Agent：命中沉淀问题则复用答案"""

    name = "knowledge"

    # 执行知识库召回，命中返回 AgentResult(answer/item_id/score/question)；参数 question=用户问题，runtime=运行上下文，history 未使用
    async def run(
        self, question: str, runtime: Runtime[DataAgentContext], history: str = ""
    ) -> AgentResult:
        if not app_config.knowledge.enabled:
            return AgentResult(agent=self.name, ok=False, error="disabled")
        # 未来年份跳过复用：沉淀答案携带历史时间口径，复用到未来会答错
        if has_future_year(question):
            return AgentResult(agent=self.name, ok=False, error="future_year")
        try:
            repository = runtime.context["knowledge_qdrant_repository"]
            embedding_client = runtime.context["embedding_client"]
            # 专属工具预处理：归属 knowledge 的 MCP 工具补充信息只用于增强检索向量，
            # 不参与口径守卫比较（守卫仍以原始用户问题为准，避免外部数字误伤复用）
            recall_text = question
            tools = await mcp_client_manager.get_tools(self.name)
            if tools:
                supplement = await collect_tool_supplement(question, self.name, tools)
                if supplement:
                    recall_text = f"{question}\n{supplement}"
            # 知识集合由沉淀服务按需创建；首问时集合不存在，保证检索不抛异常
            await repository.ensure_collection()
            vector = await embedding_client.aembed_query(recall_text)
            results = await repository.search(
                vector,
                score_threshold=app_config.knowledge.recall_score_threshold,
                # 多取候选：口径守卫可能过滤掉最高分那条（年份/季度/指标词不同）
                limit=max(app_config.knowledge.recall_limit, 3),
            )
            for item, score in results:
                # 口径守卫：数字序列/指标域/地区不一致则不复用（bge 对数字/单业务词/地区不敏感）
                if (
                    not same_numbers(question, item.question)
                    or not same_metric_domain(question, item.question)
                    or not same_region_scope(question, item.question)
                ):
                    logger.info(
                        f"知识复用跳过（口径不一致）：query={question[:30]} hit={item.question[:30]}"
                    )
                    continue
                logger.info(
                    f"知识库命中复用：item_id={item.id} score={score:.3f} query={question[:40]}"
                )
                return AgentResult(
                    agent=self.name,
                    ok=True,
                    payload={
                        "answer": item.answer,
                        "item_id": item.id,
                        "score": score,
                        "question": item.question,
                    },
                )
            return AgentResult(agent=self.name, ok=False, error="no_hit")
        except Exception as e:
            # fail-open：知识召回异常不阻断编排，退化为正常下发子任务
            logger.warning(f"知识子 Agent 召回失败：{e}")
            return AgentResult(agent=self.name, ok=False, error=str(e)[:200])
