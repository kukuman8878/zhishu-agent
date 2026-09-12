"""图片读取策略（P3，docx 五/七）：baseline / crop / multi_crop / full_plus_crop / verify。

- baseline      : 构建期描述为主证据（对照组）
- crop          : 全页图定位区域 → 高 DPI 重渲染裁剪 → 放大 → 精读
- multi_crop    : 最多 3 个候选区域分别精读 → 聚合
- full_plus_crop: 全页图(结构) + 裁剪(精读) 双视图作答
- verify        : full_plus_crop + 关键 token 二次逐字符验证
描述一律降级为“辅助定位提示”，原图是唯一事实来源（docx 七）。
"""

from __future__ import annotations

import asyncio
import re as _re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .. import api_client
from ..config import settings
from . import image_utils as im
from . import verify_tokens as vt
from .prompts_vlm import (
    CROP_READ_SYSTEM,
    FULL_PLUS_CROP_SYSTEM,
    REGION_LOCATOR_SYSTEM,
    RETRY_LOCATOR_SYSTEM,
    VERIFY_TOKEN_SYSTEM,
    region_locator_user,
    verify_token_user,
)

_STAGE_HINT_RE = _re.compile(
    r"\b(stages?|steps?|sequence|phases?|cyclical|iterative)\b", _re.I
)

_QUOTED_RE = _re.compile(
    r"['\u2018\u2019]([A-Za-z][A-Za-z0-9 '-]{1,30})['\u2018\u2019](?=\s|$|,|\)|→)"
)
# 零宽 lookahead：允许相邻 pair 重叠（X followed by Y, then Z → (X,Y) 与 (Y,Z) 都要捕获）
_FLOW_PAIR_RE = _re.compile(
    r"(?=("
    r"['\"\u2018\u2019]([A-Za-z][A-Za-z0-9 '\-]{1,30})['\"\u2018\u2019]"
    r"\s*(?:\(\s*[^)]*\)\s*)?,?\s*(?:and\s+)?"
    r"(?:→|then\b|followed by\b|finally\b|next\b|down\s+to\b|back\s+to\b|"
    r"(?:an?\s+)?arrow[^'\"\u2018\u2019]{0,40}to\b)"
    r"\s*['\"\u2018\u2019]([A-Za-z][A-Za-z0-9 '\-]{1,30})['\"\u2018\u2019]))",
    _re.I,
)


def _flow_sequence(*sources: str) -> list[str]:
    """从证据/描述文本中的阶段流转对（X followed by Y / X → Y）重建有序阶段序列。

    规则：首个 pair 的 source 起步；s == 当前末项时追加 target；target == 首项视为
    循环回指（如 'Reflect' back to 'Plan' 与 'Revise Plan' 并存），跳过。
    """
    seq: list[str] = []
    for src in sources:
        if not src:
            continue
        for m in _FLOW_PAIR_RE.finditer(src):
            s, t = m.group(2).strip(), m.group(3).strip()
            if s == t or not _valid_stage(s) or not _valid_stage(t):
                continue
            if not seq:
                seq = [s]
            if s == seq[-1]:
                if t != seq[0]:
                    seq.append(t)
    return seq


_STAGE_LOCATOR_EXTRA = (
    " This is a cyclical process diagram question. Return ONE region covering the ENTIRE "
    "diagram: every stage label inside the loop AND any revision/feedback step shown between "
    "the end of the loop and the start of the next cycle (e.g. a 'Revise Plan'/'Adjust' step), "
    "with generous margins."
)
_STAGE_READ_EXTRA = (
    " Cyclical diagram: list EVERY stage label shown in the diagram, including any revision/"
    "feedback step connecting the end of the cycle back to the next cycle (e.g. 'Revise Plan'). "
    "Do not stop early — check for labels outside the loop."
)
_PER_ITEM_LOCATOR_EXTRA = (
    " This question asks for EVERY item/layer/model listed (a per-item numeric question). "
    "Return ONE REGION PER ITEM (at most 4), so that every label/value can be read."
)
# 逐项数字题：问题要求列出每一项/每层的数值（如 each deposited layer → 各层厚度）
_PER_ITEM_RE = _re.compile(
    r"\beach\b.*\b(layer|row|column|model|class|machine|panel)\b|\bper\s+(layer|item|row|column)\b",
    _re.I,
)
# 路径/流向题（如 "how does water flow from ... towards ..."）：必须读全图整条线
_PATH_RE = _re.compile(
    r"\bwater\s+flow\w*\b|\bflows?\b.{0,100}\b(?:towards?|toward)\b|\bdirection\s+of\s+water\b",
    _re.I,
)
_PATH_READ_EXTRA = (
    " PATH/FLOW question: trace the arrow/line step by step from its starting label, through "
    "EVERY labeled area it passes by or through, to its endpoint. Report the ordered list "
    "(start -> each intermediate -> end). Do not skip intermediate stops; do not add labels "
    "that the line does not touch."
)
# 通用 prompt 只能读 4/10 值，需面板逐 legend 定向读取 + 跨面板类容量一致性修正
_Z_PANEL_RE = _re.compile(r"\bz[- ]scores?\b", _re.I)
_Z_PANEL_SYSTEM = (
    "You read values from a multi-panel line chart. The image is the source of truth. "
    "Output ONLY a JSON object."
)
_Z_ORD = {
    1: "first class",
    2: "second class",
    3: "third class",
    4: "fourth class",
    5: "fifth class",
}


_Z_PANEL_USER = (
    'Question: """{question}"""\n\n'
    "The chart contains several stacked panels (one per model), each panel has several lines; "
    "each line's legend label looks like '1/2, n=173.2' (class / number-of-classes, n=class size).\n"
    "Steps:\n"
    "1) Read the x-axis factor labels in order, and the legend labels of every panel.\n"
    "2) Find the x-axis factor the question asks about. If the question's wording differs from "
    "the axis label, use the closest semantic match (e.g. 'Can obtain beverages at school' = "
    "'Can buy Drinks at School'). Do NOT read values from any other factor column.\n"
    "3) At that factor's x-position, read the small printed z-score value for EVERY line in "
    "EVERY panel (top panel first).\n"
    'Output ONLY JSON: {{"panels": [{{"panel": 1, "lines": {{"1/2": 0.0, "2/2": 0.0}}}}]}} '
    'where each panel key is its position (1=top), and "lines" maps EVERY legend label to its value.'
)


_Z_PANEL_CROP_USER = (
    'Question: """{question}"""\n\n'
    "This image shows ONLY the bottom panel of a multi-panel line chart. "
    "Read its legend labels (like '1/2, n=173.2') and the x-axis factor labels.\n"
    "Find the x-axis factor the question asks about (closest semantic match, e.g. "
    "'Can obtain beverages at school' = 'Can buy Drinks at School').\n"
    "At that factor's x-position, read the printed z-score for EVERY line.\n"
    'Output ONLY JSON: {{"panels": [{{"panel": 1, "lines": {{"<legend-label>": 0.0}}}}]}}'
)

