"""Gradio web UI for the BRIER agent.

One entry point for the deployment targets:

  * Local self-host (``python app.py`` -> http://localhost:7860).
  * Docker container (same as local, with DEPLOYMENT_MODE / Rscript baked in).
  * Hugging Face Space (HF serves ``app.py`` at the repo root).

The UI is a thin wrapper over ``brier_agent.BrierAgent.run``: it takes a user
message (plus an optional data file), runs one agent turn, and renders the
final answer, the tools that were called, and any error. The agent loop is
async (the MCP client is async), and Gradio handlers are sync, so each turn
is bridged with ``asyncio.run``.

Environment variables (see brier_agent/config.py for the full list):

  BRIER_MODEL_ENDPOINT   OpenAI-compatible base URL. Default OpenAI; set to
                         http://localhost:8000/v1 for a local vLLM Qwen.
  BRIER_MODEL_NAME       Model id. Default gpt-4o-mini; qwen2.5-7b-awq for vLLM.
  BRIER_API_KEY          LLM auth (any non-empty value for vLLM).
  BRIER_DEPLOYMENT_MODE  "local" (shows the upload widget) or "demo" (hides it).
  BRIER_AUTH_USER /      Optional Gradio login gate (both must be set). The HF
  BRIER_AUTH_PASS        Space pattern: credentials live in Space Secrets.
"""
from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

# Gradio 4.44 pins gradio_client ~= 1.3, whose JSON-schema walker crashes on
# bool-valued schemas (e.g. additionalProperties: true emitted by gr.Chatbot /
# gr.Dataframe) with "TypeError: argument of type 'bool' is not iterable".
# Patch it in place before importing gradio, matching the fix in
# gradio_client >= 1.4.1. Without this, HF Space startup can crash.
import gradio_client.utils as _gc_utils

_orig_get_type = _gc_utils.get_type


def _safe_get_type(schema):
    if not isinstance(schema, dict):
        return "Any"
    return _orig_get_type(schema)


_gc_utils.get_type = _safe_get_type

_orig_json_to_pytype = _gc_utils._json_schema_to_python_type


def _safe_json_to_pytype(schema, defs=None):
    if not isinstance(schema, dict):
        return "Any"
    return _orig_json_to_pytype(schema, defs)


_gc_utils._json_schema_to_python_type = _safe_json_to_pytype

import gradio as gr

from brier_agent.config import AgentConfig
from brier_agent.loop import BrierAgent
from brier_agent.prompts import deployment_prompt


# --------------------------------------------------------------------------
# Config (resolved once at startup from the environment)
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.resolve()
SAMPLES_DIR = REPO_ROOT / "examples" / "data"

DEPLOYMENT_MODE = os.environ.get("BRIER_DEPLOYMENT_MODE", "local").lower()
DEFAULT_ENDPOINT = os.environ.get(
    "BRIER_MODEL_ENDPOINT", "https://api.openai.com/v1"
)
DEFAULT_MODEL = os.environ.get("BRIER_MODEL_NAME", "gpt-4o-mini")
DEFAULT_API_KEY = os.environ.get("BRIER_API_KEY", "")

INTRO_MD = """\
# BRIER-Agent

Transfer learning for risk prediction from genetic and genomics data with the
[BRIER](https://github.com/UM-KevinHe/BRIER) R package, driven by a tool-using
LLM agent. Describe your data and question in plain language; the agent
identifies the right BRIER module (summary-statistics, individual-level, or
pooled-cohort), preprocesses and aligns the inputs, fits the transfer model,
compares it against a no-transfer baseline and each external source, and
explains the result. Your data stays on this machine.
"""

EXAMPLE_QUERY = (
    "I have GWAS summary statistics and an external beta vector. "
    "Fit a BRIER PRS and cross-validate the transfer strength."
)


# --------------------------------------------------------------------------
# Mode banner (privacy posture, shown at the top)
# --------------------------------------------------------------------------


