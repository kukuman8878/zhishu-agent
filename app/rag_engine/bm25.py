"""轻量 BM25 索引（自行实现，内存版，支持多语种 token 化与序列化）。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> List[str]:
    """英文按词、中文按单字切分，统一小写。"""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@dataclass
class BM25Index:
    k1: float = 1.5
    b: float = 0.75
    _docs: Dict[str, List[str]] = field(default_factory=dict)  # id -> tokens
    _df: Dict[str, int] = field(default_factory=dict)  # term -> doc freq
    _avgdl: float = 0.0
    _n: int = 0

    def add(self, doc_id: str, text: str) -> None:
        tokens = tokenize(text)
        self._docs[doc_id] = tokens
        seen: set[str] = set()
        for t in tokens:
            if t not in seen:
                seen.add(t)
                self._df[t] = self._df.get(t, 0) + 1
        self._n = len(self._docs)
        total = sum(len(v) for v in self._docs.values())
        self._avgdl = total / max(self._n, 1)

    def search(self, query: str, top_k: int) -> List[Tuple[str, float]]:
        q_tokens = [t for t in tokenize(query) if t in self._df]
        if not q_tokens:
            return []
        scores: Dict[str, float] = {}
        for t in set(q_tokens):
            idf = math.log(1 + (self._n - self._df[t] + 0.5) / (self._df[t] + 0.5))
            for doc_id, tokens in self._docs.items():
                tf = tokens.count(t)
                if tf == 0:
                    continue
                dl = len(tokens)
                score = (
                    idf
                    * (tf * (self.k1 + 1))
                    / (tf + self.k1 * (1 - self.b + self.b * dl / max(self._avgdl, 1)))
                )
                scores[doc_id] = scores.get(doc_id, 0.0) + score
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {"k1": self.k1, "b": self.b, "docs": self._docs, "df": self._df},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "BM25Index":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        idx = cls(k1=data["k1"], b=data["b"])
        idx._docs = data["docs"]
        idx._df = data["df"]
        idx._n = len(idx._docs)
        total = sum(len(v) for v in idx._docs.values())
        idx._avgdl = total / max(idx._n, 1)
        return idx
