"""全局配置：加载 .env，暴露模型端点与超参。

所有模型交互统一走 OpenAI 兼容协议，默认指向硅基流动 API，
如需本地部署可把 .env 中的端点换成 vLLM 本地服务。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 代码已融入智数项目（zhishu-agent/app/rag_engine/），
# config.py 的父级依次是 rag_engine/ → app/ → zhishu-agent/（项目根），故取 parents[2]。
# .env 与 402M 索引目录都从智数根 .env 读取，保证单进程内配置自洽。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except ValueError:
        return default


def _rerank_host() -> str:
    """rerank 端点：优先 RERANK_ENDPOINT（完整 URL），回退 RERANK_BINDING_HOST。"""
    ep = os.getenv("RERANK_ENDPOINT", "").strip()
    if ep:
        return ep.rstrip("/").rsplit("/rerank", 1)[0]
    return os.getenv("RERANK_BINDING_HOST", "").strip()


@dataclass
class Settings:
    # ---- 模型端点（OpenAI 兼容） ----
    api_key: str = field(default_factory=lambda: _env("SILICONFLOW_API_KEY"))
    embedding_host: str = field(default_factory=lambda: _env("EMBEDDING_BINDING_HOST"))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL"))
    embedding_dim: int = field(default_factory=lambda: _env_int("EMBEDDING_DIM", 1024))
    # rerank 端点：优先 RERANK_ENDPOINT（完整 URL），回退 RERANK_BINDING_HOST：
    # 两者都支持，RERANK_ENDPOINT 优先
    rerank_host: str = field(default_factory=lambda: _rerank_host())
    rerank_model: str = field(default_factory=lambda: _env("RERANK_MODEL"))
    llm_host: str = field(default_factory=lambda: _env("LLM_BINDING_HOST"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    # 视觉复合题（image_plus_text）专用模型：趋势/多条件推理更强时单独指定
    llm_model_visual: str = field(
        default_factory=lambda: _env("LLM_MODEL_VISUAL", "") or _env("LLM_MODEL")
    )

    # ---- 数据路径 ----
    mineru_parsed_dir: str = field(
        default_factory=lambda: _env("MINERU_PARSED_DIR") or _env("DOC_FILE_PATH")
    )
    original_pdf_dir: str = field(default_factory=lambda: _env("ORIGINAL_PDF_DIR"))
    index_dir: str = field(default_factory=lambda: _env("INDEX_DIR", "index"))
    images_dir: str = field(default_factory=lambda: _env("IMAGES_DIR"))

    # ---- 检索超参 ----
    doc_topk: int = field(default_factory=lambda: _env_int("DOC_TOPK", 2))
    recall_topk: int = field(default_factory=lambda: _env_int("PAGE_RECALL_TOPK", 24))
    page_topk_default: int = field(
        default_factory=lambda: _env_int("PAGE_TOPK_DEFAULT", 5)
    )
    page_topk_visual: int = field(
        default_factory=lambda: _env_int("PAGE_TOPK_VISUAL", 7)
    )
    page_topk_text: int = field(default_factory=lambda: _env_int("PAGE_TOPK_TEXT", 6))
    rerank_weight: float = field(
        default_factory=lambda: _env_float("RERANK_WEIGHT", 0.7)
    )
    # ---- 答案强约束（拒绝低置信 + 强制可溯源）----
    # 重排分数门：检索到的页面里最高 bge-reranker 相关分低于该阈值时，判定语料未覆盖，
    # 直接拒绝作答（返回 NOT_FOUND），避免用低相关页面编造答案。
    rerank_min_score: float = field(
        default_factory=lambda: _env_float("RERANK_MIN_SCORE", 0.30)
    )
    # 可溯源门：答案必须能落到具体页码（cited_pages 非空），否则拒绝作答
    require_citation: bool = field(
        default_factory=lambda: _env("REQUIRE_CITATION", "1") == "1"
    )

    # ---- Agent 超参 ----
    max_agent_rounds: int = field(
        default_factory=lambda: _env_int("MAX_AGENT_ROUNDS", 2)
    )
    answer_max_tokens: int = field(
        default_factory=lambda: _env_int("ANSWER_MAX_TOKENS", 1500)
    )
    img_max_count: int = field(default_factory=lambda: _env_int("IMG_MAX_COUNT", 6))
    no_image_mode: bool = field(
        default_factory=lambda: _env("NO_IMAGE_MODE", "0") == "1"
    )
    ctx_page_chars: int = field(
        default_factory=lambda: _env_int("CTX_PAGE_CHARS", 2500)
    )
    ctx_total_chars: int = field(
        default_factory=lambda: _env_int("CTX_TOTAL_CHARS", 8000)
    )
    request_timeout: float = field(
        default_factory=lambda: _env_float("REQUEST_TIMEOUT", 300)
    )
    embedding_batch: int = field(
        default_factory=lambda: _env_int("EMBEDDING_BATCH", 32)
    )
    page_image_dpi: int = field(default_factory=lambda: _env_int("PAGE_IMAGE_DPI", 150))

    # ---- 视觉策略 / 缓存 ----
    image_strategy: str = field(
        default_factory=lambda: _env("IMAGE_STRATEGY", "baseline")
    )
    vlm_cache: bool = field(default_factory=lambda: _env("VLM_CACHE", "1") == "1")
    cache_dir: str = field(default_factory=lambda: _env("CACHE_DIR", "cache/vlm"))
    region_dpi: int = field(default_factory=lambda: _env_int("REGION_DPI", 300))
    crop_upscale: int = field(default_factory=lambda: _env_int("CROP_UPSCALE", 2))
    verify_max_rounds: int = field(
        default_factory=lambda: _env_int("VERIFY_MAX_ROUNDS", 2)
    )
    full_image_max_dim: int = field(
        default_factory=lambda: _env_int("FULL_IMAGE_MAX_DIM", 2048)
    )


settings = Settings()