def _mode_banner_html() -> str:
    if DEPLOYMENT_MODE == "demo":
        return (
            "<div style='background:#fff4e5;color:#8a5300;padding:10px 14px;"
            "border-radius:6px;border:1px solid #ffcc80;font-weight:600;'>"
            "DEMO MODE: file uploads are disabled; use the bundled sample "
            "data only. Tool arguments and chat messages transit the LLM "
            "provider; raw genotype/phenotype values do not."
            "</div>"
        )
    return (
        "<div style='background:#e8f5e9;color:#2e7d32;padding:10px 14px;"
        "border-radius:6px;border:1px solid #a5d6a7;font-weight:600;'>"
        "LOCAL MODE: your data stays on this machine. Only tool calls and "
        "chat messages reach the LLM provider; file contents are read by a "
        "local R subprocess."
        "</div>"
    )


# --------------------------------------------------------------------------
# Data path resolution
# --------------------------------------------------------------------------


def _resolve_upload_path(uploaded_file) -> Optional[str]:
    """Local-mode upload path (Gradio File exposes ``.name``)."""
    if not uploaded_file:
        return None
    try:
        return (
            uploaded_file.name
            if hasattr(uploaded_file, "name")
            else str(uploaded_file)
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# Agent construction + the async-to-sync bridge
# --------------------------------------------------------------------------


def _make_agent(endpoint: str, model: str, api_key: str) -> BrierAgent:
    """Build a fresh agent per query (keeps the per-turn state simple)."""
    config = AgentConfig.from_env()
    if endpoint:
        config.model_endpoint = endpoint
    if model:
        config.model_name = model
    if api_key:
        config.api_key = api_key
    config.deployment_mode = DEPLOYMENT_MODE
    return BrierAgent(config=config, system_prompt=deployment_prompt())


def _run_agent_sync(agent: BrierAgent, user_msg: str, data_path: Optional[str],
                    history_pairs):
    """Run one async agent turn from a sync Gradio handler."""
    return asyncio.run(
        agent.run(user_msg, data_path=data_path, history=history_pairs)
    )


# --------------------------------------------------------------------------
# Chat handler
# --------------------------------------------------------------------------


def chat_submit(
    user_msg: str,
    history: List[Tuple[str, str]],
    uploaded_file,
    endpoint: str,
    model: str,
    api_key: str,
):
    """One round: send the user message through the agent, render results."""
    history = history or []
    if not user_msg or not user_msg.strip():
        return history, "", None

    data_path = _resolve_upload_path(uploaded_file)

    try:
        agent = _make_agent(endpoint, model, api_key)
        result = _run_agent_sync(agent, user_msg, data_path, history)
    except Exception:
        tb = traceback.format_exc()
        history.append(
            (user_msg, f"**Agent crashed:**\n```\n{tb[-2000:]}\n```")
        )
        return history, "", None

    assistant_text = result.text or "_(no answer)_"
    if result.error:
        assistant_text += f"\n\n**Note:** {result.error}"

    # A compact, UI-only marker of which tools ran this turn.
    if result.tool_results:
        ran = ", ".join(
            f"`{tr['tool']}`"
            + (
                ""
                if isinstance(tr["result"], dict)
                and tr["result"].get("status") == "ok"
                else " (error)"
            )
            for tr in result.tool_results
        )
        assistant_text += f"\n\n<small>Tools: {ran}</small>"

    history.append((user_msg, assistant_text))

    # A simple table of the tool calls for the results panel.
    tool_rows = [
        [
            tr["tool"],
            tr["result"].get("status", "?")
            if isinstance(tr["result"], dict)
            else "?",
        ]
        for tr in result.tool_results
    ]

    return history, "", (tool_rows or None)


def reset_chat():
    return [], "", None


def _env_check():
    """Run the environment preflight and return it as a fenced text block."""
    from brier_agent import check_env
    return "```text\n" + check_env.report_text() + "\n```"


def _install_recommended():
    """Install the missing recommended/optional R packages, then show the result."""
    from brier_agent import check_env
    return "```text\n" + check_env.install_recommended() + "\n```"


def _test_connection(endpoint, model, api_key):
    """Check the current model endpoint is reachable and serving the named model."""
    from brier_agent.llm_client import probe_endpoint
    return probe_endpoint(endpoint, model, api_key)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

with gr.Blocks(title="BRIER-Agent", theme=gr.themes.Soft()) as demo:
    gr.HTML(_mode_banner_html())
    gr.Markdown(INTRO_MD)

    with gr.Accordion("Model & connection", open=False):
        with gr.Row():
            endpoint_in = gr.Textbox(
                value=DEFAULT_ENDPOINT,
                label="Model endpoint (OpenAI-compatible)",
                placeholder="https://api.openai.com/v1",
            )
            model_in = gr.Textbox(
                value=DEFAULT_MODEL,
                label="Model name",
                placeholder="gpt-4o-mini",
            )
            api_key_in = gr.Textbox(
                value=DEFAULT_API_KEY,
                label="API key",
                type="password",
                placeholder="sk-...",
            )
        test_conn_btn = gr.Button("Test connection")
        conn_out = gr.Markdown()

    with gr.Accordion("Environment check", open=False):
        gr.Markdown(
            "Verify Python, R, the BRIER package, and the tool dependencies are in "
            "place. Required items missing block a run; recommended/optional ones only "
            "disable a feature. Installing the recommended/optional R packages can take a "
            "few minutes (they compile), and in a Docker container it lasts only for this "
            "container; the permanent fix is to add them to the image."
        )
        with gr.Row():
            env_btn = gr.Button("Check environment")
            install_btn = gr.Button("Install recommended/optional R packages")
        env_out = gr.Markdown()

    with gr.Row():
        # ---- Left: data ----
        with gr.Column(scale=1, min_width=260):
            gr.Markdown("### Your data")
            data_upload = gr.File(
                label="Upload a data file (.rds / .rda / .RData)",
                file_types=[".rds", ".rda", ".RData"],
                visible=(DEPLOYMENT_MODE == "local"),
            )
            gr.Markdown(
                "The agent inspects the file's structure before fitting; "
                "you do not need to describe the columns yourself."
            )

        # ---- Center: chat ----
        with gr.Column(scale=2, min_width=420):
            chatbot = gr.Chatbot(
                height=500, label="Conversation", show_copy_button=True
            )
            msg_in = gr.Textbox(
                placeholder=f"e.g. {EXAMPLE_QUERY}",
                lines=2,
                label="Your message",
                elem_id="msg_in",
            )
            with gr.Row():
                send_btn = gr.Button("Send", variant="primary")
                clear_btn = gr.Button("Clear")

        # ---- Right: results ----
        with gr.Column(scale=1, min_width=280):
            gr.Markdown("### Tools called")
            tools_out = gr.Dataframe(
                headers=["tool", "status"],
                label="Tool calls this turn",
                interactive=False,
                wrap=True,
            )

    # ----------------------------- Handlers --------------------------------
    inputs = [msg_in, chatbot, data_upload, endpoint_in, model_in, api_key_in]
    outputs = [chatbot, msg_in, tools_out]

    send_btn.click(chat_submit, inputs=inputs, outputs=outputs)
    msg_in.submit(chat_submit, inputs=inputs, outputs=outputs)
    clear_btn.click(reset_chat, inputs=None, outputs=outputs)
    env_btn.click(_env_check, inputs=None, outputs=env_out)
    install_btn.click(_install_recommended, inputs=None, outputs=env_out)
    test_conn_btn.click(
        _test_connection,
        inputs=[endpoint_in, model_in, api_key_in],
        outputs=conn_out,
    )


# --------------------------------------------------------------------------
# Auth gate (optional; engaged only when both env vars are set)
# --------------------------------------------------------------------------


def _resolve_auth():
    """Return a Gradio auth tuple from env, or None when unset.

    Both BRIER_AUTH_USER and BRIER_AUTH_PASS must be non-empty for the login
    gate to engage. HF Space pattern: credentials live in Space Secrets, never
    in the image. Local and dev runs leave them unset so Gradio launches with
    no login page.
    """
    user = os.environ.get("BRIER_AUTH_USER", "").strip()
    pw = os.environ.get("BRIER_AUTH_PASS", "").strip()
    if user and pw:
        return (user, pw)
    return None


if __name__ == "__main__":
    demo.queue()
    _auth = _resolve_auth()
    if _auth is not None:
        print(f"[app] Gradio auth enabled (user={_auth[0]})", flush=True)
    demo.launch(
        server_name=os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
        inbrowser=False,
        show_error=True,
        show_api=False,
        auth=_auth,
        auth_message=(
            "Reviewer access only. Credentials are provided in the paper "
            "submission. For your own data, use the local install or the "
            "Docker self-host."
        ),
    )
