"""
问答结果评估服务

职责：每次问答结束（SSE 流已返回终端答案）后，对终端答案做一次在线评估：
  1. 从图终态抽取评估样本（问题/路由/答案/证据）；
  2. 调度一组可插拔判定器（LLM-as-judge + 规则检查），按权重聚合成综合分；
  3. 结果落库到 query_eval 表，供质量监控与复盘。

评估是旁路能力：判定器异常、落库失败都只记日志，不影响已返回给用户的答案。
"""

import time
import uuid

from langchain_core.messages import AIMessage

from app.agent.judges import EvalSample, Judge, JudgeResult, build_judges
from app.agent.nodes.synthesize import _rows_to_markdown
from app.conf.app_config import app_config
from app.core.log import logger
from app.entities.query_eval import QueryEval

# 被评估答案快照入库前的最大长度（避免超长答案撑爆行）
_MAX_ANSWER_CHARS = 20000


class EvaluationService:
    """负责样本抽取、判定器调度、聚合与落库"""

    # 构造函数：绑定评估仓储并构建判定器组合；参数 eval_mysql_repository=评估落库仓储，judges=自定义判定器(默认按配置构建)
    def __init__(self, eval_mysql_repository, judges: list[Judge] | None = None):
        self.repository = eval_mysql_repository
        self.judges = judges if judges is not None else build_judges()

    # 取会话历史中最后一条 AI 文本（闲聊/兜底路由的终端答案）；参数 messages=图终态消息列表
    def _last_ai_content(self, messages) -> str:
        """从消息列表尾部找最后一条 AIMessage 的文本内容"""

        for message in reversed(messages or []):
            if isinstance(message, AIMessage):
                content = message.content
                return content if isinstance(content, str) else str(content)
        return ""

    # 按路由从图终态抽取评估样本；参数 final_state=图执行终态，返回 EvalSample 或 None(无可评答案)
    def extract_sample(self, final_state) -> EvalSample | None:
        """从终态抽取"问题/路由/答案/证据"，无可评内容返回 None"""

        route = final_state.get("route")
        query = final_state.get("query") or ""
        if not route:
            return None

        if route == "sql":
            rows = final_state.get("sql_rows") or []
            answer = _rows_to_markdown(rows)
            evidence = {"sql": final_state.get("sql"), "row_count": len(rows)}
        elif route == "doc":
            answer = final_state.get("doc_answer") or ""
            evidence = {"cited_pages": final_state.get("doc_cited_pages") or []}
        elif route == "hybrid":
            answer = final_state.get("hybrid_content") or ""
            evidence = {
                "sql_rows": final_state.get("sql_rows") or [],
                "doc_answer": final_state.get("doc_answer") or "",
            }
        elif route == "knowledge":
            answer = final_state.get("knowledge_answer") or ""
            evidence = {
                "item_id": final_state.get("knowledge_item_id"),
                "recall_score": final_state.get("knowledge_score"),
            }
        elif route == "chat":
            answer = self._last_ai_content(final_state.get("messages"))
            evidence = {}
        else:
            return None

        if not (answer or "").strip():
            return None
        return EvalSample(query=query, route=route, answer=answer, evidence=evidence)

    # 执行一次在线评估并落库；参数 final_state=图终态，session_id=会话标识，返回 QueryEval 或 None(未评估)
    async def evaluate(
        self, final_state, session_id: str | None = None
    ) -> QueryEval | None:
        """评估终端答案并落库（失败/未启用时返回 None，不影响主流程）"""

        if not app_config.eval.enabled or not final_state:
            return None
        sample = self.extract_sample(final_state)
        if sample is None:
            return None

        start = time.monotonic()
        # 逐个判定器打分：单个失败只跳过，不影响其他判定器
        pairs: list[tuple[Judge, JudgeResult]] = []
        for judge in self.judges:
            try:
                result = await judge.evaluate(sample)
                pairs.append((judge, result))
            except Exception as e:
                logger.warning(f"评估判定器 {judge.name} 失败，跳过：{e}")
        if not pairs:
            return None

        # 按权重聚合综合分
        total_weight = sum(judge.weight for judge, _ in pairs) or 1.0
        score = (
            sum(result.score * judge.weight for judge, result in pairs) / total_weight
        )
        passed = score >= app_config.eval.pass_threshold

        dimensions: dict = {}
        reasons: list[str] = []
        for judge, result in pairs:
            for key, value in result.dimensions.items():
                dimensions[f"{judge.name}.{key}"] = value
            if result.reason:
                reasons.append(f"[{judge.name}] {result.reason}")

        record = QueryEval(
            id=uuid.uuid4().hex,
            session_id=session_id,
            query=sample.query,
            route=sample.route,
            judge="+".join(judge.name for judge, _ in pairs),
            score=round(score, 4),
            passed=passed,
            dimensions=dimensions,
            reason="\n".join(reasons)[:1000],
            answer=sample.answer[:_MAX_ANSWER_CHARS],
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        try:
            # 惰性建表，兼容未重建 volume 的旧库
            await self.repository.ensure_table()
            self.repository.save(record)
            await self.repository.session.commit()
            logger.info(
                f"结果评估完成：route={record.route} score={record.score:.3f} passed={passed}"
            )
        except Exception as e:
            # 落库失败不影响已返回的答案
            logger.warning(f"评估落库失败（不影响问答）：{e}")
        return record
