"""Agent 编排：路由 → 检索 → 作答 → 自校验 → (邻页扩展/加宽召回 → 重答)。

迭代有硬上限与总轮次预算，兼顾精度与时延。
"""

from __future__ import annotations

import asyncio
import re as _re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.log import logger

from .. import api_client
from ..config import settings
from ..retrieval.recall import IndexStore, retrieve_top_elements
from ..retrieval.rerank import PageEvidence, retrieve
from ..vision.strategies import _STAGE_HINT_RE, enforce_completeness, get_strategy
from .answerer import (
    AnswerResult,
    _context_text,
    answer_with_evidence,
    parse_answer,
)
from .prompts import (
    SUB_ANSWER_SYSTEM,
    SYNTHESIS_SYSTEM,
    sub_answer_user,
    synthesis_user,
)
from .router import Route, route_question, topk_for_route
from .verifier import verify_answer

# 双问句题（如 "what drives ... and how is it mainly utilized"）：
# 第二个"利用方式"子问句的答案页与主问句语义距离远，主检索常取不到，
# 用实体构造利用子查询做二次检索并合并
_UTIL_PART_RE = _re.compile(r"\band\s+how\s+is\s+it\s+(?:mainly\s+)?utilized", _re.I)
_USE_VERB_RE = _re.compile(
    r"\b(use[sd]?|using|uses|utiliz\w+|application[s]?|employed)\b", _re.I
)
_MARKET_RE = _re.compile(
    r"\b(domestic market|export|sold|demand|market share)\b", _re.I
)


def _util_sub_question(question: str, route) -> str:
    if not _UTIL_PART_RE.search(question or ""):
        return ""
    ents = (route.key_entities if route else None) or []
    entity = next((e for e in ents if _re.match(r"^[A-Z][A-Z0-9]{1,10}$", e)), "")
    if not entity:
        return ""
    return f"What is {entity} used for? What are its main applications, utilization and common uses?"


def _usage_sentences(question: str, route, evidence) -> str:
    """确定性抽取证据中的"利用句"（实体 + 使用动词，排除市场句），
    注入答案生成，防漏第二项利用（并列多个利用对象的问句）。"""
    if not _UTIL_PART_RE.search(question or ""):
        return ""
    ents = (route.key_entities if route else None) or []
    entity = next((e for e in ents if _re.match(r"^[A-Z][A-Z0-9]{1,10}$", e)), "")
    if not entity:
        return ""
    out = []
    for e in evidence:
        for sent in _re.split(r"(?<=[.!?])\s+", e.text or ""):
            if entity.lower() not in sent.lower():
                continue
            if not _USE_VERB_RE.search(sent) or _MARKET_RE.search(sent):
                continue
            sent = " ".join(sent.split())
            if 20 < len(sent) < 400 and sent not in out:
                out.append(sent)
        if len(out) >= 8:
            break
    if not out:
        return ""
    return "\n- ".join(out)


def _usage_key_term(sent: str):
    """从利用句中确定性提取 (检查词, 展示词)：如应用领域 / 具体制品。

    检查词用于判断答案是否已覆盖该制品，展示词用于补全句。
    """
    m = _re.search(r"the\s+manufacturing\s+([A-Za-z][A-Za-z-]{1,20})", sent)
    if m:
        prod = m.group(1).strip()
        return prod, f"the manufacturing of {prod}s"
    m = _re.search(r"([A-Z][A-Za-z0-9 /-]{2,40}?)\s+starts?\s+to\s+use", sent)
    if m:
        term = _re.sub(r"^(A few|a few|few|A|An|an|The|the)\s+", "", m.group(1)).strip()
        return term, term
    return None, None


def _complete_usage(question: str, route, evidence, answer: str) -> str:
    """确定性补全：LLM 答案漏掉的利用对象，从证据利用句补上。"""
    if not _UTIL_PART_RE.search(question or ""):
        return answer
    sents = (_usage_sentences(question, route, evidence) or "").split("\n- ")
    sents = [s for s in sents if s]
    if not sents or not answer:
        return answer
    added = []
    for s in sents[:6]:
        check, display = _usage_key_term(s)
        if (
            check
            and check.lower() not in answer.lower()
            and display.lower() not in answer.lower()
        ):
            added.append(f"It is also used in {display}.")
    if not added:
        return answer
    return answer.rstrip().rstrip(".") + ". " + " ".join(added)


