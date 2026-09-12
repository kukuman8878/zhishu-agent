"""页面图定位：运行期在解析目录与原始 PDF 目录中定位文档 PDF。

页面渲染已在构建期完成并随索引发布，运行期只需按需取 PDF 路径（如视觉策略补渲染）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import settings


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
        cands.append(
            Path(settings.mineru_parsed_dir) / doc_id / "auto" / f"{doc_id}_layout.pdf"
        )
    for c in cands:
        if c.exists():
            return c
    return None
