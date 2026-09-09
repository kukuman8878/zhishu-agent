"""页面图生成：构建期把 PDF 页面渲染为 JPEG，供 VLM 读图。

优先使用数据源提供的页面 PNG（images/{domain}/{id}/{id}_page_XXXX.png），
缺失时用 PyMuPDF 从原始 PDF 渲染。渲染结果缓存在索引目录，运行期零成本。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import pymupdf

from ..config import settings
from ..data_model import DocRecord

_PAGE_PNG_RE = None


def _lookup_provided_page_image(doc_id: str, page_idx: int) -> Optional[str]:
    """按既有页面图目录约定查找现成页面图。"""
    base = settings.images_dir
    if not base:
        return None
    root = Path(base)
    if not root.exists():
        return None
    cands = [
        root / doc_id / f"{doc_id}_page_{page_idx + 1:04d}.png",
        root / doc_id / f"{doc_id}_page_{page_idx + 1}.png",
        root / "images" / doc_id / f"{doc_id}_page_{page_idx + 1:04d}.png",
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def _find_origin_pdf(doc_id: str) -> Optional[Path]:
    """在解析目录与原始 PDF 目录中定位文档 PDF。

    优先级：原始 PDF > MinerU 解析 layout.pdf（兜底，
    仅用于页面图缺失时补充；随包发布的 index/pages/ 已渲染页优先于两者）。
    """
    cands: list[Path] = []
    if settings.mineru_parsed_dir:
        p = Path(settings.mineru_parsed_dir) / doc_id / "auto" / f"{doc_id}_origin.pdf"
        cands.append(p)
    if settings.original_pdf_dir:
        cands.append(Path(settings.original_pdf_dir) / f"{doc_id}_origin.pdf")
        cands.append(Path(settings.original_pdf_dir) / f"{doc_id}.pdf")
    if settings.mineru_parsed_dir:
        cands.append(Path(settings.mineru_parsed_dir) / doc_id / "auto" / f"{doc_id}_layout.pdf")
    for c in cands:
        if c.exists():
            return c
    return None


def render_page_images(doc: DocRecord, out_dir: str | Path, dpi: Optional[int] = None) -> int:
    """渲染文档全部页面图，返回成功数量。out_dir/{doc_id}/page_XXXX.jpg"""
    dpi = dpi or settings.page_image_dpi
    doc_dir = Path(out_dir) / doc.doc_id
    pdf_path = _find_origin_pdf(doc.doc_id)
    ok = 0
    for page in doc.pages:
        dst = doc_dir / f"page_{page.page_idx + 1:04d}.jpg"
        provided = _lookup_provided_page_image(doc.doc_id, page.page_idx)
        if provided:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(provided, dst)
            page.page_image = str(dst)
            ok += 1
            continue
        if pdf_path is None:
            page.page_image = ""
            continue
        if dst.exists():
            page.page_image = str(dst)
            ok += 1
            continue
        try:
            with pymupdf.open(str(pdf_path)) as pdf:
                if page.page_idx >= len(pdf):
                    page.page_image = ""
                    continue
                pix = pdf[page.page_idx].get_pixmap(dpi=dpi)
                dst.parent.mkdir(parents=True, exist_ok=True)
                pix.save(str(dst), jpg_quality=82)
                page.page_image = str(dst)
                ok += 1
        except Exception:
            page.page_image = ""
    return ok