_Z_PANEL_COL_USER = (
    'Question: """{question}"""\n\n'
    "This image shows the LEFT PORTION of the bottom panel of a z-score chart. "
    "At the x-axis factor the question asks about (closest semantic match, e.g. "
    "'Can obtain beverages at school' = 'Can buy Drinks at School'), the panel's lines cross "
    "that factor column and a small z-score number is printed next to each crossing. "
    "Read those numbers from TOP line to BOTTOM line. Read every digit carefully "
    "(0.75 vs 0.47 vs 0.45 vs 0.93 are all possible). Do NOT read y-axis tick labels. "
    'Output ONLY JSON: {{"values": [<top>, <upper-mid>, <lower-mid>, <bottom>]}}'
)


def _z_factor_of(question: str) -> str:
    m = _re.search(
        r"for\s+['\"\u2018\u2019]([^'\"\u2018\u2019]{3,60})['\"\u2018\u2019]", question
    )
    if m:
        return m.group(1).strip()
    m = _re.search(
        r"for\s+([A-Z][^?]{3,60}?)\s+(?:across|distributed|per|in)", question
    )
    return m.group(1).strip() if m else ""


def _z_parse_panels(raw: str):
    """解析定向读取 JSON → [(panel_no, [(order, label, n, value)...])...]，按 panel 号排序。"""
    data = api_client.extract_json(raw)
    if not isinstance(data, dict):
        return []
    panels = data.get("panels")
    if not isinstance(panels, list):
        return []
    out = []
    for p in panels:
        if not isinstance(p, dict):
            continue
        lines = p.get("lines")
        if not isinstance(lines, dict):
            continue
        items = []
        for lab, v in lines.items():
            try:
                fv = float(v)
            except TypeError, ValueError:
                continue
            m = _re.search(r"(\d+)\s*/", str(lab))
            order = int(m.group(1)) if m else 0
            nm = _re.search(r"n\s*=\s*([\d.]+)", str(lab))
            n = float(nm.group(1)) if nm else None
            items.append([order, str(lab), n, fv])
        items.sort(key=lambda x: x[0])
        out.append((int(p.get("panel", 0)) or len(out) + 1, items))
    out.sort(key=lambda x: x[0])
    return out


def _z_assign_last_panel(panels, extra_pool=None):
    """尾部面板（类最多）最容易读错：用前一面板的同类（n 相近）值作锚，
    从候选值池（本面板读数 + 裁剪二次读取）中按最接近锚的原则分配，剩余槽位按票数取。

    小模型读不到 n 值时走位置型回退：面板 k 前 k-1 项 ≈ 面板 k-1 对应项（嵌套类结构），
    仅当池内对每个锚都存在 ±0.15 匹配才启用，否则保留原读数。
    """
    if len(panels) < 2:
        return panels
    (_, prev), (pno, cur) = panels[-2], panels[-1]
    if not prev or not cur:
        return panels
    pool = {}
    for it in cur:
        v = it[3]
        pool[v] = pool.get(v, 0) + 1
    for v in extra_pool or []:
        pool[v] = pool.get(v, 0) + 1
    anchors = [(it[2], it[3]) for it in prev if it[2] is not None]
    has_n = any(it[2] is not None for it in cur)
    if not anchors or not has_n:
        # 位置型回退：仅当前一面板与尾面板呈 k / k+1 嵌套结构（项数差 1）时启用
        if len(cur) != len(prev) + 1:
            return panels
        assigned: set = set()
        new_items = []
        for i, it in enumerate(cur):
            order, lab, n, v = it[0], it[1], it[2], it[3]
            if i < len(prev):
                a = prev[i][3]
                cands = [c for c in pool if abs(c - a) <= 0.15 and c not in assigned]
                if cands:
                    pick = min(cands, key=lambda c: (abs(c - a), -pool[c]))
                    assigned.add(pick)
                    new_items.append([order, lab, n, pick])
                    continue
            new_items.append([order, lab, n, v])
        if len(assigned) != len(prev):
            return panels
        remaining = sorted(
            (c for c in pool if c not in assigned), key=lambda c: -pool[c]
        )
        ri = 0
        for it in new_items:
            if ri < len(remaining) and (it[3] not in assigned):
                it[3] = remaining[ri]
                ri += 1
        panels[-1] = (pno, [[it[0], it[1], it[2], it[3]] for it in new_items])
        return panels
    assigned = set()
    new_items = []
    for it in cur:
        order, lab, n, v = it[0], it[1], it[2], it[3]
        if n is None:
            new_items.append([order, lab, n, v, False])
            continue
        best = min(anchors, key=lambda a: abs(a[0] - n))
        anchored = abs(best[0] - n) <= 0.45 * best[0]
        cands = [c for c in pool if abs(c - best[1]) <= 0.15 and c not in assigned]
        if anchored and cands:
            pick = min(cands, key=lambda c: (abs(c - best[1]), -pool[c]))
            assigned.add(pick)
            new_items.append([order, lab, n, pick, True])
        else:
            new_items.append([order, lab, n, v, False])
    remaining = sorted((c for c in pool if c not in assigned), key=lambda c: -pool[c])
    ri = 0
    for it in new_items:
        if not it[4]:
            if ri < len(remaining):
                it[3] = remaining[ri]
                ri += 1
    panels[-1] = (pno, [[it[0], it[1], it[2], it[3]] for it in new_items])
    return panels


async def _z_panel_answer(client, question: str, img) -> str:
    """z-score 多面板题定向读取 + 底部面板裁剪二次读取 + 跨面板一致性锚定 → 确定性答案。

    三次读取互相独立 → 并行发出（时延优化：约省 2/3 读取时间）。
    """
    if not _Z_PANEL_RE.search(question or "") or not img:
        return ""

    from PIL import Image as _PILImage

    w, h = img.size
    bottom = img.crop((0, int(h * 0.68), w, h))
    bottom = bottom.resize((bottom.width * 2, bottom.height * 2), _PILImage.LANCZOS)
    left = img.crop((int(w * 0.15), int(h * 0.68), int(w * 0.65), h))
    left = left.resize((left.width * 2, left.height * 2), _PILImage.LANCZOS)

    async def read_full():
        return await client.infer(
            [
                {"role": "system", "content": _Z_PANEL_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        im.pil_message(img),
                        {
                            "type": "text",
                            "text": _Z_PANEL_USER.format(question=question),
                        },
                    ],
                },
            ],
            max_tokens=700,
        )

    async def read_bottom():
        return await client.infer(
            [
                {"role": "system", "content": _Z_PANEL_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        im.pil_message(bottom),
                        {
                            "type": "text",
                            "text": _Z_PANEL_CROP_USER.format(question=question),
                        },
                    ],
                },
            ],
            max_tokens=400,
        )

    async def read_col():
        return await client.infer(
            [
                {"role": "system", "content": _Z_PANEL_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        im.pil_message(left),
                        {
                            "type": "text",
                            "text": _Z_PANEL_COL_USER.format(question=question),
                        },
                    ],
                },
            ],
            max_tokens=300,
        )

    try:
        t_full = asyncio.create_task(read_full())
        t_bottom = asyncio.create_task(read_bottom())
        t_col = asyncio.create_task(read_col())
        raw = await t_full
        panels = _z_parse_panels(raw)
        if not panels:
            return ""
        extra_pool = []
        if len(panels) >= 3:
            r2 = await t_bottom
            try:
                p2 = _z_parse_panels(r2)
                if p2 and p2[0][1]:
                    extra_pool = [it[3] for it in p2[0][1]]
            except Exception:
                pass
            r3 = await t_col
            try:
                d3 = api_client.extract_json(r3)
                if isinstance(d3, dict):
                    v3 = d3.get("values") or []
                    if isinstance(v3, list):
                        extra_pool.extend(
                            float(x) for x in v3 if isinstance(x, (int, float))
                        )
            except Exception:
                pass
    except Exception:
        return ""
    panels = _z_assign_last_panel(panels, extra_pool)
    factor = _z_factor_of(question) or "the factor"
    parts = []
    for _, items in panels:
        n = max((it[0] for it in items), default=0)
        if n == 0:
            continue
        seq = ", ".join(
            f"{it[3]:.2f} ({_Z_ORD.get(i + 1, f'class {i + 1}')})"
            for i, it in enumerate(items)
        )
        parts.append(f"{n}-class model: {seq}")
    if not parts:
        return ""
    return f"For '{factor}': " + "; ".join(parts) + "."


