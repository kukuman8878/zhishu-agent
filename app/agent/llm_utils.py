"""
LLM 调用工具层

提供统一的重试、超时和 token 用量记录能力，避免每个节点各自裸调 LLM。
由于节点普遍使用 LCEL 管道（prompt | llm | parser），这里提供对任意 async
callable 的通用重试封装 retry_async，用法：

    from app.agent.llm_utils import retry_async
    result = await retry_async(chain.ainvoke, {"query": query}, node_name="recall_column")
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, TypeVar

from app.core.context import node_id_ctx_var
from app.core.log import logger

T = TypeVar("T")

# 默认配置
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_TIMEOUT_S = 60.0


def retry_async(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    node_name: str = "llm",
    **kwargs: Any,
) -> Awaitable[T]:
    """把任意 async 调用包装成带重试+超时+耗时日志的版本，返回与原调用相同的 Awaitable"""

    async def _wrapped() -> T:
        last_error: Exception | None = None
        # 把节点名写入上下文，让本节点范围内产生的日志都带结构化 node 标记；并行分支互不干扰
        token = node_id_ctx_var.set(node_name)
        try:
            for attempt in range(1, max_retries + 2):
                start = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        func(*args, **kwargs), timeout=timeout_s
                    )
                    elapsed_ms = (time.monotonic() - start) * 1000
                    # 尝试从结果里取 token 用量（不同对象字段不同，尽力而为）
                    usage = getattr(result, "usage_metadata", None)
                    if usage:
                        prompt_tokens = getattr(usage, "input_tokens", 0) or 0
                        completion_tokens = getattr(usage, "output_tokens", 0) or 0
                        logger.info(
                            f"[{node_name}] LLM 调用成功 attempt={attempt} "
                            f"elapsed={elapsed_ms:.0f}ms "
                            f"tokens=prompt:{prompt_tokens}+completion:{completion_tokens}"
                        )
                    else:
                        response_meta = getattr(result, "response_metadata", {}) or {}
                        usage = response_meta.get("usage")
                        if isinstance(usage, dict):
                            logger.info(
                                f"[{node_name}] LLM 调用成功 attempt={attempt} "
                                f"elapsed={elapsed_ms:.0f}ms "
                                f"tokens=prompt:{usage.get('prompt_tokens')}+"
                                f"completion:{usage.get('completion_tokens')}"
                            )
                        else:
                            logger.info(
                                f"[{node_name}] LLM 调用成功 attempt={attempt} "
                                f"elapsed={elapsed_ms:.0f}ms"
                            )
                    return result
                except asyncio.TimeoutError:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    logger.warning(
                        f"[{node_name}] LLM 调用超时 attempt={attempt}/{max_retries + 1} "
                        f"elapsed={elapsed_ms:.0f}ms timeout={timeout_s}s"
                    )
                    last_error = TimeoutError(f"LLM 调用超时 ({timeout_s}s)")
                    if attempt <= max_retries:
                        await asyncio.sleep(min(2 ** (attempt - 1), 10))
                except Exception as e:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    logger.warning(
                        f"[{node_name}] LLM 调用失败 attempt={attempt}/{max_retries + 1} "
                        f"elapsed={elapsed_ms:.0f}ms error={e}"
                    )
                    last_error = e
                    if attempt <= max_retries:
                        await asyncio.sleep(min(2 ** (attempt - 1), 10))
        finally:
            node_id_ctx_var.reset(token)
        raise last_error  # type: ignore[misc]

    return _wrapped()
