"""文本向量化。

默认的 HashEmbedding 不依赖任何模型文件和网络：把中文切成单字 + 双字 n-gram、
英文切成单词，再用哈希映射到固定维度向量。对“表字段描述”这类短文本、
关键词重合度高的检索已经够用，也让项目克隆下来就能跑测试。

需要更好的语义效果时，切换到 OpenAIEmbedding（任意 OpenAI 兼容接口，
例如 SiliconFlow 上的 BAAI/bge-m3），代码其余部分不用改。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from datapulse.config import Settings

_STOP_CHARS = set("的了和与及是在按各每个哪些多少什么吗呢把被给请帮我们你他它这那有")
_WORD_RE = re.compile(r"[a-z][a-z0-9]*|\d+")
_CJK_RE = re.compile(r"[一-鿿]+")


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[tuple[str, float]]:
    """返回 (token, 权重)。双字词比单字更有区分度，权重更高。"""
    text = text.lower()
    tokens: list[tuple[str, float]] = []
    for word in _WORD_RE.findall(text.replace("_", " ")):
        tokens.append((word, 1.5))
    for seg in _CJK_RE.findall(text):
        tokens.extend((ch, 1.0) for ch in seg if ch not in _STOP_CHARS)
        tokens.extend((seg[i : i + 2], 2.0) for i in range(len(seg) - 1))
    return tokens


class HashEmbedding:
    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token, weight in tokenize(text):
            h = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "little")
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[h % self.dim] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class OpenAIEmbedding:
    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url or None, api_key=api_key)
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            resp = self.client.embeddings.create(model=self.model, input=texts[i : i + 32])
            out.extend(d.embedding for d in resp.data)
        return out


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        return OpenAIEmbedding(settings.embedding_base_url, settings.embedding_api_key, settings.embedding_model)
    return HashEmbedding()