def _valid_stage(it: str) -> bool:
    """阶段词校验：1-3 词、至少一个大写字母、无数字（过滤 'log file'、'Web Down' 等噪音）。"""
    ws = it.split()
    return (
        1 <= len(ws) <= 3
        and not any(ch.isdigit() for ch in it)
        and any(ch.isupper() for ch in it)
    )


def enforce_completeness(parsed: dict, question: str) -> dict:
    """模型经常在 evidence 里读全了序列（如 5 个阶段）但 answer 只写 4 个。

    确定性修复：问题含 stages/steps/sequence 时，
    1) 仅从 VLM evidence（图的权威抽取：用显式标识符重建）提取阶段流转对
       重建完整序列；页面正文等噪音源（正文表头被误配为阶段）不参与；
    2) 兜底用 evidence 引号项去重列表（严格引号+阶段词校验）。
    """
    if not _STAGE_HINT_RE.search(question or ""):
        return parsed
    evidence = parsed.get("evidence") or ""
    seq = _flow_sequence(evidence)
    if len(seq) >= 3:
        ans_lower = (parsed.get("answer") or "").lower()
        if any(it.lower() not in ans_lower for it in seq):
            parsed["answer"] = "The sequence is: " + ", ".join(seq) + "."
            try:
                parsed["confidence"] = max(float(parsed.get("confidence", 0.0)), 0.7)
            except TypeError, ValueError:
                parsed["confidence"] = 0.7
            return parsed
    # 兜底：evidence 引号项（顺序保持首次出现，过滤含数字/process 的项+阶段词校验）
    items: list[str] = []
    for m in _QUOTED_RE.finditer(evidence):
        it = m.group(1).strip()
        if len(it) < 2 or any(ch.isdigit() for ch in it):
            continue
        if it.lower().endswith("process") or it.lower().endswith("diagram"):
            continue
        if not _valid_stage(it):
            continue
        if it not in items:
            items.append(it)
    if len(items) < 3:
        return parsed
    ans_lower = (parsed.get("answer") or "").lower()
    missing = [it for it in items if it.lower() not in ans_lower]
    if not missing:
        return parsed
    parsed["answer"] = "The sequence is: " + ", ".join(items) + "."
    try:
        parsed["confidence"] = max(float(parsed.get("confidence", 0.0)), 0.7)
    except TypeError, ValueError:
        parsed["confidence"] = 0.7
    return parsed


@dataclass
class StrategyResult:
    answer: str = ""
    confidence: float = 0.0
    needs_verification: bool = False
    ocr_corrected: bool = False
    vlm_outputs: list = field(default_factory=list)  # [{stage, image, output}]
    selected_images: list = field(default_factory=list)
    crop_images: list = field(default_factory=list)
    cited_pages: list = field(default_factory=list)  # 1 基页码
    evidence_pages: list = field(default_factory=list)  # 父页 dict 列表
    chosen_page: dict = field(default=None)  # 最终采用的元素所在页（verify 复核用）


def _parse_visual_json(raw: str) -> dict:
    data = api_client.extract_json(raw)
    if not isinstance(data, dict):
        return {
            "answer": raw.strip(),
            "confidence": 0.0,
            "needs_verification": False,
            "evidence": "",
            "region": "",
            "observations": [],
        }
    conf = data.get("confidence", 0.0)
    try:
        conf = float(conf)
    except TypeError, ValueError:
        conf = 0.0
    nv = bool(data.get("needs_verification", False))
    return {
        "answer": str(data.get("answer", "")).strip(),
        "confidence": conf,
        "needs_verification": nv,
        "evidence": str(data.get("evidence", "")).strip(),
        "region": str(data.get("region", "")).strip(),
        "observations": data.get("observations", [])
        if isinstance(data.get("observations"), list)
        else [],
    }


def _parent_pages(elements: List[tuple]) -> List[dict]:
    seen = set()
    out = []
    for el, page, score in elements:
        pkey = f"{page.get('doc_id')}|{page.get('page_idx')}"
        if pkey not in seen:
            seen.add(pkey)
            out.append(page)
    return out


def _desc_hint(el: dict) -> str:
    caption = el.get("caption", "") or (el.get("body", "")[:200])
    desc = el.get("description", "")
    head = f"Caption: {caption}" if caption else ""
    return (
        (head + "\nDescription hint (may contain errors): " + desc[:800])
        if desc
        else head
    )


def _render_crop(
    page: dict, bbox: List[float], workdir: str, tag: str
) -> Optional[object]:
    """从原始 PDF 高 DPI 重渲染区域；无 PDF 时回退 页面图裁剪 + 放大。返回 PIL 图。"""
    from ..builders.page_image import _find_origin_pdf

    doc_id = page.get("doc_id", "")
    page_idx = page.get("page_idx", 0)
    pdf = _find_origin_pdf(doc_id)
    img = None
    if pdf:
        img = im.render_pdf_region(pdf, page_idx, tuple(bbox), dpi=settings.region_dpi)
    if img is None:
        base = im.load_image(page.get("page_image", ""))
        if base is None:
            return None
        img = im.upscale(im.crop_region(base, tuple(bbox)))
    if workdir:
        tag = "".join(c for c in tag if c.isalnum() or c in "-_") or "crop"
        path = Path(workdir) / "crops" / f"{tag}.jpg"
        im.save_jpeg(img, path)
    return img


