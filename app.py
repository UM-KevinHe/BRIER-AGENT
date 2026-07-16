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
  BRIER_INCLUDE_TOOLS    Tool allowlist. Default: a validated ~11-tool core that
                         fits a small model's context. "all" exposes the full
                         surface (for a large-context model); or a comma list.
  BRIER_AUTH_USER /      Optional Gradio login gate (both must be set). The HF
  BRIER_AUTH_PASS        Space pattern: credentials live in Space Secrets.
"""
from __future__ import annotations

import asyncio
import os
import re
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


# Tool schemas are re-sent on EVERY turn and are a fixed per-turn tax on the context window.
# The full 31-tool surface is ~23k tokens: on a small-context model (Qwen 2.5-7B, 32769) that
# overflows before the analysis runs ("inputs + max_new > 32769", a 422). Default the UI to
# the validated CORE tool set (the benchmark's, plus summarize_fit for the report step in
# deployment_prompt) -- ~11 tools, which every supported workflow needs and which fits the
# window with room for the prompt and history. Override with BRIER_INCLUDE_TOOLS: "all" exposes
# the full surface (for a large-context model), or a comma-separated list of tool names.
_CORE_TOOLS = {
    "inspect_user_data", "prep_auto",
    "brier_i", "brier_s", "brier_full",
    "brier_i_selection", "brier_s_selection", "brier_full_selection",
    "brier_evaluate", "score_external_prs",
    # NOTE: summarize_fit is deliberately NOT here. The HTML report + reproduce.R are
    # generated by the harness after the run (_generate_report), deterministically from the
    # recorded selection_id + prepared object, rather than by asking a small model to
    # construct the summarize_fit call (which it does unreliably). Keeping it out also frees
    # its schema tokens.
}


def _agent_tools():
    """The tool allowlist for the UI agent (None => the full surface)."""
    raw = os.environ.get("BRIER_INCLUDE_TOOLS", "").strip()
    if raw.lower() == "all":
        return None
    if raw:
        return {t.strip() for t in raw.split(",") if t.strip()}
    return set(_CORE_TOOLS)


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
    return BrierAgent(config=config, system_prompt=deployment_prompt(),
                      include_tools=_agent_tools())


# The assistant turns stored for the chatbot carry UI-only decorations: the
# "<small>Tools: ...</small>" marker (which names failed tools) and a "**Note:** ..."
# line for an aborted/errored run. Those are for the human to read, but the same string
# is fed back to the MODEL as history -- and a small model that sees "prep_auto (error)"
# in its own prior turns is primed to REPEAT the failure (a stray fumble snowballs into a
# stuck loop). Strip them so the model's history holds only the substantive answer.
# The assistant turn appends UI-only decorations after the substantive answer: a
# "**Note:** ..." abort line, an "**Errors:** ..." block (the real tool error messages), and
# a "<small>Tools: ...</small>" marker. Cut at the EARLIEST of these so none of them, in any
# order, reaches the model as history.
_UI_MARKERS = ("\n\n**Decision (computed):**", "\n\n**Report:**", "\n\n**Note:**",
               "\n\n**Errors:**", "\n\n<small>")


def _decision_summary(tool_results):
    """A deterministic transfer-vs-external comparison from the test R^2 metrics.

    A small model reliably produces the right numbers but sometimes narrates the comparison
    backwards ("does not beat" when 0.1575 > 0.1311). Higher R^2 is better and both the
    transfer metric (brier_evaluate) and the external-only metric (score_external_prs) report
    the same scale-invariant cor^2, so the comparison is exact. Returns a markdown line, or ""
    if the two comparable metrics are not both present. UI-only (stripped from model history).
    """
    transfer = external = None
    for tr in (tool_results or []):
        r = tr.get("result")
        if not isinstance(r, dict) or r.get("status") != "ok":
            continue
        crit, val = r.get("criteria"), r.get("metric_value")
        if val is None or not str(crit).endswith("rsq"):
            continue
        if tr.get("tool") == "brier_evaluate":
            transfer = float(val)
        elif tr.get("tool") == "score_external_prs":
            external = float(val)
    if transfer is None or external is None:
        return ""
    if transfer > external:
        who = (f"the **transfer model wins** (test R^2 {transfer:.4f} > external-only "
               f"{external:.4f})")
    elif transfer < external:
        who = (f"the **external model alone wins** (test R^2 {external:.4f} > transfer "
               f"{transfer:.4f})")
    else:
        who = f"they **tie** on test R^2 ({transfer:.4f})"
    return f"**Decision (computed):** higher R^2 is better, so {who}."


def _history_for_model(history_pairs):
    """Return history with the UI-only tool/error decorations stripped from assistant turns."""
    cleaned = []
    for entry in history_pairs or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        u, a = entry[0], entry[1]
        if a:
            a = str(a)
            cut = len(a)
            for marker in _UI_MARKERS:
                idx = a.find(marker)
                if idx != -1:
                    cut = min(cut, idx)
            a = a[:cut].strip()
        cleaned.append((u, a))
    return cleaned


def _report_inputs(tool_results):
    """Assemble a correct summarize_fit call from a completed fitting chain: the last
    selection_id, the prepared object's path, and its held-out-test expressions. Returns the
    argument dict, or None when there is no fit to report (a chat turn, a failed run)."""
    sel_id = data_path = None
    hints, crit = {}, None
    for tr in (tool_results or []):
        r = tr.get("result")
        if not isinstance(r, dict) or r.get("status") != "ok":
            continue
        t = tr.get("tool")
        if t in ("brier_i_selection", "brier_s_selection", "brier_full_selection"):
            sel_id = r.get("selection_id") or sel_id
        elif t == "prep_auto":
            data_path = r.get("prepared_path") or data_path
            hints = r.get("expr_hints") or hints
        elif t == "brier_evaluate":
            crit = r.get("criteria") or crit
    if not sel_id:
        return None
    args = {"selection_id": sel_id}
    if data_path:
        args["data_path"] = data_path
    nx, ny = hints.get("X_test_expr"), hints.get("y_test_expr")
    if nx and ny:
        args.update(newx_expr=nx, newy_expr=ny, criteria=crit or "gaussian.rsq")
    return args


async def _generate_report(agent: BrierAgent, tool_results):
    """Deterministically generate the HTML report + reproduce.R after a fit, using the
    recorded selection_id + prepared object (never the model's own summarize_fit call).
    Returns a markdown status line to show the user, or "" when there is no fit to report."""
    args = _report_inputs(tool_results)
    if not args:
        return ""
    try:
        async with agent.mcp.session() as session:
            r = await agent.mcp.call_tool(session, "summarize_fit", args)
    except Exception as e:  # never let report generation break the turn
        return f"\n\n**Report:** could not be generated ({type(e).__name__}: {e})."
    if isinstance(r, dict) and r.get("status") == "ok":
        html = r.get("report_html_path") or ""
        repro = r.get("reproduce_r_path") or ""
        line = "\n\n**Report:** generated."
        if html:
            line += f" HTML: `{html}`"
        if repro:
            line += f"  |  reproduce script: `{repro}`"
        return line
    msg = (r.get("message") or r.get("error") or "") if isinstance(r, dict) else str(r)
    return f"\n\n**Report:** not generated ({str(msg)[:200]})."


async def _run_and_report(agent, user_msg, data_path, history_pairs):
    result = await agent.run(user_msg, data_path=data_path,
                             history=_history_for_model(history_pairs))
    report_line = await _generate_report(agent, result.tool_results)
    return result, report_line


def _run_agent_sync(agent: BrierAgent, user_msg: str, data_path: Optional[str],
                    history_pairs):
    """Run one async agent turn (plus deterministic report generation) from a sync handler."""
    return asyncio.run(_run_and_report(agent, user_msg, data_path, history_pairs))


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
        result, report_line = _run_agent_sync(agent, user_msg, data_path, history)
    except Exception:
        tb = traceback.format_exc()
        history.append(
            (user_msg, f"**Agent crashed:**\n```\n{tb[-2000:]}\n```")
        )
        return history, "", None

    assistant_text = result.text or "_(no answer)_"

    # A deterministic transfer-vs-external comparison, so a backwards narration ("does not
    # beat" when the transfer R^2 is actually higher) is corrected by the numbers themselves.
    decision = _decision_summary(result.tool_results)
    if decision:
        assistant_text += f"\n\n{decision}"

    # The report is generated by the harness (deterministically), not the model.
    if report_line:
        assistant_text += report_line

    if result.error:
        assistant_text += f"\n\n**Note:** {result.error}"

    # Surface the ACTUAL error message from any failed tool call, so a stuck run can be
    # debugged from the chat instead of showing only "tool (error)". Deduplicated, since a
    # looping call repeats the same message. These are stripped from the model's history by
    # _history_for_model (they are for the human).
    errs, seen = [], set()
    for tr in (result.tool_results or []):
        r = tr.get("result")
        if isinstance(r, dict) and r.get("status") != "ok":
            msg = str(r.get("message") or r.get("error") or "").strip()
            key = (tr["tool"], msg)
            if msg and key not in seen:
                seen.add(key)
                errs.append(f"- `{tr['tool']}`: {msg}")
    if errs:
        assistant_text += "\n\n**Errors:**\n" + "\n".join(errs)

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

    # A table of the tool calls for the results panel, with the error message when present.
    tool_rows = [
        [
            tr["tool"],
            tr["result"].get("status", "?") if isinstance(tr["result"], dict) else "?",
            (str(tr["result"].get("message") or tr["result"].get("error") or "")
             if isinstance(tr["result"], dict) else "")[:300],
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
                headers=["tool", "status", "message"],
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
