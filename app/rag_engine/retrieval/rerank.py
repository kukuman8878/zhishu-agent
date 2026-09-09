"""重排与页面证据组装。

两阶段评分（借鉴 ERC2 冠军的加权融合思路，用赛方指定重排模型实现）：
  final = rerank_weight * bge_rerank_score + (1-rerank_weight) * 归一化稠密分
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import api_client
from ..config import settings
from .recall import IndexStore, multi_path_recall, rrf_fuse, scope_to_docs

RERANK_TEXT_CHARS = 4000


@dataclass
class PageEvidence:
    pkey: str
    doc_id: str
    page_idx: int
    text: str
    page_image: str
    elements: list = field(default_factory=list)
    final_score: float = 0.0
    sources: dict = field(default_factory=dict)  # path -> raw score


async def rerank_and_assemble(
    query: str,
    query_vec: List[float],
    store: IndexStore,
    candidate_pages: List[Tuple[str, float]],
    top_k: int,
    element_scores: Optional[Dict[str, float]] = None,
    element_weight: float = 0.0,
    rerank_on_elements: bool = False,
) -> List[PageEvidence]:
    """对候选页做 bge-reranker 重排，加权融合(重排/稠密/元素)后取 top_k 组装证据。

    rerank_on_elements=True 时按页面内图/表注重排（图片题答案在图里，正文不可靠）。
    """
    texts: List[str] = []
    metas: List[dict] = []
    for pkey, fused_score in candidate_pages[: settings.recall_topk]:
        meta = store.page_of(pkey)
        if not meta:
            continue
        if rerank_on_elements:
            caps = " ".join(el.get("caption", "") for el in meta["elements"] if el.get("caption"))
            texts.append(caps[:RERANK_TEXT_CHARS] or meta["text"][:RERANK_TEXT_CHARS])
        else:
            texts.append(meta["text"][:RERANK_TEXT_CHARS])
        metas.append(meta)

    if not texts:
        return []

    reranked = await api_client.rerank_documents(query, texts, top_n=len(texts))
    r_scores: Dict[int, float] = {idx: score for idx, score in reranked}
    dense: Dict[str, float] = {}
    for pkey, score in candidate_pages:
        dense[pkey] = max(dense.get(pkey, 0.0), score)
    dmax = max(dense.values()) if dense else 1.0
    dmax = dmax or 1.0

    elem: Dict[str, float] = {}
    if element_scores:
        for pkey, score in element_scores.items():
            elem[pkey] = max(elem.get(pkey, 0.0), score)
    emax = max(elem.values()) if elem else 1.0
    emax = emax or 1.0

    scored: List[PageEvidence] = []
    for i, meta in enumerate(metas):
        pkey = f"{meta['doc_id']}|{meta['page_idx']}"
        r = r_scores.get(i, 0.0)
        d = dense.get(pkey, 0.0) / dmax
        e = elem.get(pkey, 0.0) / emax
        wr = settings.rerank_weight
        wd = (1 - settings.rerank_weight) * (1 - element_weight)
        we = (1 - settings.rerank_weight) * element_weight
        final = wr * r + wd * d + we * e
        scored.append(
            PageEvidence(
                pkey=pkey,
                doc_id=meta["doc_id"],
                page_idx=meta["page_idx"],
                text=meta["text"],
                page_image=meta["page_image"],
                elements=meta["elements"],
                final_score=final,
                sources={"rerank": r, "dense_norm": d, "element_norm": e},
            )
        )
    scored.sort(key=lambda e: e.final_score, reverse=True)
    return scored[:top_k]


async def retrieve(
    query: str,
    store: IndexStore,
    query_vec: List[float],
    top_k: int,
    doc_scope: Optional[List[str]] = None,
    element_weight: float = 0.0,
    rerank_on_elements: bool = False,
) -> List[PageEvidence]:
    """完整检索：多路召回 -> RRF -> (文档限定) -> 重排(含元素加权) -> 证据。"""
    paths = await multi_path_recall(query, query_vec, store)
    fused = rrf_fuse(paths)
    if doc_scope:
        fused = scope_to_docs(fused, doc_scope)
    elem_scores = dict(paths.get("element_dense", []))
    return await rerank_and_assemble(
        query, query_vec, store, fused, top_k,
        element_scores=elem_scores, element_weight=element_weight,
        rerank_on_elements=rerank_on_elements,
    )