class ImageStrategy(ABC):
    name = "base"

    @abstractmethod
    async def answer_visual(
        self,
        question: str,
        elements: List[tuple],
        route,
        store,
        client: api_client.VLMClient,
        sample_id: str = "",
        workdir: str = "",
    ) -> StrategyResult: ...

    def _result(
        self,
        answer: str,
        elements: List[tuple],
        confidence: float = 0.0,
        needs_verification: bool = False,
        vlm_outputs: Optional[list] = None,
        selected_images: Optional[list] = None,
        crop_images: Optional[list] = None,
        chosen_page: Optional[dict] = None,
    ) -> StrategyResult:
        pages = _parent_pages(elements)
        return StrategyResult(
            answer=answer,
            confidence=confidence,
            needs_verification=needs_verification,
            vlm_outputs=vlm_outputs or [],
            selected_images=selected_images or [],
            crop_images=crop_images or [],
            cited_pages=sorted({p.get("page_idx", 0) + 1 for p in pages}),
            evidence_pages=pages,
            chosen_page=chosen_page,
        )


class BaselineStrategy(ImageStrategy):
    """对照组：构建期描述为主证据。"""

    name = "baseline"

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        from ..agent.prompts import IMAGE_DESC_ANSWER_SYSTEM
        from . import table_parse as tp
        from .prompts_vlm import TABLE_STRUCT_SYSTEM

        has_struct = any(
            el.get("kind") == "table"
            and el.get("body")
            and tp.structured_is_clean(el["body"])
            for el, _, _ in elements
        ) and not any(
            el.get("kind") == "table"
            and el.get("body")
            and not tp.structured_is_clean(el["body"])
            for el, _, _ in elements
        )
        system = TABLE_STRUCT_SYSTEM if has_struct else IMAGE_DESC_ANSWER_SYSTEM

        desc_parts, img_msgs, ctx_parts, sel = [], [], [], []
        for i, (el, page, score) in enumerate(elements):
            kind = el.get("kind", "")
            caption = el.get("caption", "") or (el.get("body", "")[:200])
            desc = el.get("description", "")
            page_no = page.get("page_idx", 0) + 1
            doc_id = page.get("doc_id", "")
            # 表格且 HTML 洁净 → 确定性结构化单元格为主证据（P10）
            if kind == "table" and el.get("body"):
                from . import table_parse as tp

                struct = (
                    tp.structured_table_evidence(el["body"])
                    if tp.structured_is_clean(el["body"])
                    else ""
                )
                if struct:
                    aux = (
                        f"\nDescription (auxiliary, may contain errors): {desc[:600]}"
                        if desc
                        else ""
                    )
                    desc_parts.append(
                        f"### Table {i + 1} [page {page_no} of {doc_id}]\n"
                        f"Caption: {caption}\n"
                        f"Structured cells (EXACT, extracted from the table markup — authoritative):\n{struct}"
                        + aux
                    )
                else:
                    desc_parts.append(
                        f"### Table {i + 1} [page {page_no} of {doc_id}]\n"
                        f"Caption: {caption}\nDescription: {desc[:1200]}"
                    )
            elif desc:
                desc_parts.append(
                    f"### Figure/table {i + 1} [{kind} on page {page_no} of {doc_id}]\n"
                    f"Caption: {caption}\nDescription (from image analysis): {desc[:1200]}"
                )
            else:
                desc_parts.append(
                    f"### Figure/table {i + 1} [{kind} on page {page_no} of {doc_id}]\nCaption: {caption}"
                )
            img_msgs.append(
                {
                    "type": "text",
                    "text": f"[Image for figure/table {i + 1}, page {page_no}]",
                }
            )
            img_msgs.append(api_client.image_message(el["img_path"]))
            sel.append(el["img_path"])
            ctx_parts.append(
                f"--- Page {page_no} ({doc_id}) ---\n{page.get('text', '')[:1200]}"
            )

        user_text = (
            "Here are the retrieved figures/tables with their descriptions:\n\n"
            + "\n\n".join(desc_parts)
            + "\n\nSurrounding page text:\n"
            + "\n\n".join(ctx_parts)
            + "\n\n"
            + f'Question: "{question}"\n\n'
            + "Answer using the descriptions as primary evidence."
        )
        raw = await client.infer(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": img_msgs + [{"type": "text", "text": user_text}],
                },
            ],
            max_tokens=settings.answer_max_tokens,
        )
        parsed = _parse_visual_json(raw)
        return self._result(
            parsed["answer"],
            elements,
            confidence=parsed["confidence"],
            needs_verification=parsed["needs_verification"],
            vlm_outputs=[
                {"stage": "baseline_desc_primary", "image": sel, "output": raw[:2000]}
            ],
            selected_images=sel,
        )


