"""题型路由：VLM 分类 + 关键词启发式兜底。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .. import api_client
from .prompts import ROUTER_SYSTEM, ROUTER_USER

_COMPARE_WORDS = re.compile(
    r"\b(compare|comparison|versus|vs\.?|difference|higher|lower|which one|both)\b",
    re.I,
)
_SUMMARY_WORDS = re.compile(
    r"\b(summarize|summary|overview|main points|describe the overall)\b", re.I
)
_TABLE_WORDS = re.compile(
    r"\b(table|row|column|parameters|measurements|values of)\b", re.I
)
_IMAGE_WORDS = re.compile(
    r"\b(figure|chart|plot|diagram|graph|illustration|image)\b", re.I
)
# 离子信号/指印类题：答案是图像面板+正文逐对象结论，必须走图文路径
_ION_IMG_WORDS = re.compile(r"\b(ion signals?|fingermarks?|MALDI)\b", re.I)
# 阶段图题（如 "stages ... cyclical approach"）：答案在循环流程图里，需强制图文路径
_STAGE_DIAG = re.compile(r"\bstages\b.*\bcyclical\b|\bcyclical\b.*\bstages\b", re.I)
# 弱点-模式关联图（如 "weaknesses connected to the absence of X"）：答案在分析图节点里
_WEAKNESS_ABSENCE = re.compile(
    r"\bweaknesses?\b.*\b(?:connected|linked|related|associated|tied)\b.*\babsence\b"
    r"|\babsence\b.*\b(?:connected|linked|related|associated|tied)\b.*\bweaknesses?\b",
    re.I,
)
# 财务指标 + 期间变化（如 "shift in accumulated profits ... over the period"）→ 表格题
_FIN_METRIC = re.compile(
    r"\b(profit|earnings|revenue|equity|capital|assets|liabilities|retained|balance|accumulated)\b",
    re.I,
)
_PERIOD_SHIFT = re.compile(
    r"\b(over the period|shift|change|movement|decline|increase|difference)\b", re.I
)


@dataclass
class Route:
    question_type: str = "factual_retrieval"
    answer_type: str = "text_only"
    needs_image: bool = False
    key_entities: List[str] = field(default_factory=list)
    sub_questions: List[str] = field(default_factory=list)
    confidence: float = 1.0
    source: str = "heuristic"  # "vlm" | "heuristic" | "vlm+heuristic"


def heuristic_route(question: str) -> Route:
    if _SUMMARY_WORDS.search(question):
        qt = "summarization"
    elif _COMPARE_WORDS.search(question):
        qt = "comparison"
    else:
        qt = "factual_retrieval"
    if _TABLE_WORDS.search(question):
        at = "table_required"
    elif _ION_IMG_WORDS.search(question):
        at = "image_plus_text"
    elif _STAGE_DIAG.search(question):
        at = "image_plus_text"
    elif _WEAKNESS_ABSENCE.search(question):
        at = "image_only"
    elif _IMAGE_WORDS.search(question):
        at = "image_only"
    elif _FIN_METRIC.search(question) and _PERIOD_SHIFT.search(question):
        at = "table_required"
    else:
        at = "text_only"
    needs_image = at in ("image_only", "table_required", "image_plus_text")
    return Route(
        question_type=qt, answer_type=at, needs_image=needs_image, source="heuristic"
    )


async def route_question(question: str, use_vlm: bool = True) -> Route:
    if not use_vlm:
        return heuristic_route(question)
    try:
        # 走 VLMClient（含缓存）：同题跨轮次/跨策略复用路由，消除路由抖动
        client = api_client.VLMClient()
        raw = await client.infer(
            [
                {"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": ROUTER_USER.format(question=question)},
            ],
            max_tokens=400,
        )
        data = api_client.extract_json(raw)
        if isinstance(data, dict):
            qt = str(data.get("question_type", "")).strip()
            at = str(data.get("answer_type", "")).strip()
            if qt not in ("factual_retrieval", "comparison", "summarization"):
                qt = "factual_retrieval"
            if at not in (
                "text_only",
                "image_only",
                "table_required",
                "image_plus_text",
            ):
                at = "text_only"
            subs = data.get("sub_questions", [])
            if not isinstance(subs, list):
                subs = []
            conf = data.get("confidence", 0.8)
            try:
                conf = float(conf)
            except TypeError, ValueError:
                conf = 0.8
            # 启发式兜底：VLM 把图题误判为 text_only 时强制升级（P4.5）；
            # 离子信号题即使被 VLM 判为表格题也要强制升级到图文路径
            h = heuristic_route(question)
            source = "vlm"
            if at == "text_only" and h.answer_type in (
                "image_only",
                "table_required",
                "image_plus_text",
            ):
                at = h.answer_type
                conf = min(conf, 0.6)
                source = "vlm+heuristic"
            elif h.answer_type == "image_plus_text" and at != "image_plus_text":
                at = "image_plus_text"
                conf = min(conf, 0.6)
                source = "vlm+heuristic"
            return Route(
                question_type=qt,
                answer_type=at,
                needs_image=bool(data.get("needs_image", False))
                or at in ("image_only", "table_required", "image_plus_text"),
                key_entities=[str(e) for e in data.get("key_entities", [])][:8],
                sub_questions=[str(s) for s in subs][:5],
                confidence=conf,
                source=source,
            )
    except Exception:
        pass
    return heuristic_route(question)


def topk_for_route(route: Route) -> int:
    from ..config import settings

    if route.question_type == "summarization":
        return settings.page_topk_default
    if route.answer_type in ("image_only", "table_required", "image_plus_text"):
        return settings.page_topk_visual
    return settings.page_topk_text
