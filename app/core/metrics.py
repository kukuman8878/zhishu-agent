"""
节点耗时 Metrics

提供 SQL 分析链路各节点的耗时采集与汇总能力：
以装饰器方式包裹节点函数，记录每次调用的墙钟耗时，汇总到进程内计数器，
一次请求执行结束时由 QueryService 打印一份分节点耗时摘要，便于定位链路瓶颈。
请求间通过 reset 清空，避免跨请求数据串扰。
"""

import functools
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Awaitable, Callable

from app.core.log import logger


class NodeMetrics:
    """进程内节点耗时统计（调用次数 / 总耗时 / 单次最大耗时）"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "total_ms": 0.0, "max_ms": 0.0}
        )

    def record(self, node: str, elapsed_ms: float) -> None:
        """记录一次节点执行耗时；参数 node=节点名，elapsed_ms=本次墙钟毫秒"""
        with self._lock:
            item = self._data[node]
            item["calls"] += 1
            item["total_ms"] += elapsed_ms
            if elapsed_ms > item["max_ms"]:
                item["max_ms"] = elapsed_ms

    def reset(self) -> None:
        """清空统计，供新一轮请求复用"""
        with self._lock:
            self._data.clear()

    def summary(self) -> dict[str, dict]:
        """返回当前统计快照（节点名 → 调用次数/总耗时/最大耗时）"""
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# 全局单例：全进程共享一份节点耗时计数
node_metrics = NodeMetrics()


def timed_node(node_name: str) -> Callable:
    """装饰器：包裹 LangGraph 节点函数，测量单次执行墙钟耗时并写入统计。

    只用来装饰签名固定的图节点（state, runtime），并保留原函数签名与 __wrapped__，
    保证 LangGraph 对节点参数的推断行为不变。
    参数 node_name=用于统计与日志的节点名，返回包装后的节点函数。
    """

    def _decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def _wrapped(state: Any, runtime: Any) -> Any:
            start = time.monotonic()
            try:
                return await fn(state, runtime)
            finally:
                node_metrics.record(node_name, (time.monotonic() - start) * 1000)

        return _wrapped

    return _decorator


def log_metrics_summary() -> None:
    """打印一次请求的节点耗时摘要，并清空统计供下一请求使用"""
    summary = node_metrics.summary()
    if not summary:
        return
    parts = [
        f"{node}={item['calls']}次 均{(item['total_ms'] / item['calls']):.0f}ms 峰{item['max_ms']:.0f}ms"
        for node, item in summary.items()
    ]
    logger.info(f"节点耗时摘要 | {' | '.join(parts)}")
    node_metrics.reset()
