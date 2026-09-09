"""索引数据模型：Doc / Page / Element。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Element:
    """页面内的非文本元素（图/表/公式），用于元素级索引与视觉证据提供。"""

    kind: str  # "image" | "table" | "equation"
    page_idx: int
    caption: str = ""  # 图注/表注/公式原文（检索文本）
    body: str = ""  # table: HTML; equation: LaTeX; image: ""
    img_path: str = ""  # 元素裁剪图绝对路径（image/table 有）
    order: int = 0  # 页内阅读顺序
    elem_id: str = ""  # "doc|page|order"


@dataclass
class Page:
    """单页聚合记录：整页文本 + 元素列表 + 页面图路径。"""

    doc_id: str
    page_idx: int  # 0 基
    text: str  # 页文本（含标题/表格/图注内联）
    elements: List[Element] = field(default_factory=list)
    page_image: str = ""  # 页面渲染图绝对路径（构建期生成）
    pkey: str = ""  # "doc|page"


@dataclass
class DocRecord:
    """文档记录。"""

    doc_id: str  # 如 finance_4880250
    domain: str  # 由 doc_id 前缀解析
    title: str = ""
    pages: List[Page] = field(default_factory=list)
    n_pages: int = 0

    def summary_text(self) -> str:
        """文档摘要文本（用于文档级向量），取标题+首页文本头部。"""
        head = ""
        if self.pages:
            head = self.pages[0].text[:3000]
        return f"{self.domain} {self.title}\n{head}".strip()
