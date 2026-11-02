"""大模型调用封装：任意 OpenAI 兼容接口（DeepSeek / Qwen / GPT ...）。

Agent 只依赖 ChatModel 这个最小接口，测试时可以换成脚本化的 FakeChatModel，
不需要联网、不需要 API Key 也能完整跑通 Agent 流程。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from datapulse.config import Settings

Message = dict[str, str]


class ChatModel(Protocol):
    async def complete(self, messages: list[Message]) -> str: ...


class OpenAICompatibleChat:
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.0):
        from openai import AsyncOpenAI

        if not api_key:
            raise ValueError("未配置大模型 API Key，请在 .env 中设置 DATAPULSE_LLM_API_KEY")
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.temperature = temperature

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleChat:
        return cls(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_temperature)

    async def complete(self, messages: list[Message]) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature
        )
        return resp.choices[0].message.content or ""


class FakeChatModel:
    """按顺序返回预设回复，用于测试和离线演示。同时记录收到的所有提示词，方便断言。"""

    def __init__(self, replies: Iterable[str]):
        self._replies = list(replies)
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message]) -> str:
        self.calls.append(messages)
        if not self._replies:
            raise RuntimeError("FakeChatModel 的预设回复已用完")
        return self._replies.pop(0)
