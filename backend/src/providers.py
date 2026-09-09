"""Pluggable LLM synthesis with explicit, observable offline fallback."""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class LLMEvent:
    type: str
    content: str = ""
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    fallback_reason: str | None = None


def _pricing() -> dict[str, tuple[float, float]]:
    """Read USD-per-million token rates from config so prices never go stale in code."""
    raw = os.getenv("LLM_PRICING_JSON", "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
        return {
            str(model): (float(rates[0]), float(rates[1]))
            for model, rates in values.items()
            if isinstance(rates, list) and len(rates) == 2
        }
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_rate, output_rate = _pricing().get(model, (0.0, 0.0))
    return input_tokens / 1_000_000 * input_rate + output_tokens / 1_000_000 * output_rate


class BaseProvider(ABC):
    name = "base"

    @abstractmethod
    async def stream(
        self, system: str, user: str, fallback: str
    ) -> AsyncIterator[LLMEvent]:
        if False:  # pragma: no cover - makes this an async generator contract
            yield LLMEvent("done")


class OfflineProvider(BaseProvider):
    name = "offline"

    def __init__(self, reason: str | None = None):
        self.reason = reason

    async def stream(
        self, system: str, user: str, fallback: str
    ) -> AsyncIterator[LLMEvent]:
        del system
        model = "offline-grounded"
        for token in re.findall(r"\S+\s*", fallback):
            yield LLMEvent(
                "token",
                token,
                model=model,
                provider=self.name,
                fallback_reason=self.reason,
            )
        yield LLMEvent(
            "usage",
            model=model,
            provider=self.name,
            input_tokens=max(1, len(user) // 4),
            output_tokens=max(1, len(fallback) // 4),
            fallback_reason=self.reason,
        )


class OpenAICompatibleProvider(BaseProvider):
    name = "openai-compatible"

    def __init__(self) -> None:
        self.key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.base = (
            os.getenv("LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.timeout = max(5.0, float(os.getenv("LLM_TIMEOUT_SECONDS", "45")))
        self.max_output_tokens = min(
            4_096, max(64, int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1200")))
        )
        self.max_input_chars = min(
            200_000, max(2_000, int(os.getenv("LLM_MAX_INPUT_CHARS", "60000")))
        )

    async def stream(
        self, system: str, user: str, fallback: str
    ) -> AsyncIterator[LLMEvent]:
        if not self.key:
            async for event in OfflineProvider("provider key is not configured").stream(
                system, user, fallback
            ):
                yield event
            return
        input_tokens = max(1, (len(system) + min(len(user), self.max_input_chars)) // 4)
        output_tokens = 0
        emitted_content = False
        try:
            import httpx

            bounded_user = user[: self.max_input_chars]
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": bounded_user},
                ],
                "temperature": 0.1,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": self.max_output_tokens,
            }
            timeout = httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        data = json.loads(line[6:])
                        content = (
                            (data.get("choices") or [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if content:
                            emitted_content = True
                            output_tokens += max(1, len(content) // 4)
                            yield LLMEvent(
                                "token",
                                content,
                                model=self.model,
                                provider=self.name,
                            )
                        if usage := data.get("usage"):
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)
            yield LLMEvent(
                "usage",
                model=self.model,
                provider=self.name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimate_cost(self.model, input_tokens, output_tokens),
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: provider request failed"
            if emitted_content:
                yield LLMEvent(
                    "token",
                    "\n\n> Luồng mô hình bị gián đoạn; phần trả lời trên có thể chưa hoàn chỉnh.",
                    model=self.model,
                    provider=self.name,
                    fallback_reason=reason,
                )
                yield LLMEvent(
                    "usage",
                    model=self.model,
                    provider=self.name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimate_cost(self.model, input_tokens, output_tokens),
                    fallback_reason=reason,
                )
                return
            async for event in OfflineProvider(reason).stream(system, user, fallback):
                yield event


def get_provider() -> BaseProvider:
    provider = os.getenv("LLM_PROVIDER", "openai-compatible").strip().lower()
    if provider in {"offline", "local", "none"}:
        return OfflineProvider("offline provider selected")
    return OpenAICompatibleProvider()