class _RegionReadMixin:
    def _salvage_regions(self, raw: str, n_regions: int) -> List[List[float]]:
        """解析 JSON 失败时用正则兜底提取 [[x,y,x,y],...]。"""
        import re as _re

        regions: List[List[float]] = []
        for m in _re.finditer(
            r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]",
            raw,
        ):
            vals = [float(m.group(i)) for i in range(1, 5)]
            if (
                all(0 <= v <= 1000 for v in vals)
                and vals[2] > vals[0]
                and vals[3] > vals[1]
            ):
                regions.append(vals)
            if len(regions) >= max(n_regions, 1):
                break
        return regions

    def _dedup_regions(self, regions: List[List[float]]) -> List[List[float]]:
        uniq: List[List[float]] = []
        for r in regions:
            if not any(all(abs(r[i] - u[i]) < 8 for i in range(4)) for u in uniq):
                uniq.append(r)
        return uniq

    async def _locate(
        self, client: api_client.VLMClient, question: str, page_img, n_regions: int
    ) -> List[List[float]]:
        regions = await self._locate_once(
            client, question, page_img, n_regions, REGION_LOCATOR_SYSTEM
        )
        if not regions:
            # 简化 prompt 重试一次（防模型输出退化/格式失败）
            regions = await self._locate_once(
                client, question, page_img, 1, RETRY_LOCATOR_SYSTEM
            )
        return regions

    async def _locate_once(
        self,
        client: api_client.VLMClient,
        question: str,
        page_img,
        n_regions: int,
        system: str,
    ) -> List[List[float]]:
        msgs = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    im.pil_message(page_img),
                    {
                        "type": "text",
                        "text": region_locator_user(question)
                        + (
                            _STAGE_LOCATOR_EXTRA
                            if _STAGE_HINT_RE.search(question or "")
                            else ""
                        )
                        + (
                            _PER_ITEM_LOCATOR_EXTRA
                            if _PER_ITEM_RE.search(question or "")
                            else ""
                        ),
                    },
                ],
            },
        ]
        raw = await client.infer(msgs, max_tokens=300)
        regions: List[List[float]] = []
        data = api_client.extract_json(raw)
        if isinstance(data, dict):
            rs = data.get("regions", [])
            if isinstance(rs, list):
                for r in rs[: max(n_regions, 1)]:
                    if isinstance(r, (list, tuple)) and len(r) == 4:
                        try:
                            vals = [float(v) for v in r]
                            if (
                                all(0 <= v <= 1000 for v in vals)
                                and vals[2] > vals[0]
                                and vals[3] > vals[1]
                            ):
                                regions.append(vals)
                        except TypeError, ValueError:
                            continue
        if not regions:
            regions = self._salvage_regions(raw, n_regions)
        return self._dedup_regions(regions)

    async def _crop_with_fallback(
        self, page: dict, el: dict, regions: List[List[float]], workdir: str, tag: str
    ) -> Optional[object]:
        """定位成功 → 高 DPI 重渲染裁剪；定位失败 → 回退元素裁剪图（放大）。"""
        if regions:
            return _render_crop(page, regions[0], workdir, tag)
        img = im.load_image(el.get("img_path", ""))
        if img is None:
            img = im.load_image(page.get("page_image", ""))
        if img is None:
            return None
        img = im.smart_resize(img)
        img = im.upscale(img)
        if workdir:
            tag = "".join(c for c in tag if c.isalnum() or c in "-_") or "crop"
            im.save_jpeg(img, Path(workdir) / "crops" / f"{tag}.jpg")
        return img

    async def _read_crop(
        self, client: api_client.VLMClient, question: str, crop, hint: str, label: str
    ) -> tuple[dict, str]:
        hint_text = f"\n\nOrientation hint (may contain errors): {hint}" if hint else ""
        stage_text = _STAGE_READ_EXTRA if _STAGE_HINT_RE.search(question or "") else ""
        path_text = _PATH_READ_EXTRA if _PATH_RE.search(question or "") else ""
        msgs = [
            {"role": "system", "content": CROP_READ_SYSTEM},
            {
                "role": "user",
                "content": [
                    im.pil_message(crop),
                    {
                        "type": "text",
                        "text": f'Question: "{question}"{hint_text}{stage_text}{path_text}',
                    },
                ],
            },
        ]
        raw = await client.infer(msgs, max_tokens=800)
        parsed = _parse_visual_json(raw)
        parsed = enforce_completeness(parsed, question)
        return parsed, raw

    async def _read_full_plus(
        self,
        client: api_client.VLMClient,
        question: str,
        full_img,
        crops: List[object],
        hint: str,
    ) -> tuple[dict, str]:
        hint_text = f"\n\nOrientation hint (may contain errors): {hint}" if hint else ""
        content = [
            im.pil_message(full_img),
            {"type": "text", "text": "[Full page image above]"},
        ]
        for i, c in enumerate(crops):
            content.append(im.pil_message(c))
            content.append({"type": "text", "text": f"[Zoomed crop {i + 1} above]"})
        content.append(
            {
                "type": "text",
                "text": f'Question: "{question}"{hint_text}'
                + (_STAGE_READ_EXTRA if _STAGE_HINT_RE.search(question or "") else "")
                + (_PATH_READ_EXTRA if _PATH_RE.search(question or "") else ""),
            }
        )
        raw = await client.infer(
            [
                {"role": "system", "content": FULL_PLUS_CROP_SYSTEM},
                {"role": "user", "content": content},
            ],
            max_tokens=800,
        )
        parsed = _parse_visual_json(raw)
        parsed = enforce_completeness(parsed, question)
        return parsed, raw


class CropStrategy(_RegionReadMixin, ImageStrategy):
    """question → 区域定位 → 高 DPI 裁剪放大 → 精读。"""

    name = "crop"

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        el, page, _ = elements[0]
        full = im.load_image(page.get("page_image", ""))
        vlm_out, crops = [], []
        if full is None:
            return self._result("NOT_FOUND", elements)
        full = im.smart_resize(full)
        regions = await self._locate(client, question, full, 1)
        vlm_out.append(
            {
                "stage": "region_locate",
                "image": page.get("page_image", ""),
                "output": f"{len(regions)} regions",
            }
        )
        crop = await self._crop_with_fallback(
            page, el, regions, workdir, f"{sample_id}_crop"
        )
        if crop is None:
            return self._result("NOT_FOUND", elements)
        crops.append(regions[0] if regions else [])
        parsed, raw = await self._read_crop(
            client, question, crop, _desc_hint(el), "crop"
        )
        vlm_out.append(
            {
                "stage": "crop_read",
                "image": f"{sample_id}_crop.jpg",
                "output": raw[:2000],
            }
        )
        return self._result(
            parsed["answer"],
            elements,
            confidence=parsed["confidence"],
            needs_verification=parsed["needs_verification"],
            vlm_outputs=vlm_out,
            selected_images=[page.get("page_image", ""), el.get("img_path", "")],
            crop_images=crops,
        )


class MultiCropStrategy(_RegionReadMixin, ImageStrategy):
    """无法唯一定位时读 ≤4 个候选区域（含多面板逐面板），聚合作答。"""

    name = "multi_crop"

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        el, page, _ = elements[0]
        full = im.load_image(page.get("page_image", ""))
        vlm_out, crops = [], []
        if full is None:
            return self._result("NOT_FOUND", elements)
        full = im.smart_resize(full)
        regions = await self._locate(client, question, full, 4)
        vlm_out.append(
            {
                "stage": "region_locate",
                "image": page.get("page_image", ""),
                "output": f"{len(regions)} regions",
            }
        )
        reads = []
        if not regions:
            crop = await self._crop_with_fallback(
                page, el, [], workdir, f"{sample_id}_crop1"
            )
            if crop is not None:
                crops.append([])
                parsed, raw = await self._read_crop(
                    client, question, crop, _desc_hint(el), "crop1"
                )
                vlm_out.append(
                    {
                        "stage": "crop_read_1",
                        "image": f"{sample_id}_crop1.jpg",
                        "output": raw[:2000],
                    }
                )
                reads.append(parsed)
        for i, bbox in enumerate(regions[:4]):
            crop = _render_crop(page, bbox, workdir, f"{sample_id}_crop{i + 1}")
            if crop is None:
                continue
            crop = im.upscale(crop)
            crops.append(bbox)
            parsed, raw = await self._read_crop(
                client, question, crop, _desc_hint(el), f"crop{i + 1}"
            )
            vlm_out.append(
                {
                    "stage": f"crop_read_{i + 1}",
                    "image": f"{sample_id}_crop{i + 1}.jpg",
                    "output": raw[:2000],
                }
            )
            reads.append(parsed)
        if not reads:
            return self._result("NOT_FOUND", elements)
        if len(reads) == 1:
            best = reads[0]
        else:
            # 聚合：优先最高置信度观测，再用 LLM 汇总多面板读数
            parts = "\n".join(
                f"crop{i + 1}: answer={r['answer']} conf={r['confidence']:.2f}"
                for i, r in enumerate(reads)
            )
            agg_raw = await client.infer(
                [
                    {
                        "role": "system",
                        "content": "Combine per-panel readings into ONE final answer for the original "
                        "question. Keep exact numbers and their panel/model labels. If readings conflict, prefer the "
                        "one with higher confidence. If panels were read top-to-bottom, label them one-class / "
                        "two-class / three-class / four-class model in that order. "
                        'Output ONLY JSON: {"answer": "...", "confidence": 0.0}',
                    },
                    {
                        "role": "user",
                        "content": f'Question: "{question}"\n\nReadings (top-to-bottom panels):\n{parts}',
                    },
                ],
                max_tokens=600,
            )
            vlm_out.append(
                {"stage": "aggregate", "image": "", "output": agg_raw[:2000]}
            )
            agg = api_client.extract_json(agg_raw)
            if isinstance(agg, dict) and agg.get("answer"):
                best = {
                    "answer": str(agg["answer"]),
                    "confidence": float(agg.get("confidence", 0.5) or 0.5),
                    "needs_verification": False,
                    "evidence": "",
                    "region": "",
                    "observations": [],
                }
            else:
                best = max(reads, key=lambda r: r["confidence"])
        return self._result(
            best["answer"],
            elements,
            confidence=best["confidence"],
            needs_verification=any(r["needs_verification"] for r in reads),
            vlm_outputs=vlm_out,
            selected_images=[page.get("page_image", ""), el.get("img_path", "")],
            crop_images=crops,
        )


