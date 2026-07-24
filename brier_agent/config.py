"""Configuration for the BRIER agent.

All settings are env-overridable so the same code runs against a stub, a
cheap cloud API (for development on a laptop without a GPU), or a local
vLLM server hosting Qwen (the eventual target). Swapping backends is a
matter of changing the endpoint + model name, nothing in the loop.

Environment variables
---------------------
BRIER_MODEL_ENDPOINT   OpenAI-compatible base URL.
                       Default: https://api.openai.com/v1
                       Set to http://localhost:8000/v1 for a local vLLM Qwen.
BRIER_MODEL_NAME       Model identifier the endpoint expects.
                       Default: gpt-4o-mini (a cheap dev model).
                       Use qwen2.5-7b-awq (or the served name) for vLLM.
BRIER_API_KEY          Bearer token. vLLM ignores the value; pass anything
                       non-empty. For OpenAI, your real key.
BRIER_MCP_SERVER       Path to the bundled MCP server entry point.
                       Default: <repo>/mcp/server.py
BRIER_RSCRIPT          Rscript path, forwarded to the MCP server's env so it
                       finds R. Optional; the server can also discover it.
BRIER_MAX_TURNS        Loop safety cap (max LLM turns). Default: 12.
BRIER_MAX_TOKENS       Per-turn generation cap. Default: 2048.
BRIER_TEMPERATURE      Decoding temperature. Default: 0.0 (greedy).
BRIER_DEPLOYMENT_MODE  "local" or "demo". Recorded for the UI. Default: local.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _repo_root() -> Path:
    """Best-effort path to the BRIER-Agent repo root.

    This file lives at <repo>/brier_agent/config.py, so the root is two
    parents up. Falls back to the current directory if that structure is
    not present (e.g. installed as a package elsewhere).
    """
    here = Path(__file__).resolve()
    candidate = here.parent.parent
    if (candidate / "mcp" / "server.py").exists():
        return candidate
    return Path.cwd()


def _default_mcp_server() -> str:
    return str(_repo_root() / "mcp" / "server.py")


def _env_on(name: str) -> bool:
    """A layer toggle: ON unless the env var is explicitly falsy (0/false/no/off).

    Default ON is deliberate: an ordinary run (the var unset) is always the full agent,
    so a layer can never be disabled by accident -- only a dedicated ablation that sets
    the var to a falsy value turns it off. Every value other than the explicit falsies
    (including unset) is ON.
    """
    v = os.environ.get(name, "").strip().lower()
    return v not in ("0", "false", "no", "off")


@dataclass
class AgentConfig:
    """Resolved agent settings. Build with :meth:`from_env` or directly.

    Direct construction is handy in tests; ``from_env`` is the normal path
    for the CLI and the UI.
    """

    model_endpoint: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    api_key: str = "EMPTY"
    mcp_server_path: str = ""
    rscript: Optional[str] = None
    max_turns: int = 12
    max_tokens: int = 2048
    temperature: float = 0.0
    # Sampling seed passed to the model API (OpenAI/Together honor `seed`), so a run is
    # reproducible and repeats are independent, controlled samples. None = unset.
    seed: Optional[int] = None
    deployment_mode: str = "local"

    # --- Ablation layer toggles (all default ON) ---------------------------------
    # Each scaffolding layer is ON by default and disabled ONLY when its env var is
    # explicitly falsy (0/false/no/off). Keeping the default ON means an ordinary run is
    # always the full agent, and an accidental disable is impossible: the var is simply
    # unset. A dedicated ablation sbatch sets exactly one of these for a controlled OFF.
    enable_hooks: bool = True            # BRIER_HOOKS: continuation hooks + retry nudges (T3)
    enable_context_inject: bool = True   # BRIER_CONTEXT_INJECT: scaffolded inspect-field injection (T2)
    enable_guards: bool = True           # BRIER_GUARDS: repeated-call / stall guards + abort (T5)
    enable_role_advisor: bool = True     # BRIER_ROLE_ADVISOR: data-role advisor injection (T6)

    def __post_init__(self) -> None:
        # Fill the MCP server path lazily so the dataclass default does not
        # run filesystem logic at import time.
        if not self.mcp_server_path:
            self.mcp_server_path = _default_mcp_server()

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Build a config from environment variables, with sane defaults."""
        return cls(
            model_endpoint=os.environ.get(
                "BRIER_MODEL_ENDPOINT", "https://api.openai.com/v1"
            ),
            model_name=os.environ.get("BRIER_MODEL_NAME", "gpt-4o-mini"),
            api_key=os.environ.get("BRIER_API_KEY", "") or "EMPTY",
            mcp_server_path=os.environ.get("BRIER_MCP_SERVER", "")
            or _default_mcp_server(),
            rscript=os.environ.get("BRIER_RSCRIPT") or None,
            max_turns=int(os.environ.get("BRIER_MAX_TURNS", "12")),
            max_tokens=int(os.environ.get("BRIER_MAX_TOKENS", "2048")),
            temperature=float(os.environ.get("BRIER_TEMPERATURE", "0.0")),
            seed=(int(os.environ["BRIER_SEED"])
                  if os.environ.get("BRIER_SEED", "").strip() else None),
            deployment_mode=os.environ.get(
                "BRIER_DEPLOYMENT_MODE", "local"
            ).lower(),
            enable_hooks=_env_on("BRIER_HOOKS"),
            enable_context_inject=_env_on("BRIER_CONTEXT_INJECT"),
            enable_guards=_env_on("BRIER_GUARDS"),
            enable_role_advisor=_env_on("BRIER_ROLE_ADVISOR"),
        )

    def server_env(self) -> dict:
        """Environment dict to pass to the spawned MCP server subprocess.

        Forwards BRIER_RSCRIPT when set so the server finds R the same way
        the standalone deployments do. Inherits the rest of the parent env.
        """
        env = dict(os.environ)
        if self.rscript:
            env["BRIER_RSCRIPT"] = self.rscript
        return env