@dataclass
class AgentTrace:
    route: Optional[Route] = None
    evidence: List[PageEvidence] = field(default_factory=list)
    answer: str = ""
    rounds: int = 0
    timing: dict = field(default_factory=dict)
    vlm_outputs: list = field(default_factory=list)
    selected_images: list = field(default_factory=list)
    crop_images: list = field(default_factory=list)
    strategy_name: str = ""
    # 可溯源：本次答案实际引用的页码（1-based，来自模型 cited_pages 或证据页）
    cited_pages: List[int] = field(default_factory=list)
    # 是否因约束（重排分低于阈值 / 无可溯源引用）被拒绝作答
    refused: bool = False
    refuse_reason: str = ""


# 重排分门：返回 (是否拒绝, 原因)。仅当证据里带 rerank 原始分时才判定（视觉/元素路径无此分）。
# 最高重排分低于 rerank_min_score 视为语料未覆盖，拒绝作答以防编造。
def _rerank_gate(evidence: List[PageEvidence]) -> tuple[bool, str]:
    scores = [
        e.sources.get("rerank")
        for e in evidence
        if e.sources and isinstance(e.sources.get("rerank"), (int, float))
    ]
    if not scores:
        return False, ""
    top = max(scores)
    if top < settings.rerank_min_score:
        return (
            True,
            f"top rerank score {top:.3f} < threshold {settings.rerank_min_score}",
        )
    return False, ""


# 校验模型引用的页码是否都来自证据页；只保留证据里真实存在的页（1-based）；参数 cited=模型引用页码，evidence=证据页
def _valid_cited(cited, evidence: List[PageEvidence]) -> List[int]:
    pages = {e.page_idx + 1 for e in evidence}
    valid = {
        int(c) for c in (cited or []) if isinstance(c, (int, float)) and int(c) in pages
    }
    return sorted(valid)


def _neighbor_expand(
    store: IndexStore, evidence: List[PageEvidence]
) -> List[PageEvidence]:
    """把当前证据中得分最高的前 3 页的相邻页加入（同文档 ±1 页）。

    只从高分页扩展：低分页的邻居多为噪声，会把证据带偏（实证观察）。
    """
    seen = {e.pkey for e in evidence}
    out = list(evidence)
    for e in evidence[:3]:
        for dp in (-1, 1):
            nk = f"{e.doc_id}|{e.page_idx + dp}"
            if nk in seen:
                continue
            meta = store.page_of(nk)
            if meta:
                seen.add(nk)
                out.append(
                    PageEvidence(
                        pkey=nk,
                        doc_id=meta["doc_id"],
                        page_idx=meta["page_idx"],
                        text=meta["text"],
                        page_image=meta["page_image"],
                        elements=meta["elements"],
                        final_score=e.final_score * 0.98,
                    )
                )
    out.sort(key=lambda x: x.final_score, reverse=True)
    return out


def _sub_questions_valid(subs: List[str]) -> bool:
    """守卫：拆解出的子问题必须是实质句子（≥3 词），否则回退单次对比作答。"""
    if len(subs) < 2:
        return False
    return all(len(s.split()) >= 3 for s in subs)


def _answer_overlaps_question(answer: str, question: str) -> bool:
    """视觉 rescue 答案是否与问题关键 token 有交集（防"风暴残骸"类完全无关答案覆盖）。"""
    import re

    qtoks = {t for t in re.findall(r"[a-z0-9]{4,}", question.lower())}
    atoks = {t for t in re.findall(r"[a-z0-9]{4,}", (answer or "").lower())}
    return bool(qtoks & atoks)


