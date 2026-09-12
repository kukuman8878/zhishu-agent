"""
重排分批调用单元测试

TEI /rerank 服务端有单次 batch 上限（默认 8），主链路一次召回 30+ 候选会 413。
rerank_candidates 必须按 max_batch 分批调用并合并全局分数，且保证按分数排序取 top_k。
纯逻辑 + 假重排客户端，不依赖真实服务。
"""

from types import SimpleNamespace

import pytest

from app.agent.rerank import rerank_candidates
from app.conf.app_config import app_config


class FakeRerankClient:
    """假重排客户端：记录每批文档数，按 (分数=文档下标) 返回，便于校验合并排序"""

    def __init__(self):
        self.batch_sizes: list[int] = []

    async def rerank(self, query, texts, top_n=None):
        self.batch_sizes.append(len(texts))

        # 用文本内容当分数（等价真实模型分，跨批可比），文本非数字时记 0
        def _score(text: str) -> float:
            try:
                return float(text)
            except ValueError:
                return 0.0

        return [(i, _score(texts[i])) for i in range(len(texts))]


def _runtime(client):
    return SimpleNamespace(context={"rerank_client": client})


class TestRerankBatching:
    async def test_按max_batch分批调用(self, monkeypatch):
        monkeypatch.setattr(app_config.rerank, "max_batch", 4)
        client = FakeRerankClient()
        candidates = list(range(10))  # 10 个候选 → 分批 4/4/2
        out = await rerank_candidates(
            "q", candidates, render=str, runtime=_runtime(client), top_k=3
        )
        assert client.batch_sizes == [4, 4, 2]
        assert len(out) == 3

    async def test_单批不超上限(self, monkeypatch):
        monkeypatch.setattr(app_config.rerank, "max_batch", 8)
        client = FakeRerankClient()
        await rerank_candidates(
            "q", list(range(30)), render=str, runtime=_runtime(client), top_k=10
        )
        assert all(size <= 8 for size in client.batch_sizes)

    async def test_合并全局分数并降序取topk(self, monkeypatch):
        monkeypatch.setattr(app_config.rerank, "max_batch", 4)
        client = FakeRerankClient()
        # 假客户端给"分数=全局下标"，降序后应取最大的 3 个：9,8,7
        out = await rerank_candidates(
            "q", list(range(10)), render=str, runtime=_runtime(client), top_k=3
        )
        assert out == [9, 8, 7]

    async def test_候选不足top_k直接返回(self, monkeypatch):
        monkeypatch.setattr(app_config.rerank, "max_batch", 4)
        client = FakeRerankClient()
        out = await rerank_candidates(
            "q", [1, 2], render=str, runtime=_runtime(client), top_k=10
        )
        assert out == [1, 2]
        assert client.batch_sizes == []  # 未触发重排

    async def test_重排异常failopen原序截取(self, monkeypatch):
        monkeypatch.setattr(app_config.rerank, "max_batch", 4)

        class Boom:
            async def rerank(self, query, texts, top_n=None):
                raise RuntimeError("service down")

        out = await rerank_candidates(
            "q", list(range(10)), render=str, runtime=_runtime(Boom()), top_k=3
        )
        assert out == [0, 1, 2]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
