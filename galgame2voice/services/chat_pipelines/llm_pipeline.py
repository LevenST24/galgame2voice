"""
LLM Stream Pipeline for Galgame2Voice.
Encapsulates token streaming from LLM adapters with cancellation monitoring
and defensive socket/generator cleanup.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger("galgame2voice.services.chat_pipelines.llm_pipeline")


class LlmStreamPipeline:
    """
    Manages asynchronous token streaming from an LLM adapter.
    """

    def __init__(
        self,
        adapter: Any,
        messages: List[Dict[str, Any]],
        model_name: str,
        cancel_event: Optional[asyncio.Event] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
    ) -> None:
        self.adapter = adapter
        self.messages = messages
        self.model_name = model_name
        self.cancel_event = cancel_event
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty

    async def stream_tokens(self) -> AsyncGenerator[str, None]:
        """
        Streams raw tokens from the underlying adapter while checking cancel_event.
        Guarantees socket/generator aclose() cleanup upon completion or interruption.
        """
        stream_kwargs: Dict[str, Any] = {"model": self.model_name}
        if self.temperature is not None:
            stream_kwargs["temperature"] = self.temperature
        if self.top_p is not None:
            stream_kwargs["top_p"] = self.top_p
        if self.max_tokens is not None:
            stream_kwargs["max_tokens"] = self.max_tokens
        if self.frequency_penalty is not None:
            stream_kwargs["frequency_penalty"] = self.frequency_penalty
        if self.presence_penalty is not None:
            stream_kwargs["presence_penalty"] = self.presence_penalty

        stream_gen = None
        try:
            stream_gen = self.adapter.stream_chat(self.messages, **stream_kwargs)
            async for token in stream_gen:
                if self.cancel_event and self.cancel_event.is_set():
                    break
                yield token
        finally:
            if stream_gen is not None and hasattr(stream_gen, "aclose"):
                try:
                    if self.cancel_event and self.cancel_event.is_set():
                        asyncio.create_task(stream_gen.aclose())
                    else:
                        await asyncio.wait_for(stream_gen.aclose(), timeout=0.05)
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.debug("Silently ignored stream generator close error: %s", exc)
