"""MinerU content_list.json 解析：重建文档/页面/元素结构。

设计要点（借鉴"父文档检索"思想，自行实现）：
- 元素（图/表/公式）与其所在页强绑定，检索命中元素即返回整页证据；
- 页面文本按阅读顺序重排，表格转 Markdown 内联，图注/公式内联，保证
  文本向量与 BM25 都建立在"完整页面"粒度上；
- 图片路径在构建期解析为绝对路径，运行期直接可用。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from pathlib import Path
from typing import List, Optional

from ..data_model import DocRecord, Element, Page

_TAG_RE = re.compile(r"<[^>]+>")
_COLSPAN_RE = re.compile(r'colspan\s*=\s*["\']?(\d+)', re.I)
_ROWSPAN_RE = re.compile(r'rowspan\s*=\s*["\']?(\d+)', re.I)


def _clean_cell(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return html_mod.unescape(text).strip()


def _split_cells(tr: str) -> List[str]:
    return re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.I | re.S) or re.findall(
        r"<th[^>]*>(.*?)</th>", tr, flags=re.I | re.S
    )


def html_table_to_markdown(table_html: str) -> str:
    """把 MinerU 输出的 HTML 表格转成 GitHub 风格 Markdown。

    正确展开 colspan 与 rowspan，单元格数不齐按最长行补齐；
    多行单元格内容以 <br> 保留换行。
    """
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S)
    if not rows_html:
        return ""
    # 收集每行的单元格 (text, colspan, rowspan)
    cells: List[List[dict]] = []
    for tr in rows_html:
        row_cells = []
        for m in re.finditer(r"<(td|th)([^>]*)>(.*?)</\1>", tr, flags=re.I | re.S):
            attrs, body = m.group(2), m.group(3)
            cs = int(_COLSPAN_RE.search(attrs).group(1)) if _COLSPAN_RE.search(attrs) else 1
            rs = int(_ROWSPAN_RE.search(attrs).group(1)) if _ROWSPAN_RE.search(attrs) else 1
            text = _clean_cell(body)
            row_cells.append({"text": text, "colspan": cs, "rowspan": rs})
        cells.append(row_cells)
    if not cells:
        return ""
    n_rows = len(cells)
    grid: List[List[str]] = [[] for _ in range(n_rows)]
    occupied = set()
    for r in range(n_rows):
        c = 0
        for cell in cells[r]:
            while (r, c) in occupied:
                c += 1
            # 保证该行有足够列
            need = c + cell["colspan"]
            while len(grid[r]) < need:
                grid[r].append("")
            # 填写主单元格与跨列
            for dc in range(cell["colspan"]):
                if dc > 0:
                    grid[r][c + dc] = ""
            grid[r][c] = cell["text"]
            # 跨行：下方各行补列并标记占用
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
    # 多行单元格内容换行符转 <br>，转义竖线
    def fmt(s: str) -> str:
        return (s or "").replace("\n", "<br>").replace("|", "\\|")
    lines = ["| " + " | ".join(fmt(x) for x in grid[0]) + " |"]
    lines.append("|" + "|".join([" --- "] * col_count) + "|")
    for row in grid[1:]:
        lines.append("| " + " | ".join(fmt(x) for x in row) + " |")
    return "\n".join(lines)


def _resolve_img_path(raw: str, parsed_root: Path) -> str:
    """把 MinerU 输出的相对 img_path 解析为绝对路径（存在性校验）。"""
    if not raw:
        return ""
    raw = raw.replace("\\", "/").strip()
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return str(p)
    candidates = [
        parsed_root.parent / raw,               # 相对解析数据根（mineru-parsed/...）
        parsed_root / raw,                      # 直接拼在根下
        parsed_root / "/".join(raw.split("/")[1:]),  # 去掉首个目录段
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return raw


def parse_content_list(json_path: str | Path) -> Optional[DocRecord]:
    """解析单个 *_content_list.json 为一个 DocRecord。"""
    json_path = Path(json_path)
    try:
        with open(json_path, encoding="utf-8") as f:
            items: List[dict] = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    doc_id = json_path.stem.replace("_content_list", "")
    domain = doc_id.rsplit("_", 1)[0]
    doc = DocRecord(doc_id=doc_id, domain=domain)

    # 1. 按页分组，页内按 bbox 阅读顺序（上→下，左→右）排序
    by_page: dict[int, list[dict]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        pidx = int(it.get("page_idx", 0))
        by_page.setdefault(pidx, []).append(it)
    for pidx in by_page:
        by_page[pidx].sort(key=lambda it: (float(it.get("bbox", [0, 0, 0, 0])[1]), float(it.get("bbox", [0, 0, 0, 0])[0])))

    max_page = max(by_page) if by_page else -1
    parsed_root = json_path.parent.parent.parent  # .../<doc>/auto/*.json -> mineru-parsed

    for pidx in range(max_page + 1):
        page_items = by_page.get(pidx, [])
        page = Page(doc_id=doc_id, page_idx=pidx, text="", pkey=f"{doc_id}|{pidx}")
        parts: List[str] = [f"[Page {pidx + 1}]"]
        order = 0
        for it in page_items:
            kind = it.get("type", "text")
            if kind == "text":
                txt = str(it.get("text", "")).strip()
                if not txt:
                    continue
                level = int(it.get("text_level", 0) or 0)
                if not doc.title and level == 1 and pidx == 0:
                    doc.title = txt
                if level == 1:
                    parts.append(f"## {txt}")
                elif level == 2:
                    parts.append(f"### {txt}")
                else:
                    parts.append(txt)
            elif kind == "image":
                caption = " ".join(it.get("image_caption") or []).strip()
                foot = " ".join(it.get("image_footnote") or []).strip()
                img = _resolve_img_path(it.get("img_path", ""), parsed_root)
                elem = Element(
                    kind="image", page_idx=pidx, caption=caption,
                    img_path=img, order=order,
                    elem_id=f"{doc_id}|{pidx}|{order}",
                )
                page.elements.append(elem)
                order += 1
                parts.append(f"[Figure: {caption}]" if caption else "[Figure]")
                if foot:
                    parts.append(f"[Figure footnote: {foot}]")
            elif kind == "table":
                md = html_table_to_markdown(it.get("table_body", "") or "")
                caption = " ".join(it.get("table_caption") or []).strip()
                img = _resolve_img_path(it.get("img_path", ""), parsed_root)
                elem = Element(
                    kind="table", page_idx=pidx, caption=caption,
                    body=it.get("table_body", "") or "", img_path=img, order=order,
                    elem_id=f"{doc_id}|{pidx}|{order}",
                )
                page.elements.append(elem)
                order += 1
                head = f"[Table: {caption}]" if caption else "[Table]"
                parts.append(f"{head}\n{md}" if md else head)
            elif kind == "equation":
                latex = str(it.get("text", "")).strip()
                elem = Element(
                    kind="equation", page_idx=pidx, caption=latex, body=latex,
                    img_path=_resolve_img_path(it.get("img_path", ""), parsed_root),
                    order=order, elem_id=f"{doc_id}|{pidx}|{order}",
                )
                page.elements.append(elem)
                order += 1
                parts.append(f"[Equation: {latex}]")
            else:
                continue
        page.text = "\n".join(parts)
        doc.pages.append(page)
    doc.n_pages = len(doc.pages)
    return doc


def iter_content_lists(root: str | Path) -> List[Path]:
    """递归收集 *_content_list.json（排除 .bak）。"""
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*_content_list.json")):
        if p.name.endswith(".bak"):
            continue
        out.append(p)
    return out
