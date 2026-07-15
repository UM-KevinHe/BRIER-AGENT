"""A scriptable stub LLM client for tests and offline development.

This fake exposes the same ``chat.completions.create`` surface the real
OpenAI SDK does, but returns pre-scripted responses instead of calling a
model. It lets the whole agent loop run on a laptop with no network, no
API key, and no GPU, which is how we validate the loop's mechanics before
the real Qwen path exists.

You script it with a list of "turns". Each turn is either:
  * a list of tool-call requests:  [{"name": "...", "arguments": {...}}, ...]
  * a final text answer:           "some assistant text"

The stub returns them in order, one per ``create`` call, mimicking how a
real model alternates between asking for tools and giving a final answer.

Example
-------
    stub = StubLLM(script=[
        [{"name": "inspect_data", "arguments": {"data_path": "/x.rds"}}],
        "Here is what I found in your data.",
    ])
    # First create() -> a tool call; second create() -> final text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Union

TurnScript = Union[str, List[Dict[str, Any]]]


# -- Minimal response objects mimicking the OpenAI SDK shape -----------------


@dataclass
class _StubFunction:
    name: str
    arguments: str  # JSON string, as the real SDK provides


@dataclass
class _StubToolCall:
    id: str
    function: _StubFunction
    type: str = "function"


@dataclass
class _StubMessage:
    content: str = ""
    tool_calls: List[_StubToolCall] = field(default_factory=list)


@dataclass
class _StubChoice:
    message: _StubMessage


@dataclass
class _StubUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _StubCompletion:
    choices: List[_StubChoice]
    usage: _StubUsage = field(default_factory=_StubUsage)


# -- The scriptable client ---------------------------------------------------


class _Completions:
    def __init__(self, parent: "StubLLM") -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _StubCompletion:
        return self._parent._next_completion(**kwargs)


class _Chat:
    def __init__(self, parent: "StubLLM") -> None:
        self.completions = _Completions(parent)


class StubLLM:
    """A fake OpenAI-style client returning scripted responses.

    Parameters
    ----------
    script:
        A list of turns. Each turn is either a final-text string or a list
        of tool-call dicts ``{"name": str, "arguments": dict}``. Returned
        one per ``create`` call, in order. When the script is exhausted,
        the stub returns an empty final-text turn (ends the loop safely).
    """

    def __init__(self, script: List[TurnScript]) -> None:
        self._script = list(script)
        self._i = 0
        self.calls: List[Dict[str, Any]] = []  # records each create() kwargs
        self.chat = _Chat(self)

    def _next_completion(self, **kwargs: Any) -> _StubCompletion:
        # Record what the loop sent, so tests can assert on messages/tools.
        self.calls.append(kwargs)

        if self._i >= len(self._script):
            # Script exhausted: emit an empty final answer to end the loop.
            return _StubCompletion(
                choices=[_StubChoice(message=_StubMessage(content=""))]
            )

        turn = self._script[self._i]
        self._i += 1

        if isinstance(turn, str):
            return _StubCompletion(
                choices=[_StubChoice(message=_StubMessage(content=turn))]
            )

        # Otherwise it is a list of tool-call requests.
        tool_calls = [
            _StubToolCall(
                id=f"call_{self._i}_{j}",
                function=_StubFunction(
                    name=tc["name"],
                    arguments=json.dumps(tc.get("arguments", {})),
                ),
            )
            for j, tc in enumerate(turn)
        ]
        return _StubCompletion(
            choices=[_StubChoice(message=_StubMessage(content="", tool_calls=tool_calls))]
        )
