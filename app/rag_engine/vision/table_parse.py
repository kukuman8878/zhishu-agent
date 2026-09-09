"""MinerU HTML 表格确定性解析（P10，docx 九）。

Element.body 保留原始 <table> HTML：展开 colspan/rowspan 成 2D 网格，
产出结构化单元格证据（行列标签+逐格值），替代"线性化描述"作为表格题主证据。

描述是 VLM 转录（可能丢行列方向/读错数字）；HTML 是 MinerU 结构化输出（保真）。
"""

from __future__ import annotations

import html as html_mod
import re
from typing import List, Optional, Tuple

_TAG_RE = re.compile(r"<[^>]+>")
_COLSPAN_RE = re.compile(r'colspan\s*=\s*["\']?(\d+)', re.I)
_ROWSPAN_RE = re.compile(r'rowspan\s*=\s*["\']?(\d+)', re.I)


def _clean_cell(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return html_mod.unescape(text).strip()


def parse_grid(table_html: str) -> List[List[str]]:
    """展开 colspan/rowspan，返回 2D 网格（缺失格补空串）。"""
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S)
    if not rows_html:
        return []
    cells: List[List[dict]] = []
    for tr in rows_html:
        row_cells = []
        for m in re.finditer(r"<(td|th)([^>]*)>(.*?)</\1>", tr, flags=re.I | re.S):
            attrs, body = m.group(2), m.group(3)
            cs = int(_COLSPAN_RE.search(attrs).group(1)) if _COLSPAN_RE.search(attrs) else 1
            rs = int(_ROWSPAN_RE.search(attrs).group(1)) if _ROWSPAN_RE.search(attrs) else 1
            row_cells.append({"text": _clean_cell(body), "colspan": cs, "rowspan": rs})
        cells.append(row_cells)
    n_rows = len(cells)
    grid: List[List[str]] = [[] for _ in range(n_rows)]
    occupied = set()
    for r in range(n_rows):
        c = 0
        for cell in cells[r]:
            while (r, c) in occupied:
                c += 1
            need = c + cell["colspan"]
            while len(grid[r]) < need:
                grid[r].append("")
            grid[r][c] = cell["text"]
            for dr in range(1, cell["rowspan"]):
                rr = r + dr
                if rr >= len(grid):
                    grid.append([])
                while len(grid[rr]) < need:
                    grid[rr].append("")
                for dc in range(cell["colspan"]):
                    occupied.add((rr, c + dc))
            c += cell["colspan"]
    col_count = max(len(row) for row in grid)
    for row in grid:
        while len(row) < col_count:
            row.append("")
    return grid


def _is_numeric(s: str) -> bool:
    return bool(re.fullmatch(r"[-+.,\d\s()%]*\d[-+.,\d\s()%]*", s))


_STRUCT_WORDS = {"prediction", "predicted", "reference", "actual", "state", "class", "true", "label"}


def _row_label(row: List[str]) -> Optional[Tuple[int, str]]:
    """行标签 = 前两列中第一个非数字短文本（跳过结构词如 Prediction）。"""
    for idx in (0, 1):
        lab = row[idx].strip()
        if (
            lab
            and len(lab) <= 12
            and not _is_numeric(lab)
            and lab.lower() not in _STRUCT_WORDS
        ):
            return idx, lab
    return None


def _looks_like_data_row(row: List[str]) -> bool:
    """含 ≥2 个 '(x, y)' 置信区间格式的数值格 → 数据行。"""
    n = sum(1 for c in row[1:] if re.search(r"\([-+\d.,\s]+\)", c))
    return n >= 2


def confusion_from_grid(grid: List[List[str]]) -> Optional[List[str]]:
    """混淆矩阵：表头标签集合 == 数据行标签集合 → 逐 off-diagonal 生成句式。"""
    if len(grid) < 3:
        return None
    data = []
    for r in range(1, len(grid)):
        hit = _row_label(grid[r])
        if hit:
            data.append((r, hit[0], hit[1]))
    if len(data) < 3:
        return None
    labels = {d[2] for d in data}
    hdr = None
    for r in range(0, data[0][0]):
        cells = {c.strip() for c in grid[r] if c.strip()}
        if labels <= cells:
            hdr = r
            break
    if hdr is None:
        return None
    lines = []
    for r, _, plab in data:
        for c in range(len(grid[r])):
            ref = grid[hdr][c].strip() if c < len(grid[hdr]) else ""
            if not ref or ref.lower() == plab.lower():
                continue
            val = grid[r][c].strip()
            if val and _is_numeric(val):
                lines.append(f"{ref} is misclassified as {plab}: {val}")
    return lines or None


def grid_evidence(grid: List[List[str]], max_cells: int = 120) -> str:
    """网格 → 逐格证据文本。首行若为数据行（无表头），按行标签+值列表输出。"""
    if not grid:
        return ""
    first_is_data = _looks_like_data_row(grid[0])
    lines = []
    n = 0
    if first_is_data:
        for r in range(0, len(grid)):
            row = grid[r]
            lab = row[0].strip() or "(no label)"
            vals = [v.strip() for v in row[1:] if v.strip()]
            lines.append(f"{lab}: " + ", ".join(vals[:12]))
            n += len(vals)
            if n >= max_cells:
                lines.append("... (truncated)")
                break
        return "\n".join(lines)
    header = grid[0]
    for r in range(1, len(grid)):
        row = grid[r]
        parts = [f"{row[0] or '(no row label)'}:"]
        for c in range(1, len(row)):
            if n >= max_cells:
                break
            col_lab = header[c] or f"col{c}"
            parts.append(f"{col_lab} = {row[c] or '(empty)'}")
            n += 1
        lines.append(" | ".join(parts))
        if n >= max_cells:
            lines.append("... (truncated)")
            break
    return "\n".join(lines)


def structured_table_evidence(body_html: str, kind_hint: str = "") -> str:
    """入口：HTML → 证据文本。混淆矩阵走专用表述，否则通用逐格。"""
    grid = parse_grid(body_html)
    if not grid:
        return ""
    conf = confusion_from_grid(grid)
    if conf:
        return (
            "Confusion matrix (each line reads: '<reference category> is misclassified as "
            "<predicted category>: count'):\n" + "\n".join(conf)
        )
    return "Table cells (row label | column header = value):\n" + grid_evidence(grid)


def structured_is_clean(body_html: str) -> bool:
    """结构化表是否可信：MinerU HTML 有时单元格混排（如 '10,157 | -13.5% 73,434'），
    此时确定性证据不可用，应回退到 图+描述 路径。"""
    grid = parse_grid(body_html)
    if not grid or len(grid) < 2:
        return False
    bad = 0
    total = 0
    for r in range(1, len(grid)):
        for c in range(1, len(grid[r])):
            v = grid[r][c].strip()
            if not v:
                continue
            total += 1
            # 一个单元格里混了两个数字（如 '-13.5% 73,434 -86.2%'）→ 混排坏表
            if len(re.findall(r"\d", v)) >= 4 and ("%" in v or len(re.findall(r"[\d,]+", v)) >= 2):
                bad += 1
    if total == 0:
        return False
    return bad / total < 0.25


def has_structured_table(el: dict) -> bool:
    return bool(el.get("kind") == "table" and el.get("body"))
