"""证据自校验：判断答案是否被检索证据支撑，驱动迭代检索。

两重校验：
1. 数值接地：答案中的关键数值必须原样出现在证据文本中（防幻觉数值）；
2. 语义支撑：LLM 判断答案是否被证据内容支撑。
"""

from __future__ import annotations

import re

from .. import api_client
from ..retrieval.rerank import PageEvidence
from .answerer import _context_text
from .prompts import VERIFY_SYSTEM, verify_user

_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_MIN_SIG_DIGITS = 3
_DIFF_WORDS = re.compile(r"\b(differ|difference|compare|comparison|versus|vs\.?|which (one|is))\b", re.I)
_SAME_WORDS = re.compile(r"\b(no difference|not differ|the same|identical|similar to each other|both are)\b", re.I)


def _contradicts_diff_question(question: str, answer: str) -> bool:
    """问差异/对比却回答“无差别/相同”→ 疑似幻觉，判不通过触发扩展。"""
    return bool(_DIFF_WORDS.search(question) and _SAME_WORDS.search(answer))


def _extract_numbers(text: str) -> list[str]:
    return [_norm_num(m.group()) for m in _NUM_RE.finditer(text)]


def _norm_num(raw: str) -> str:
    return raw.replace(",", "").lstrip("+")


def _significant(s: str) -> bool:
    return len(s.replace(".", "").lstrip("-")) >= _MIN_SIG_DIGITS


def numbers_grounded(answer: str, evidence_text: str) -> tuple[bool, list[str]]:
    """答案中的关键数值是否都出现在证据文本中。返回 (是否全部接地, 缺失数值)。"""
    ev_norm = evidence_text.replace(",", "")
    missing = []
    for n in _extract_numbers(answer):
        if not _significant(n):
            continue
        if n not in ev_norm:
            missing.append(n)
    return not missing, missing


async def verify_answer(
    question: str, answer: str, evidence: list[PageEvidence]
) -> tuple[bool, str, str]:
    """返回 (supported, reason, missing)。数值不接地则直接判不通过并触发扩展。"""
    if _contradicts_diff_question(question, answer):
        return False, "answer claims no difference for a difference/comparison question", ""
    full_ctx = _context_text(evidence)
    grounded, missing = numbers_grounded(answer, full_ctx)
    if not grounded:
        return False, f"answer contains numbers not in evidence: {missing[:6]}", ""

    try:
        raw = await api_client.chat_complete(
            [
                {"role": "system", "content": VERIFY_SYSTEM},
                {"role": "user", "content": verify_user(question, answer, full_ctx)},
            ],
            max_tokens=300,
            temperature=0.0,
        )
        data = api_client.extract_json(raw)
        if isinstance(data, dict):
            supported = bool(data.get("supported", False))
            reason = str(data.get("reason", ""))
            missing_facts = str(data.get("missing", "") or "")
            return supported, reason, missing_facts
    except Exception as e:
        return True, f"verify skipped: {e}", ""
    return True, "verify skipped", ""
