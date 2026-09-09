"""索引装载与多路召回 + RRF 融合。

召回路径（各自独立打分，再按 rank 融合）：
  page_dense   : 整页文本向量（bge-m3）
  page_bm25    : 整页文本 BM25
  element_dense: 图/表/公式 caption 向量 -> 回指父页
  element_bm25 : 元素文本 BM25 -> 回指父页
  doc          : 文档摘要命中 -> 该文档全部页面（低权重兜底）
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from nano_vectordb import NanoVectorDB

from ..bm25 import BM25Index
from ..config import settings

RRF_K = 60.0
DOC_PATH_BOOST_RANK = 200  # doc 路径参与 RRF 时的固定高位 rank


class IndexStore:
    """索引文件装载器（进程内单例使用）。"""

    def __init__(self, index_dir: str | Path):
        self.dir = Path(index_dir)
        self.page_store: dict = json.loads(
            (self.dir / "page_store.json").read_text(encoding="utf-8")
        )
        self.doc_store: dict = json.loads(
            (self.dir / "doc_store.json").read_text(encoding="utf-8")
        )
        self.page_vdb = NanoVectorDB(
            settings.embedding_dim, metric="cosine", storage_file=str(self.dir / "page_vectors.json")
        )
        self.element_vdb = NanoVectorDB(
            settings.embedding_dim, metric="cosine", storage_file=str(self.dir / "element_vectors.json")
        )
        self.doc_vdb = NanoVectorDB(
            settings.embedding_dim, metric="cosine", storage_file=str(self.dir / "doc_vectors.json")
        )
        self.bm25_pages = BM25Index.load(self.dir / "bm25_pages.json")
        self.bm25_elements = BM25Index.load(self.dir / "bm25_elements.json")
        self.bm25_docs = BM25Index.load(self.dir / "bm25_docs.json")

    def page_of(self, pkey: str) -> Optional[dict]:
        if pkey.endswith("|tail"):
            pkey = pkey.rsplit("|", 1)[0]
        return self.page_store.get(pkey)

    def element_page(self, elem_id: str) -> Optional[str]:
        doc_id, pidx, _ = elem_id.split("|")
        return f"{doc_id}|{pidx}"


def _elem_to_page(elem_hits: List[Tuple[str, float]], store: IndexStore) -> List[Tuple[str, float]]:
    out: Dict[str, float] = {}
    for elem_id, score in elem_hits:
        pkey = store.element_page(elem_id)
        if pkey:
            out[pkey] = max(out.get(pkey, 0.0), score)
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


async def retrieve_top_elements(
    query_vec: List[float],
    store: IndexStore,
    top_k: int,
) -> List[tuple]:
    """直接检索最相关的图/表元素，返回 [(元素dict, 父页dict, 相关分)]。"""
    hits = store.element_vdb.query(np.array(query_vec, dtype=np.float32), top_k=top_k)
    out = []
    for h in hits:
        elem_id = h["__id__"]
        pkey = store.element_page(elem_id)
        page = store.page_of(pkey)
        if not page:
            continue
        for el in page["elements"]:
            if el.get("elem_id") == elem_id and el.get("img_path"):
                out.append((el, page, float(h["__metrics__"])))
                break
    return out


async def multi_path_recall(
    query: str,
    query_vec: List[float],
    store: IndexStore,
    page_topk: int = 30,
    element_topk: int = 30,
) -> Dict[str, List[Tuple[str, float]]]:
    """执行全部召回路径，返回 {path: [(id, score), ...]}。"""
    paths: Dict[str, List[Tuple[str, float]]] = {}

    # 页面稠密
    hits = store.page_vdb.query(np.array(query_vec, dtype=np.float32), top_k=page_topk)
    paths["page_dense"] = [(h["__id__"], float(h["__metrics__"])) for h in hits]

    # 页面 BM25
    paths["page_bm25"] = store.bm25_pages.search(query, page_topk)

    # 元素稠密 + BM25 -> 父页
    e_hits = store.element_vdb.query(np.array(query_vec, dtype=np.float32), top_k=element_topk)
    paths["element_dense"] = _elem_to_page(
        [(h["__id__"], float(h["__metrics__"])) for h in e_hits], store
    )
    paths["element_bm25"] = _elem_to_page(store.bm25_elements.search(query, element_topk), store)

    # 文档级（摘要向量 + BM25）-> 文档
    d_hits = store.doc_vdb.query(np.array(query_vec, dtype=np.float32), top_k=settings.doc_topk)
    doc_cands = [(h["__id__"], float(h["__metrics__"])) for h in d_hits]
    for doc_id, _ in store.bm25_docs.search(query, settings.doc_topk):
        if doc_id not in {d for d, _ in doc_cands}:
            doc_cands.append((doc_id, 0.0))
    paths["docs"] = doc_cands
    return paths


def rrf_fuse(paths: Dict[str, List[Tuple[str, float]]]) -> List[Tuple[str, float]]:
    """RRF 融合各路径结果，返回 [(pkey, rrf_score)] 降序。"""
    scores: Dict[str, float] = defaultdict(float)
    for path, ranked in paths.items():
        if path == "docs":
            continue
        for rank, (pid, _) in enumerate(ranked):
            scores[pid] += 1.0 / (RRF_K + rank + 1)
    # 文档级兜底：对 top 文档的所有页面给予低 rank 参与融合
    for rank, (doc_id, _) in enumerate(paths.get("docs", [])):
        pages = [p for p in paths["page_dense"] if p[0].startswith(doc_id + "|")]
        for pkey, _ in pages:
            scores[pkey] += 1.0 / (RRF_K + DOC_PATH_BOOST_RANK + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def scope_to_docs(
    fused: List[Tuple[str, float]], doc_ids: List[str]
) -> List[Tuple[str, float]]:
    """按候选文档过滤/降权（文档路由失败时提供全局降级入口）。"""
    keep = [kv for kv in fused if kv[0].split("|")[0] in doc_ids]
    return keep or fused
