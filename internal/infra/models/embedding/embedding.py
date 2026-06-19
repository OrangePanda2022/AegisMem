"""
Embedding 客户端模块。
======================

基于 OpenAI Embeddings API 的文本向量服务封装。

功能：
  1. 将文本转换为向量嵌入（embedding），用于语义相似度计算。
  2. 支持批量文本嵌入（逐条调用，串行执行）。

在记忆系统中的作用：
  - 输入阶段：对提取的事实进行嵌入，存入向量数据库。
  - 检索阶段：将用户查询转为嵌入向量，进行语义相似度搜索。
  - MAS 计算阶段：计算查询嵌入与事实嵌入的余弦相似度。

使用方式：
  >>> from internal.infra.models.embedding.embedding import embedding_client
  >>> vec = await embedding_client.embed_text("一段文本")
"""

import logging

import httpx
from openai import AsyncOpenAI

from internal.config.settings import settings
from internal.util.api_retry import get_semaphore, with_retry
from internal.util.token_tracker import tracker as token_tracker

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """
    Embedding 客户端：封装 OpenAI Embeddings API。

    提供文本到向量嵌入的转换能力，是记忆系统语义检索的基础组件。
    """

    def __init__(self) -> None:
        t = settings.embedding_call_timeout_s
        self._client = AsyncOpenAI(
            api_key=settings.embedding_api_key or "EMPTY",
            base_url=settings.embedding_base_url or None,
            timeout=httpx.Timeout(t, connect=5.0),
            max_retries=0,
        )

    async def embed_text(self, text: str) -> list[float]:
        logger.debug("Embedding: text='%s...'", text[:80])
        sem = get_semaphore("embedding", settings.embedding_max_concurrency)

        async def _call():
            async with sem:
                return await self._client.embeddings.create(
                    model=settings.embedding_model,
                    input=text,
                    dimensions=settings.embedding_dim,
                )

        resp = await with_retry(
            _call,
            max_retries=settings.api_max_retries,
            base_delay=settings.api_retry_base_delay,
            label="embedding",
            per_call_timeout=settings.embedding_call_timeout_s,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            token_tracker.add(
                "embedding",
                prompt=getattr(usage, "prompt_tokens", 0) or 0,
                total=getattr(usage, "total_tokens", 0) or 0,
            )
        embedding = resp.data[0].embedding
        logger.debug("Embedding: dim=%d", len(embedding))
        return embedding

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        sem = get_semaphore("embedding", settings.embedding_max_concurrency)

        async def _call():
            async with sem:
                return await self._client.embeddings.create(
                    model=settings.embedding_model,
                    input=texts,
                    dimensions=settings.embedding_dim,
                )

        resp = await with_retry(
            _call,
            max_retries=settings.api_max_retries,
            base_delay=settings.api_retry_base_delay,
            label="embedding_batch",
            per_call_timeout=settings.embedding_call_timeout_s,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            token_tracker.add(
                "embedding",
                prompt=getattr(usage, "prompt_tokens", 0) or 0,
                total=getattr(usage, "total_tokens", 0) or 0,
            )
        # resp.data 已按 input index 排序
        sorted_data = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]


# 全局单例：方便在整个应用中共享同一个 EmbeddingClient 实例
embedding_client = EmbeddingClient()
