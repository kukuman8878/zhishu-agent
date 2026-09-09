"""OpenAI 兼容协议客户端：embedding / rerank / chat，带重试与并发节流。

全部用 httpx 直连实现，不依赖第三方 SDK，方便在 API 与 vLLM 端点间切换。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Optional

import httpx

from .config import settings


class ApiError(RuntimeError):
    pass


async def _post_json(
    client: httpx.AsyncClient, url: str, payload: dict, timeout: float
) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    last_err: Optional[Exception] = None
    for attempt in range(5):
        try:
            resp = await client.post(
                url, json=payload, headers=headers, timeout=timeout
            )
            if resp.status_code == 429:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            last_err = e
            await asyncio.sleep(1.5 * (attempt + 1))
    raise ApiError(f"request failed after retries: {last_err}")


async def embed_texts(
    texts: list[str],
    client: Optional[httpx.AsyncClient] = None,
    batch: Optional[int] = None,
) -> list[list[float]]:
    """批量文本向量化，返回与输入同序的向量列表。"""
    batch = batch or settings.embedding_batch
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(trust_env=False)
    url = settings.embedding_host.rstrip("/") + "/embeddings"
    out: list[list[float]] = []
    try:
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            data = await _post_json(
                client,
                url,
                {"model": settings.embedding_model, "input": chunk},
                settings.request_timeout,
            )
            rows = sorted(data["data"], key=lambda r: r.get("index", 0))
            out.extend(r["embedding"] for r in rows)
    finally:
        if owns_client:
            await client.aclose()
    return out


async def rerank_documents(
    query: str,
    documents: list[str],
    top_n: int,
    client: Optional[httpx.AsyncClient] = None,
) -> list[tuple[int, float]]:
    """重排文档，返回 [(原文档下标, 相关分), ...]，按分数降序，最多 top_n 个。"""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(trust_env=False)
    url = settings.rerank_host.rstrip("/") + "/rerank"
    try:
        data = await _post_json(
            client,
            url,
            {
                "model": settings.rerank_model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
            },
            settings.request_timeout,
        )
        results = data.get("results", [])
        return [(r["index"], float(r["relevance_score"])) for r in results]
    finally:
        if owns_client:
            await client.aclose()


async def chat_complete(
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.0,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """OpenAI 兼容 chat 请求（支持 content 为图文混合列表）。"""
    raw, _ = await _chat_complete_with_usage(messages, max_tokens, temperature, client)
    return raw


async def _chat_complete_with_usage(
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.0,
    client: Optional[httpx.AsyncClient] = None,
    model: Optional[str] = None,
) -> tuple[str, tuple[int, int]]:
    """返回 (文本, (prompt_tokens, completion_tokens))。"""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(trust_env=False)
    url = settings.llm_host.rstrip("/") + "/chat/completions"
    try:
        data = await _post_json(
            client,
            url,
            {
                "model": model or settings.llm_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            settings.request_timeout,
        )
        choices = data.get("choices") or []
        if not choices:
            raise ApiError(f"chat returned empty choices: {data.get('error', '')}")
        usage = data.get("usage") or {}
        return (
            choices[0]["message"]["content"] or "",
            (
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
            ),
        )
    finally:
        if owns_client:
            await client.aclose()


class VLMClient:
    """统一 VLM 调用入口（docx 十一）：infer / infer_batch / verify。

    - 同一 (messages + model + temperature) 命中 cache/vlm 直接复用
    - 统计 API 调用次数与 token 用量，供 ablation 报告
    - 换 provider/model 只需改 .env 或传 model 参数，代码零改动
    """

    def __init__(self, use_cache: Optional[bool] = None, model: Optional[str] = None):
        self.use_cache = settings.vlm_cache if use_cache is None else use_cache
        self.model = model or settings.llm_model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def infer(
        self,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        from . import cache as vlm_cache

        key = vlm_cache.cache_key(messages, self.model, temperature)
        if self.use_cache:
            hit = vlm_cache.cache_get(key)
            if hit is not None:
                return hit
        self.calls += 1
        raw, usage = await _chat_complete_with_usage(
            messages, max_tokens, temperature, model=self.model
        )
        self.prompt_tokens += usage[0]
        self.completion_tokens += usage[1]
        if self.use_cache:
            vlm_cache.cache_put(key, raw)
        return raw

    async def infer_batch(
        self,
        reqs: list[dict],
        concurrency: int = 4,
    ) -> list[str]:
        sem = asyncio.Semaphore(concurrency)

        async def one(r: dict) -> str:
            async with sem:
                return await self.infer(
                    r["messages"],
                    max_tokens=r.get("max_tokens", 1024),
                    temperature=r.get("temperature", 0.0),
                )

        return await asyncio.gather(*[one(r) for r in reqs])

    async def verify(self, messages: list[dict], max_tokens: int = 512) -> str:
        return await self.infer(messages, max_tokens=max_tokens)

    def stats(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def image_message(image_path: str, label: str = "") -> dict:
    """把本地图片编码为 OpenAI 兼容 image_url 消息段。"""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
    }


def extract_json(text: str) -> dict | list | None:
    """从模型输出中提取 JSON（容忍 ``` 围栏与前后杂文本）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.lstrip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip().rstrip("`").strip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        start_b, end_b = t.find("["), t.rfind("]")
        if start_b == -1 or end_b <= start_b:
            return None
        try:
            return json.loads(t[start_b : end_b + 1])
        except json.JSONDecodeError:
            return None
    try:
        return json.loads(t[start : end + 1])
    except json.JSONDecodeError:
        return None


def now_ms() -> float:
    return time.perf_counter() * 1000
