"""LLM provider abstraction (ADR-006).

The reason this port exists is not vendor shopping, it is residency: PDPL may
require inference to happen in-Kingdom or on self-hosted models, and that must
be a configuration change rather than a rewrite of every agent under regulatory
time pressure.

Model choice is expressed as a *tier*, never a hard-coded model id, so the
mapping can be retuned as prices and capabilities move without touching agent
code.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ModelTier(StrEnum):
    """What an agent needs, not which model provides it."""

    SMALL = "SMALL"  # classification, signal detection
    STANDARD = "STANDARD"  # structured extraction, verification synthesis
    LARGE = "LARGE"  # investment memo synthesis


class ProviderError(RuntimeError):
    """Provider could not produce a response."""


class ProviderUnavailableError(ProviderError):
    """Provider is not configured -- missing credentials, or disabled by policy."""


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    user: str
    tier: ModelTier
    schema_name: str
    json_schema: dict[str, Any]
    max_output_tokens: int = 2048
    temperature: float = 0.0
    # Untrusted external content is passed separately so the provider adapter
    # can never accidentally concatenate it into the instruction block.
    untrusted_blocks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMProvider(abc.ABC):
    """Every provider adapter implements exactly this."""

    name: str

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...

    @abc.abstractmethod
    def available(self) -> bool: ...


# --------------------------------------------------------------------------
# Offline deterministic provider


class DeterministicProvider(LLMProvider):
    """An offline stand-in that satisfies the port without calling a model.

    This is **not an LLM and does not pretend to be one.** It exists so that:

      * the pipeline runs end to end without credentials;
      * no property text leaves the region during development, which is the
        cross-border transfer the PDPL assessment is most concerned with;
      * the runtime's contract (schema validation, cost accounting, recording,
        injection framing) is exercised by the test suite deterministically.

    Runs are recorded with `provider="deterministic-offline"`, so no stored
    result can later be mistaken for model reasoning.
    """

    name = "deterministic-offline"

    def __init__(self, responder: Any | None = None) -> None:
        self._responder = responder

    def available(self) -> bool:
        return True

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._responder is None:
            raise ProviderError(
                "DeterministicProvider needs a responder for "
                f"schema {request.schema_name!r}; none was configured"
            )
        text = self._responder(request)
        return LLMResponse(
            text=text,
            provider=self.name,
            model="deterministic",
            input_tokens=len(request.system) // 4 + len(request.user) // 4,
            output_tokens=len(text) // 4,
            cost_usd=Decimal("0"),
            metadata={"offline": True},
        )


# --------------------------------------------------------------------------
# Real adapters. Wired, but inert without credentials -- deliberately explicit
# rather than silently falling back, so nobody ships thinking inference ran.


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    MODELS = MappingProxyType(
        {
            ModelTier.SMALL: "claude-haiku-4-5-20251001",
            ModelTier.STANDARD: "claude-sonnet-5",
            ModelTier.LARGE: "claude-opus-5",
        }
    )

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def available(self) -> bool:
        return bool(self._api_key)

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.available():
            raise ProviderUnavailableError(
                "ANTHROPIC_API_KEY is not set. Inference is disabled rather than "
                "silently skipped; configure a key or select another provider."
            )
        raise ProviderUnavailableError(
            "Anthropic adapter is wired but has not been exercised in this "
            "environment (no credentials). Implement the HTTP call and add a "
            "recorded cross-border transfer assessment before enabling it."
        )


class OpenAIProvider(LLMProvider):
    name = "openai"

    MODELS = MappingProxyType(
        {
            ModelTier.SMALL: "gpt-4.1-mini",
            ModelTier.STANDARD: "gpt-4.1",
            ModelTier.LARGE: "gpt-4.1",
        }
    )

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")

    def available(self) -> bool:
        return bool(self._api_key)

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.available():
            raise ProviderUnavailableError("OPENAI_API_KEY is not set.")
        raise ProviderUnavailableError(
            "OpenAI adapter is wired but has not been exercised in this environment."
        )


def default_provider(responder: Any | None = None) -> LLMProvider:
    """Pick a provider from the environment.

    Order is deliberate: an explicitly configured provider wins, otherwise we
    fall back to offline. We never pick a cross-border provider implicitly.
    """
    preference = os.environ.get("SREOI_LLM_PROVIDER", "").strip().lower()
    candidates: dict[str, LLMProvider] = {
        "anthropic": AnthropicProvider(),
        "openai": OpenAIProvider(),
    }
    if preference in candidates and candidates[preference].available():
        return candidates[preference]
    return DeterministicProvider(responder)