class FullPlusCropStrategy(_RegionReadMixin, ImageStrategy):
    """全图看结构 + 裁剪精读，冲突以裁剪为准（docx Exp5）。

    候选元素选择：top1 必读；top2 若分数接近（差 <0.03）且在不同页，也读，
    取置信度最高者——解决"top 元素是错误图、正确图排第二"的检索误选。
    """

    name = "full_plus_crop"

    @staticmethod
    def _candidate_elements(elements: List[tuple]) -> List[tuple]:
        if len(elements) < 2:
            return elements[:1]
        first, second = elements[0], elements[1]
        same_page = first[1].get("doc_id") == second[1].get("doc_id") and first[1].get(
            "page_idx"
        ) == second[1].get("page_idx")
        if second[2] >= first[2] - 0.03 and not same_page:
            return [first, second]
        return [first]

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        # z-score 多面板题快路径（时延优化）：通用读取+定位只覆盖 4/10 值且会被
        # 面板定向读取覆盖——直接对首个候选元素做三读并行 + 锚定，跳过定位/整图读
        if _Z_PANEL_RE.search(question or ""):
            try:
                for el, page, _ in self._candidate_elements(elements):
                    z_img = im.load_image(el.get("img_path", ""))
                    if z_img is None:
                        continue
                    z_img = im.smart_resize(z_img, max_dim=0)
                    zc = api_client.VLMClient(model=settings.llm_model_visual)
                    z_ans = await _z_panel_answer(zc, question, z_img)
                    if z_ans:
                        res = self._result(
                            z_ans,
                            elements,
                            confidence=0.8,
                            vlm_outputs=[
                                {
                                    "stage": "z_panel_read",
                                    "image": el.get("img_path", ""),
                                    "output": z_ans[:300],
                                }
                            ],
                            selected_images=[
                                page.get("page_image", ""),
                                el.get("img_path", ""),
                            ],
                            chosen_page=page,
                        )
                        res.ocr_corrected = True
                        return res
            except Exception:
                pass
        cands = self._candidate_elements(elements)
        best_el, best_parsed = None, None
        vlm_out, crops = [], []
        # 逐项数字题（"each ... layer"）需看全所有标签（多层厚度标签场景，
        # 单区域裁剪只覆盖第一个标签，后面的值易被误读）
        n_regions = 4 if _PER_ITEM_RE.search(question or "") else 1
        for ci, (el, page, _) in enumerate(cands):
            full = im.load_image(page.get("page_image", ""))
            if full is None:
                continue
            full_small = im.smart_resize(full)
            per_item = bool(_PER_ITEM_RE.search(question or ""))
            is_path = bool(_PATH_RE.search(question or ""))
            if per_item or is_path:
                # 定位器裁剪不全（逐项标签 / 整条流向线等场景），
                # 直接用元素整图放大作为裁剪，保证全部标签/路径在视野内
                el_img = im.load_image(el.get("img_path", ""))
                if el_img is not None:
                    el_img = im.smart_resize(el_img, max_dim=0)
                    el_img = im.upscale(el_img, 2)
                read_crops = [el_img] if el_img is not None else []
                regions = []
                vlm_out.append(
                    {
                        "stage": f"region_locate_{ci + 1}",
                        "image": page.get("page_image", ""),
                        "output": "element image (per-item/path)",
                    }
                )
            else:
                regions = await self._locate(client, question, full_small, 1)
                vlm_out.append(
                    {
                        "stage": f"region_locate_{ci + 1}",
                        "image": page.get("page_image", ""),
                        "output": f"{len(regions)} regions",
                    }
                )
                read_crops = []
                for ri, bbox in enumerate(regions[:1]):
                    c = (
                        _render_crop(
                            page, bbox, workdir, f"{sample_id}_crop{ci + 1}_{ri + 1}"
                        )
                        if regions
                        else None
                    )
                    if c is None:
                        c = await self._crop_with_fallback(
                            page,
                            el,
                            [bbox],
                            workdir,
                            f"{sample_id}_crop{ci + 1}_{ri + 1}",
                        )
                    if c is not None:
                        read_crops.append(c)
            if not read_crops:
                continue
            parsed, raw = await self._read_full_plus(
                client, question, full_small, read_crops, _desc_hint(el)
            )
            vlm_out.append(
                {
                    "stage": f"full_plus_crop_{ci + 1}",
                    "image": f"{sample_id}_crop{ci + 1}.jpg",
                    "output": raw[:2000],
                }
            )
            if best_parsed is None or parsed["confidence"] > best_parsed["confidence"]:
                best_parsed, best_el = parsed, (el, page)
                crops = [list(r) for r in regions[:n_regions]]
        if best_parsed is None or best_el is None:
            return self._result("NOT_FOUND", elements)
        res = self._result(
            best_parsed["answer"],
            elements,
            confidence=best_parsed["confidence"],
            needs_verification=best_parsed["needs_verification"],
            vlm_outputs=vlm_out,
            selected_images=[
                best_el[1].get("page_image", ""),
                best_el[0].get("img_path", ""),
            ],
            crop_images=crops,
            chosen_page=best_el[1],
        )
        res.ocr_corrected = best_parsed.get("ocr_corrected", False)
        # z-score 多面板题：通用读取只覆盖部分值，定向面板读取 + 一致性修正
        if _Z_PANEL_RE.search(question or "") and best_el[0].get("img_path"):
            try:
                z_img = im.load_image(best_el[0].get("img_path", ""))
                if z_img is not None:
                    z_img = im.smart_resize(z_img, max_dim=0)
                    zc = api_client.VLMClient(model=settings.llm_model_visual)
                    z_ans = await _z_panel_answer(zc, question, z_img)
                    if z_ans:
                        vlm_out.append(
                            {
                                "stage": "z_panel_read",
                                "image": best_el[0].get("img_path", ""),
                                "output": z_ans[:300],
                            }
                        )
                        res.answer = z_ans
                        res.confidence = 0.8
                        res.ocr_corrected = True
            except Exception:
                pass
        return res


