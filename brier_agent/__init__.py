"""BRIER-Agent: an agent layer over the BRIER MCP tool surface.

Drives the bundled BRIER MCP server with an OpenAI-compatible LLM (a stub,
a cloud model, or a local vLLM Qwen) through a guarded ReAct loop.
"""
from __future__ import annotations

from .config import AgentConfig
from .loop import BrierAgent, AgentResult
from .llm_client import LLMClient
from .mcp_client import MCPClient
from .prompts import SYSTEM_PROMPT, prompt_sha256

__all__ = [
    "AgentConfig",
    "BrierAgent",
    "AgentResult",
    "LLMClient",
    "MCPClient",
    "SYSTEM_PROMPT",
    "prompt_sha256",
]

__version__ = "1.2.0"
