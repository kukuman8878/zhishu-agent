"""证据组装与 VLM 作答（多图 CoT，结构化 JSON 输出 + 鲁棒解析）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .. import api_client
from ..retrieval.rerank import PageEvidence
from .prompts import _pick_answer_system, answer_user


@dataclass
class AnswerResult:
    answer: str
    analysis: str = ""
    cited_pages: List[int] = field(default_factory=list)
    confidence: float = 0.0
    raw: str = ""


def _context_text(evidence: List[PageEvidence]) -> str:
    from ..config import settings

    parts = []
    budget = settings.ctx_total_chars
    for e in evidence:
        txt = e.text[: settings.ctx_page_chars]
        parts.append(f"--- Page {e.page_idx + 1} (doc {e.doc_id}) ---\n{txt}")
        budget -= len(txt)
        if budget <= 0:
            break
    return "\n\n".join(parts)


def _context_images(evidence: List[PageEvidence]) -> List[tuple[str, str]]:
    """返回带标签的图片列表 [(标签, 图片路径)]。

    只送整页图（整页已含图/表，且避免元素裁剪图挤占后面的关键页）。
    """
    from ..config import settings

    items: List[tuple[str, str]] = []
    for e in evidence:
        if e.page_image:
            items.append((f"[Full page {e.page_idx + 1} of {e.doc_id}]", e.page_image))
    return items[: settings.img_max_count]


def _build_messages(
    question: str,
    evidence: List[PageEvidence],
    include_images: bool,
    question_type: str = "factual_retrieval",
    answer_type: str = "text_only",
    extra_hint: str = "",
) -> List[dict]:
    ctx = _context_text(evidence)
    imgs = _context_images(evidence)
    system = _pick_answer_system(question_type, answer_type)
    hint = ""
    if extra_hint:
        hint = (
            "\n\nIMPORTANT INSTRUCTION: the facts below are present in the evidence and "
            f"MUST be included in your answer: {extra_hint}\n"
        )
    content: List[dict] = []
    for i, (label, img) in enumerate(imgs):
        content.append({"type": "text", "text": f"{label}"})
        content.append(api_client.image_message(img))
    content.append({"type": "text", "text": answer_user(ctx, question) + hint})
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": content if imgs else answer_user(ctx, question) + hint,
        },
    ]


def parse_answer(raw: str) -> AnswerResult:
    data = api_client.extract_json(raw)
    if isinstance(data, dict):
        answer = str(data.get("answer", "")).strip()
        analysis = str(data.get("analysis", "")).strip()
        cited = data.get("cited_pages", [])
        if isinstance(cited, list):
            cited = [int(c) for c in cited if isinstance(c, (int, float))]
        conf = data.get("confidence", 0.0)
        try:
            conf = float(conf)
        except TypeError, ValueError:
            conf = 0.0
        if answer:
            return AnswerResult(
                answer=answer,
                analysis=analysis,
                cited_pages=cited,
                confidence=conf,
                raw=raw,
            )
    return AnswerResult(answer=raw.strip(), raw=raw)


async def answer_with_evidence(
    question: str,
    evidence: List[PageEvidence],
    include_images: bool,
    max_tokens: int = 1500,
    question_type: str = "factual_retrieval",
    answer_type: str = "text_only",
    extra_hint: str = "",
) -> AnswerResult:
    messages = _build_messages(
        question,
        evidence,
        include_images=include_images,
        question_type=question_type,
        answer_type=answer_type,
        extra_hint=extra_hint,
    )
    raw = await api_client.chat_complete(
        messages, max_tokens=max_tokens, temperature=0.0
    )
    return parse_answer(raw)