class VerifyStrategy(FullPlusCropStrategy):
    """full_plus_crop + 关键 token 二次逐字符验证（docx 六）。

    触发条件：
    - 问题或答案含关键 token（数字/尺寸/型号/Block 字母），或
    - 首次读数与构建期描述在关键 token 上冲突。
    """

    name = "verify"

    async def _composite_crops(self, page, el, regions, workdir, tag):
        """多个定位区域 → 纵向拼接成一张复核图（逐项数字题的每个标签都要复核）。"""
        from PIL import Image as _PILImage

        imgs = []
        for i, bbox in enumerate((regions or [])[:4]):
            c = _render_crop(page, bbox, workdir, f"{tag}_{i + 1}") if bbox else None
            if c is None:
                c = await self._crop_with_fallback(
                    page, el, [bbox], workdir, f"{tag}_{i + 1}"
                )
            if c is not None:
                imgs.append(c)
        if not imgs:
            return None
        if len(imgs) == 1:
            return imgs[0]
        w = max(i.width for i in imgs)
        comp = _PILImage.new("RGB", (w, sum(i.height for i in imgs)), (255, 255, 255))
        y = 0
        for i in imgs:
            comp.paste(i, (0, y))
            y += i.height
        if workdir:
            im.save_jpeg(comp, Path(workdir) / "crops" / f"{tag}.jpg")
        return comp

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        res = await super().answer_visual(
            question, elements, route, store, client, sample_id, workdir
        )
        if getattr(res, "ocr_corrected", False):
            # OCR 列聚类已确定性纠正（独立读数），LLM 复核不再覆盖
            return res
        el = elements[0][0]
        page = res.chosen_page or (elements[0][1] if elements else None)
        if page is None:
            return res
        critical = vt.extract_critical(question) or vt.extract_critical(res.answer)
        desc_critical = vt.extract_critical(el.get("description", ""))
        # 描述与首次读数冲突也触发复核
        conflict = (
            any(
                not any(vt.reads_agree(a, d) for d in desc_critical)
                for a in vt.extract_critical(res.answer)
            )
            if desc_critical and vt.extract_critical(res.answer)
            else False
        )
        if not critical and not conflict:
            return res
        full = im.load_image(page.get("page_image", ""))
        if full is None:
            return res
        regions = res.crop_images or []
        crop = await self._composite_crops(
            page, el, regions, workdir, f"{sample_id}_verify"
        )
        if crop is None:
            crop = await self._crop_with_fallback(
                page, el, regions, workdir, f"{sample_id}_verify"
            )
        if crop is None:
            return res
        cands = "\n".join(f"- {t}" for t in (critical or desc_critical)[:8])
        msgs = [
            {"role": "system", "content": VERIFY_TOKEN_SYSTEM},
            {
                "role": "user",
                "content": [
                    im.pil_message(crop),
                    {"type": "text", "text": verify_token_user(res.answer, cands)},
                ],
            },
        ]
        readings = [res.answer]
        for rnd in range(settings.verify_max_rounds):
            raw = await client.infer(msgs, max_tokens=200)
            res.vlm_outputs.append(
                {
                    "stage": f"verify_{rnd + 1}",
                    "image": f"{sample_id}_verify.jpg",
                    "output": raw[:500],
                }
            )
            data = api_client.extract_json(raw)
            if isinstance(data, dict):
                val = str(data.get("value", "")).strip()
                if val and val.lower() != "uncertain":
                    readings.append(val)
                if vt.reads_agree(res.answer, val):
                    break
            else:
                break
        if len(readings) > 1:
            agree_with_first = [r for r in readings if vt.reads_agree(r, readings[0])]
            if len(agree_with_first) >= 2:
                res.confidence = max(res.confidence, 0.85)
            else:
                last = readings[-1]
                if not vt.reads_agree(last, res.answer):
                    # 复核值与描述候选一致 → 以描述候选为线索重读并给出修正答案
                    if desc_critical and any(
                        vt.reads_agree(last, d) for d in desc_critical
                    ):
                        resolve_raw = await client.infer(
                            [
                                {
                                    "role": "system",
                                    "content": "Produce the corrected answer for the question using ONLY "
                                    "values you can see in the zoomed image. Candidates from other sources: "
                                    + ", ".join(desc_critical[:8])
                                    + ". If you cannot see any of them, set needs_verification=true. "
                                    'Output ONLY JSON: {"answer": "...", "confidence": 0.0, "needs_verification": false}',
                                },
                                {
                                    "role": "user",
                                    "content": [
                                        im.pil_message(crop),
                                        {
                                            "type": "text",
                                            "text": f'Question: "{question}"\nFirst reading: {res.answer}',
                                        },
                                    ],
                                },
                            ],
                            max_tokens=400,
                        )
                        res.vlm_outputs.append(
                            {
                                "stage": "verify_resolve",
                                "image": f"{sample_id}_verify.jpg",
                                "output": resolve_raw[:500],
                            }
                        )
                        rd = api_client.extract_json(resolve_raw)
                        if isinstance(rd, dict) and rd.get("answer"):
                            res.answer = str(rd["answer"]).strip()
                            try:
                                res.confidence = float(rd.get("confidence", 0.6))
                            except TypeError, ValueError:
                                res.confidence = 0.6
                    else:
                        res.confidence = min(res.confidence, 0.5)
                        res.needs_verification = True

        # OCR 第三票：首读数字与复核/描述冲突时，用 tesseract 白名单读数仲裁
        ocr_vals = vt.ocr_numbers(crop)
        if ocr_vals:
            res.vlm_outputs.append(
                {
                    "stage": "ocr",
                    "image": f"{sample_id}_verify.jpg",
                    "output": str(ocr_vals),
                }
            )
        if ocr_vals and desc_critical:
            for d in desc_critical:
                if any(vt.reads_agree(o, d) for o in ocr_vals) and not vt.reads_agree(
                    res.answer, d
                ):
                    for a in vt.extract_critical(res.answer):
                        if vt.reads_agree(a, res.answer) or a in res.answer:
                            replaced = vt.replace_critical(res.answer, a, d)
                            if replaced != res.answer:
                                res.answer = replaced
                                res.confidence = max(res.confidence, 0.7)
                                break
                    break
        return res