class Agent:
    def __init__(
        self,
        store: IndexStore,
        use_vlm_router: bool = True,
        image_strategy: Optional[str] = None,
        workdir: str = "",
        vlm_client: Optional[api_client.VLMClient] = None,
    ):
        self.store = store
        self.use_vlm_router = use_vlm_router
        self.image_strategy = get_strategy(image_strategy)
        self.workdir = workdir
        self.vlm_client = vlm_client or api_client.VLMClient()
        # 视觉复合题专用模型（如 30B 对趋势/多条件推理更强）
        self.vlm_visual_client = self.vlm_client
        if settings.llm_model_visual != self.vlm_client.model:
            self.vlm_visual_client = api_client.VLMClient(
                model=settings.llm_model_visual
            )

    async def _answer_comparison_decomposed(
        self,
        question: str,
        sub_questions: List[str],
        include_images: bool,
    ) -> AnswerResult:
        """对比题拆解：逐子问题检索+提取，再综合（对比题常需逐项比对后再归纳）。"""
        sub_results = []
        cited = []
        for sq in sub_questions:
            vec = (await api_client.embed_texts([sq]))[0]
            evidence = await retrieve(
                sq, self.store, vec, top_k=settings.page_topk_text
            )
            # 可溯源：把每个子问题的证据页也纳入引用候选，保证答案能落到具体页码
            cited.extend(e.page_idx + 1 for e in evidence)
            ctx = _context_text(evidence)
            try:
                raw = await api_client.chat_complete(
                    [
                        {"role": "system", "content": SUB_ANSWER_SYSTEM},
                        {"role": "user", "content": sub_answer_user(sq, ctx)},
                    ],
                    max_tokens=400,
                    temperature=0.0,
                )
                data = api_client.extract_json(raw)
                if isinstance(data, dict):
                    item = str(data.get("item", sq))
                    value = str(data.get("value", "NOT_FOUND"))
                    cp = data.get("cited_pages", [])
                    if isinstance(cp, list):
                        cited.extend(int(c) for c in cp if isinstance(c, (int, float)))
                    sub_results.append(f"- {item}: {value}")
                else:
                    sub_results.append(f"- {sq}: (parse failed)")
            except Exception as e:
                sub_results.append(f"- {sq}: (error {str(e)[:40]})")

        raw = await api_client.chat_complete(
            [
                {"role": "system", "content": SYNTHESIS_SYSTEM},
                {
                    "role": "user",
                    "content": synthesis_user(question, "\n".join(sub_results)),
                },
            ],
            max_tokens=settings.answer_max_tokens,
            temperature=0.0,
        )
        result = parse_answer(raw)
        if cited:
            result.cited_pages = sorted(set(cited))
        return result

    async def answer(
        self, question: str, trace: Optional[AgentTrace] = None, sample_id: str = ""
    ) -> tuple[str, AgentTrace]:
        trace = trace or AgentTrace()
        t0 = time.perf_counter()

        # 1. 路由与问题向量化并行（互不依赖）
        route_task = asyncio.create_task(
            route_question(question, use_vlm=self.use_vlm_router)
        )
        embed_task = asyncio.create_task(api_client.embed_texts([question]))
        route = await route_task
        trace.route = route
        trace.timing["route"] = round(time.perf_counter() - t0, 3)
        vec = (await embed_task)[0]
        top_k = topk_for_route(route)
        # 阶段/序列题：文本证据常缺图中最后一个阶段（如 'Revise Plan'），
        # 用元素构建期描述（确定性、无 API 调用）补全序列
        stage_elements = None
        if _STAGE_HINT_RE.search(question):
            stage_elements = await retrieve_top_elements(vec, self.store, top_k=3)
        # 页面图总是送（路由对图题判断不可靠，且整页图对文本题也无害）
        include_images = not settings.no_image_mode
        # 图片/表格题答案在图/表里，加大元素相关度权重
        element_weight = (
            0.35
            if route.answer_type in ("image_only", "table_required", "image_plus_text")
            else 0.0
        )
        rerank_on_elements = False

        # 2. 图/表题：按题型分流视觉策略（视觉 Router）：
        #    - image_only       → 走所选策略（crop/full_plus_crop/verify，读原图）
        #    - table_required   → baseline 描述+markdown 证据（表格图片直读反而差，低置信度时再做 VLM 兜底）
        #    - image_plus_text  → 常规页文本+整页图路径（需图文结合，元素捷径易误）
        #    - text_only        → 常规路径（仅在校验失败后 rescue 视觉策略）
        if not settings.no_image_mode:
            top_elements = await retrieve_top_elements(vec, self.store, top_k=3)
            use_element_shortcut = route.answer_type in (
                "image_only",
                "table_required",
                "image_plus_text",
            )
            if top_elements and use_element_shortcut:
                strat = self.image_strategy
                top_kind = top_elements[0][0].get("kind")
                if route.answer_type == "table_required":
                    from ..vision.strategies import BaselineStrategy, VerifyStrategy

                    if top_kind == "table":
                        # 表元素 → 描述+markdown 证据（实测优于直读表格图）
                        strat = BaselineStrategy()
                    else:
                        # 被误路由成 table 的图题（层厚图/评级矩阵等）→ 读图+数字复核
                        strat = VerifyStrategy()
                elif route.answer_type == "image_plus_text":
                    from ..vision.strategies import HybridStrategy

                    strat = HybridStrategy()
                sres = await strat.answer_visual(
                    question,
                    top_elements,
                    route,
                    self.store,
                    self.vlm_visual_client
                    if route.answer_type == "image_plus_text"
                    else self.vlm_client,
                    sample_id=sample_id,
                    workdir=self.workdir,
                )
                trace.strategy_name = strat.name
                trace.vlm_outputs = sres.vlm_outputs
                trace.selected_images = sres.selected_images
                trace.crop_images = sres.crop_images
                # 记录父页证据（旧元素路径丢失 cited_pages，此处补上）
                trace.evidence = [
                    PageEvidence(
                        pkey=f"{p.get('doc_id')}|{p.get('page_idx')}",
                        doc_id=p.get("doc_id", ""),
                        page_idx=p.get("page_idx", 0),
                        text=p.get("text", ""),
                        page_image=p.get("page_image", ""),
                        elements=p.get("elements", []),
                    )
                    for p in sres.evidence_pages
                ]
                if (
                    sres.answer
                    and "NOT_FOUND" not in sres.answer
                    and "missing" not in sres.answer.lower()
                ):
                    # 阶段/序列题：逐条 VLM 读取证据重建流转链，取「全合法阶段」最长者
                    if stage_elements:
                        from ..vision.strategies import _valid_stage

                        best_ans, best_len = sres.answer, 0
                        for v in sres.vlm_outputs or []:
                            ev = str(v.get("output", ""))
                            enriched = enforce_completeness(
                                {
                                    "answer": sres.answer,
                                    "evidence": ev,
                                    "confidence": 1.0,
                                },
                                question,
                            )
                            ans = enriched.get("answer") or ""
                            m = _re.search(r"The sequence is: (.+?)[.!]?$", ans)
                            if m:
                                items = [x.strip() for x in m.group(1).split(",")]
                                if (
                                    items
                                    and all(_valid_stage(x) for x in items)
                                    and len(items) > best_len
                                ):
                                    best_ans, best_len = ans, len(items)
                        if best_len >= 5:
                            sres.answer = best_ans
                    trace.answer = sres.answer
                    trace.cited_pages = [e.page_idx + 1 for e in trace.evidence]
                    trace.timing["total"] = round(time.perf_counter() - t0, 3)
                    return sres.answer, trace

        # 2.5 对比题拆解路径（仅纯文本对比；图/表类对比走常规路径+校验+rescue，
        #     拆解路径只读文本、忽略图片，正是 crm_0118 类错因）
        if (
            route.question_type == "comparison"
            and route.answer_type == "text_only"
            and _sub_questions_valid(route.sub_questions)
        ):
            result = await self._answer_comparison_decomposed(
                question, route.sub_questions, include_images
            )
            trace.answer = result.answer
            trace.evidence = []
            trace.cited_pages = [
                int(c) for c in result.cited_pages if isinstance(c, (int, float))
            ]
            trace.timing["total"] = round(time.perf_counter() - t0, 3)
            return result.answer, trace

        # 3. 常规路径：检索 → 作答
        evidence = await retrieve(
            question,
            self.store,
            vec,
            top_k=top_k,
            element_weight=element_weight,
            rerank_on_elements=rerank_on_elements,
        )
        util_q = _util_sub_question(question, route)
        if util_q:
            util_vec = (await api_client.embed_texts([util_q]))[0]
            more = await retrieve(
                util_q,
                self.store,
                util_vec,
                top_k=top_k,
                element_weight=element_weight,
                rerank_on_elements=rerank_on_elements,
            )
            seen = {e.pkey for e in evidence}
            for m in more:
                if m.pkey not in seen:
                    evidence.append(m)
            evidence.sort(key=lambda x: x.final_score, reverse=True)
            evidence = evidence[: max(top_k + 2, settings.page_topk_visual)]
        trace.evidence = evidence
        trace.timing["retrieve"] = round(time.perf_counter() - t0, 3)

        # 3.1 重排分门：证据里最高 bge-reranker 相关分低于阈值 → 语料未覆盖，拒绝作答
        refused, reason = _rerank_gate(evidence)
        if refused:
            logger.warning(f"文档问答拒绝作答（重排分过低）：{reason}")
            trace.refused = True
            trace.refuse_reason = reason
            trace.answer = "NOT_FOUND"
            trace.timing["total"] = round(time.perf_counter() - t0, 3)
            return "NOT_FOUND", trace

        result = await answer_with_evidence(
            question,
            evidence,
            include_images=include_images,
            question_type=route.question_type,
            answer_type=route.answer_type,
            extra_hint=_usage_sentences(question, route, evidence),
        )
        trace.answer = result.answer
        # 可溯源：优先用模型引用且必须落在证据页内，否则回落到证据页
        trace.cited_pages = _valid_cited(result.cited_pages, evidence) or [
            e.page_idx + 1 for e in evidence
        ]

        # 4. 自校验 + 迭代（最多 max_agent_rounds 次额外重答）。
        #    利用题的利用子查询检索已在作答前合并、末尾还有 _complete_usage 确定性补全，
        #    校验重答轮冗余 → 利用题跳过多轮校验（时延优化）
        supported = True
        missing_facts = ""
        if not util_q:
            for rnd in range(settings.max_agent_rounds):
                trace.rounds = rnd + 1
                supported, _, missing_facts = await verify_answer(
                    question, result.answer, evidence
                )
                if supported:
                    break
                evidence = _neighbor_expand(self.store, evidence)
                top_k2 = top_k + 3
                if len(evidence) < top_k2:
                    more = await retrieve(
                        question,
                        self.store,
                        vec,
                        top_k=top_k2,
                        element_weight=element_weight,
                        rerank_on_elements=rerank_on_elements,
                    )
                    seen = {e.pkey for e in evidence}
                    for m in more:
                        if m.pkey not in seen:
                            evidence.append(m)
                evidence.sort(key=lambda x: x.final_score, reverse=True)
                evidence = evidence[: max(top_k2, settings.page_topk_visual + 2)]
                trace.evidence = evidence
                result = await answer_with_evidence(
                    question,
                    evidence,
                    include_images=include_images,
                    question_type=route.question_type,
                    answer_type=route.answer_type,
                    extra_hint=missing_facts[:400] if missing_facts else "",
                )
                trace.answer = result.answer

        # 5. rescue：校验失败后的补答。
        #    - 文本题：加宽文本重答 + 视觉策略双通道；视觉答案须与问题关键 token 有交集
        #      且置信度≥0.9 才允许覆盖（实证区分相近题型的误答）
        #    - 图/表题：视觉策略补答（置信度门控，防垃圾覆盖）
        if not settings.no_image_mode and not supported and not trace.strategy_name:
            wide = await retrieve(
                question,
                self.store,
                vec,
                top_k=settings.page_topk_visual + 4,
                element_weight=0.0,
                rerank_on_elements=False,
            )
            wide_res = await answer_with_evidence(
                question,
                wide,
                include_images=True,
                question_type=route.question_type,
                answer_type=route.answer_type,
                extra_hint=missing_facts[:400] if missing_facts else "",
            )
            trace.evidence = wide
            result = wide_res
            trace.answer = wide_res.answer
            trace.strategy_name = "widen_text"
            top_elements = await retrieve_top_elements(vec, self.store, top_k=3)
            if top_elements:
                sres = await self.image_strategy.answer_visual(
                    question,
                    top_elements,
                    route,
                    self.store,
                    self.vlm_client,
                    sample_id=sample_id,
                    workdir=self.workdir,
                )
                trace.vlm_outputs = sres.vlm_outputs
                trace.selected_images = sres.selected_images
                trace.crop_images = sres.crop_images
                if (
                    sres.answer
                    and "NOT_FOUND" not in sres.answer
                    and "missing" not in sres.answer.lower()
                ):
                    text_empty = (
                        not result.answer.strip() or "NOT_FOUND" in result.answer
                    )
                    overlap = _answer_overlaps_question(sres.answer, question)
                    if text_empty or (sres.confidence >= 0.9 and overlap):
                        trace.answer = sres.answer
                        result = AnswerResult(answer=sres.answer)
                        trace.strategy_name = self.image_strategy.name

        # 利用题补全：LLM 漏掉的利用对象从证据句补上
        trace.answer = _complete_usage(question, route, trace.evidence, trace.answer)
        result.answer = trace.answer

        # 可溯源兜底：模型未给出有效引用时，回落到证据页，保证答案必可追溯
        if not trace.cited_pages:
            trace.cited_pages = [e.page_idx + 1 for e in trace.evidence]

        trace.timing["total"] = round(time.perf_counter() - t0, 3)
        return result.answer, trace
