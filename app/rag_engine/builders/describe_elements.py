"""构建期图像/表格描述（阶段二·多模态表征）。

用 Qwen3-VL-8B 把每张图/表"翻译"成详细文字（含数值、坐标、趋势、结论），
再把这些描述用 bge-m3 嵌入（竞赛约束的向量方案），从而让"读图结果"可被文本检索命中。

产出 element_descriptions.json，支持断点续跑与并发控制。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .. import api_client
from ..config import settings

IMAGE_DESC_SYSTEM = """You are a document analyst. Describe the figure/chart in the image for RETRIEVAL purposes.
Be maximally faithful to what is visible:
- Transcribe every label, legend entry, axis label/tick, annotation exactly as shown.
- Transcribe qualitative ratings/categories (e.g., Good/Fair/Excellent/Yes/No) VERBATIM — never paraphrase them.
- Give exact numbers with units, sign, and decimals.
- State the figure type, what it shows, trends, and the main conclusion.
Output plain text (no JSON, no markdown fences), 4-8 sentences."""

TABLE_DESC_SYSTEM = """You are a document analyst. Describe the table in the image for RETRIEVAL purposes.
Be maximally faithful:
- Identify the table title and all column headers and row labels exactly.
- Transcribe the table CELL BY CELL: for each row, list its label and every cell value
  (exact numbers with units, sign where brackets mean negative, thousands separators, scale).
- Transcribe qualitative ratings (e.g., Good/Fair/Excellent) VERBATIM.
Output plain text (no JSON, no markdown fences), dense and complete."""


def _desc_prompt(kind: str, caption: str) -> str:
    head = f"Caption: {caption}" if caption else ""
    if kind == "table":
        return f"{head}\n\nDescribe this table completely with all its values."
    return f"{head}\n\nDescribe this figure completely with all its values."


async def describe_one(elem: dict, page: dict, sem: asyncio.Semaphore) -> str:
    kind = elem.get("kind", "image")
    img_path = elem.get("img_path", "")
    if not img_path or not Path(img_path).exists():
        return ""
    system = TABLE_DESC_SYSTEM if kind == "table" else IMAGE_DESC_SYSTEM
    prompt = _desc_prompt(kind, elem.get("caption", ""))
    async with sem:
        for attempt in range(3):
            try:
                msg = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        api_client.image_message(img_path),
                        {"type": "text", "text": prompt},
                    ]},
                ]
                return await api_client.chat_complete(msg, max_tokens=600, temperature=0.0)
            except Exception as e:
                if attempt == 2:
                    return f"[desc failed: {str(e)[:60]}]"
                await asyncio.sleep(2 * (attempt + 1))
    return ""


async def describe_all(
    page_store: dict,
    out_path: str,
    concurrency: int = 4,
    limit: int = 0,
    kinds: tuple = ("image", "table"),
    force: bool = False,
) -> dict:
    """遍历所有元素生成描述，断点续跑；force=True 时重写全部。"""
    out = {}
    if Path(out_path).exists():
        out = json.loads(Path(out_path).read_text(encoding="utf-8"))

    tasks = []
    for pkey, meta in page_store.items():
        for el in meta.get("elements", []):
            if el.get("kind") not in kinds:
                continue
            eid = el.get("elem_id")
            if not eid or (not force and eid in out):
                continue
            tasks.append((el, meta))

    if limit:
        tasks = tasks[:limit]

    print(f"[describe] {len(tasks)} elements to describe (concurrency={concurrency})")
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()
    done = 0

    async def run(el, meta):
        nonlocal done
        desc = await describe_one(el, meta, sem)
        out[el["elem_id"]] = desc
        done += 1
        if done % 10 == 0:
            Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            print(f"[describe] {done}/{len(tasks)} elapsed={time.time()-t0:.0f}s", flush=True)

    # 分批并发
    batch = 8
    for i in range(0, len(tasks), batch):
        chunk = tasks[i : i + batch]
        await asyncio.gather(*[run(el, meta) for el, meta in chunk])
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[describe] done {len(out)} in {time.time()-t0:.0f}s -> {out_path}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=settings.index_dir)
    ap.add_argument("--out", default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="忽略已有描述，全部重写")
    args = ap.parse_args()

    index_dir = Path(args.index)
    page_store = json.loads((index_dir / "page_store.json").read_text(encoding="utf-8"))
    out = args.out or str(index_dir / "element_descriptions.json")
    asyncio.run(describe_all(page_store, out, concurrency=args.concurrency, limit=args.limit, force=args.force))


if __name__ == "__main__":
    main()
