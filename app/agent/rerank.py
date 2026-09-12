"""
召回后的跨编码器语义重排工具

三路召回（字段/指标/取值）在去重之后会得到一批粗候选，这里用 cross-encoder 对
(query, 每个候选) 重新打分并按分数降序截取 top_k，从而把真正和用户问题语义最贴近
的候选排到最前，提升后续 SQL 生成时喂给模型的元数据质量。

设计上与项目一致遵循 fail-open：重排服务不可用时降级为按原顺序截取，不让单点故障
中断整条问数链路。
"""

from typing import Callable, TypeVar

from langgraph.runtime import Runtime

from app.conf.app_config import app_config
from app.core.log import logger

T = TypeVar("T")


# 对去重后的候选列表做跨编码器语义重排并截取 top_k；
# 参数 query=用户问题，candidates=去重后的候选实体列表，render=把单个候选渲染成待打分文本，
# runtime=携带 rerank_client 的运行上下文；返回按重排分数降序的 top_k 候选列表（服务不可用时按原序截取兜底）
async def rerank_candidates(
    query: str,
    candidates: list[T],
    render: Callable[[T], str],
    runtime: Runtime,
    top_k: int | None = None,
) -> list[T]:
    """对候选做语义重排后截取 top_k，返回重排后的候选列表"""

    # 候选为空或只有一个时无需重排，直接返回
    if not candidates:
        return candidates
    if top_k is None:
        top_k = app_config.rerank.top_k
    # 候选数不超过目标条数时，重排没有增益，直接按原序返回
    if len(candidates) <= top_k:
        return candidates

    rerank_client = runtime.context.get("rerank_client")
    try:
        # 把每个候选渲染成一段文本，分批提交给 cross-encoder 打分：
        # TEI /rerank 有服务端单次 batch 上限（超限返回 413），分批后合并全局分数
        texts = [render(candidate) for candidate in candidates]
        batch = max(1, app_config.rerank.max_batch)
        ranked: list[tuple[int, float]] = []
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            chunk_ranked = await rerank_client.rerank(query, chunk)
            ranked.extend((start + idx, score) for idx, score in chunk_ranked)
        ranked.sort(key=lambda item: item[1], reverse=True)
        # rerank 返回 (全局原始下标, 分数)，按下标回填对应候选并按分数取前 top_k
        ordered = [candidates[index] for index, _ in ranked]
        picked = ordered[:top_k]
        logger.info(
            f"重排后保留 {len(picked)} 条候选，最高分 {ranked[0][1] if ranked else 0:.4f}"
        )
        return picked
    except Exception as e:
        # fail-open：重排服务异常时按原顺序截取，保证链路不中断
        logger.warning(f"语义重排不可用，降级为按原序截取: {e}")
        return candidates[:top_k]