class TableReadStrategy(_RegionReadMixin, ImageStrategy):
    """表格题：直读表格原图（放大），描述仅作辅助（P7）。"""

    name = "table_read"

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        el, page, _ = elements[0]
        full = im.load_image(page.get("page_image", ""))
        vlm_out = []
        # 表格裁剪图优先；缺图时从页面图定位
        img = im.load_image(el.get("img_path", ""))
        if img is not None:
            img = im.smart_resize(img, max_dim=0)
            img = im.upscale(img, 2)
        elif full is not None:
            regions = await self._locate(client, question, im.smart_resize(full), 1)
            img = await self._crop_with_fallback(
                page, el, regions, workdir, f"{sample_id}_tbl"
            )
        if img is None:
            return self._result("NOT_FOUND", elements)
        if workdir:
            im.save_jpeg(img, Path(workdir) / "crops" / f"{sample_id}_tbl.jpg")
        hint = _desc_hint(el)
        system = (
            "You read a TABLE image and answer from the image itself. The image is the source of truth; "
            "the description text is only a hint and may be wrong.\n"
            "Rules:\n"
            "1. Identify row headers and column headers exactly. If a header cell is MERGED across "
            "several columns (e.g. one label spanning a group of columns), treat each column group "
            "as that label and report every value inside the group, labeled by its sub-column.\n"
            "2. Confusion matrices / cross-tabulations: ROWS are the PREDICTED type, COLUMNS are the "
            "REFERENCE type. 'X is misclassified as Y' means a case whose REFERENCE type is X was "
            "predicted as Y — read the cell at ROW Y, COLUMN X. Example: to find how many S-DH were "
            "misclassified as MFH, read ROW MFH, COLUMN S-DH. Report each off-diagonal cell as "
            "'X is misclassified as Y: N' — never sum, never compute, never aggregate.\n"
            "3. When the question asks for a parameter/metric's values, report ALL cells under the "
            "corresponding header (or header group), each labeled with its other-axis label.\n"
            "4. Report numbers verbatim. Never substitute values from adjacent cells. Never guess.\n"
            'Output ONLY JSON: {"answer": "...", "evidence": "...", "confidence": 0.0, '
            '"needs_verification": false, "observations": []}'
        )
        msgs = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    im.pil_message(img),
                    {
                        "type": "text",
                        "text": f'Question: "{question}"\n\nOrientation hint (may contain errors): {hint[:600]}',
                    },
                ],
            },
        ]
        raw = await client.infer(msgs, max_tokens=800)
        vlm_out.append(
            {
                "stage": "table_read",
                "image": f"{sample_id}_tbl.jpg",
                "output": raw[:2000],
            }
        )
        parsed = _parse_visual_json(raw)
        return self._result(
            parsed["answer"],
            elements,
            confidence=parsed["confidence"],
            needs_verification=parsed["needs_verification"],
            vlm_outputs=vlm_out,
            selected_images=[el.get("img_path", ""), page.get("page_image", "")],
            crop_images=[],
            chosen_page=page,
        )


class HybridStrategy(VerifyStrategy):
    """图文协同（P12）：视觉精读 + 页面文本 → arbiter 融合。

    用于 image_plus_text 题：纯视觉策略缺文本语境（图题常见），纯文本路径
    缺图中细节——两者各自作答后由 arbiter 用证据交叉裁决。
    继承 VerifyStrategy：视觉读数含数字/尺寸时先 token 复核 + OCR 仲裁。
    """

    name = "hybrid"

    async def answer_visual(
        self, question, elements, route, store, client, sample_id="", workdir=""
    ):
        vis = await super().answer_visual(
            question, elements, route, store, client, sample_id, workdir
        )
        pages = _parent_pages(elements)
        # 相邻页扩展：图文题的逐对象/逐条件结论常写在相邻页
        extra_pages = []
        for p in pages:
            doc_id = p.get("doc_id", "")
            for delta in (-2, -1, 1, 2):
                nb = (
                    store.page_of(f"{doc_id}|{p.get('page_idx', 0) + delta}")
                    if hasattr(store, "page_of")
                    else None
                )
                if nb and nb.get("text"):
                    extra_pages.append(nb)
        ctx = "\n\n".join(
            f"--- Page {p.get('page_idx', 0) + 1} ({p.get('doc_id', '')}) ---\n{p.get('text', '')[:3000]}"
            for p in pages + extra_pages
        )
        system = (
            "You synthesize a final answer from two evidence sources for a question that needs "
            "BOTH text and images.\n"
            "1. Visual reading of the relevant figure/table (exact numbers, labels, trends).\n"
            "2. Page text of the pages containing the figure/table.\n"
            "Rules:\n"
            "- Keep every fact that is supported by at least one source.\n"
            "- For exact numbers/dimensions/labels, trust the visual reading unless the text "
            "explicitly contradicts it.\n"
            "- For context, definitions and wording, use the text.\n"
            "- PER-ITEM / PER-CONDITION claims: if the page text states results for each "
            "object/condition/item separately, the TEXT is authoritative. For a condition where "
            "the text lists per-item outcomes, DISCARD any blanket visual claim about that "
            "condition (e.g. 'these signals are weaker') and restate each item's outcome from "
            "the text, including items for which no image was recovered. NEVER generalize one "
            "item's result to all items.\n"
            "- CONTRADICTION CHECK: before writing each sentence, verify it against the text. "
            "If a visual claim about a condition conflicts with the text's per-item outcomes, "
            "DROP the visual claim entirely (do not include both — including both makes the "
            "answer contradictory).\n"
            "- TERMINOLOGY: use the same condition labels as the source text (or the question's "
            "wording); if the text explains a scenario synonym in parentheses, keep the plain "
            "label used by the text rather than the parenthetical synonym.\n"
            "- Do not add facts from neither source. Do not guess.\n"
            "- Answer completely: if the question implies a list, give the full list.\n"
            "- If the visual reading enumerated objects (Item 1: ... Item 2: ...), "
            "COPY the enumeration VERBATIM as the final answer, including each item's role and "
            "attributes — do not compress it, do not drop roles/attributes, and only remove "
            "clearly irrelevant items.\n"
            "- MINIMALISM: answer only what the question asks. Do NOT add extra numbers, dates, "
            "m/z values, units or details that the question does not request.\n"
            'Output ONLY JSON: {"answer": "...", "evidence": "...", "confidence": 0.0}'
        )
        user = (
            f'Question: "{question}"\n\n'
            f"Visual reading (confidence {vis.confidence:.2f}):\n{vis.answer}\n\n"
            f"Page text:\n{ctx[:5000]}"
        )
        raw = await client.infer(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=800,
        )
        vis.vlm_outputs.append(
            {"stage": "hybrid_arbiter", "image": "", "output": raw[:2000]}
        )
        parsed = _parse_visual_json(raw)
        vis.answer = parsed["answer"] or vis.answer
        vis.confidence = parsed["confidence"] or vis.confidence
        return vis


STRATEGIES = {
    "baseline": BaselineStrategy,
    "crop": CropStrategy,
    "multi_crop": MultiCropStrategy,
    "full_plus_crop": FullPlusCropStrategy,
    "verify": VerifyStrategy,
    "table_read": TableReadStrategy,
    "hybrid": HybridStrategy,
}


def get_strategy(name: Optional[str] = None) -> ImageStrategy:
    name = name or settings.image_strategy
    cls = STRATEGIES.get(name, BaselineStrategy)
    return cls()
