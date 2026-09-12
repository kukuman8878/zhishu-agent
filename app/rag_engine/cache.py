"""VLM 结果缓存：同一 (messages + model + temperature) 不重复调用 API。

key = sha256(序列化 messages + model + temperature)
命中直接复用，未命中写入 cache/vlm/{key}.json。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from .config import PROJECT_ROOT, settings


def cache_root() -> Path:
    p = Path(settings.cache_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def cache_key(messages: list[dict], model: str, temperature: float = 0.0) -> str:
    payload = json.dumps(
        {"messages": messages, "model": model, "temperature": temperature},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_get(key: str) -> Optional[str]:
    if not settings.vlm_cache:
        return None
    f = cache_root() / f"{key}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data.get("response", "")
        except json.JSONDecodeError, OSError:
            return None
    return None


def cache_put(key: str, response: str) -> None:
    if not settings.vlm_cache:
        return
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    f = root / f"{key}.json"
    tmp = root / f"{key}.json.tmp"
    tmp.write_text(
        json.dumps(
            {"key": key, "response": response, "ts": time.time()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(f)
