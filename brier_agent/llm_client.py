"""OpenAI-compatible LLM client wrapper.

A thin layer over the OpenAI Python SDK's chat-completions call. It knows
nothing about BRIER or MCP: it sends messages plus a tool list and returns
the model's response. This isolation is deliberate, it is the one place
that talks to the model, so swapping a stub, a cheap cloud model, or a
local vLLM Qwen changes only what this client points at, never the loop.

The underlying client is constructed lazily and can be injected (the
``client`` argument), which is how tests run the whole agent loop against a
fake model with no network and no GPU.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class LLMClient:
    """Wrapper over an OpenAI-compatible chat-completions endpoint.

    Parameters
    ----------
    endpoint:
        OpenAI-compatible base URL (vLLM, OpenAI, Together, ...).
    model_name:
        Model identifier the endpoint expects.
    api_key:
        Bearer token. vLLM ignores the value; pass anything non-empty.
    client:
        Optional pre-built client exposing ``chat.completions.create``.
        When omitted, a real ``openai.OpenAI`` is built lazily on first
        use. Tests inject a stub here.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        api_key: str = "EMPTY",
        client: Optional[Any] = None,
    ) -> None:
        self.endpoint = endpoint
        self.model_name = model_name
        self.api_key = api_key
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover - exercised only without openai
            raise RuntimeError(
                "openai SDK not installed. Run `pip install openai`."
            ) from e
        self._client = OpenAI(base_url=self.endpoint, api_key=self.api_key)
        return self._client

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        tool_choice: str = "auto",
    ) -> Any:
        """Call chat-completions once and return the raw response object.

        Returns the SDK's completion object (so the caller can read
        ``choices[0].message`` including ``tool_calls`` and ``usage``).
        Native tool-calling: ``tools`` is the OpenAI function-tool schema
        list, and the model replies with structured ``tool_calls`` when it
        wants to act. Greedy decoding (temperature 0) by default, matching
        the deterministic-routing requirement for small models.
        """
        client = self._ensure_client()
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        return client.chat.completions.create(**kwargs)
