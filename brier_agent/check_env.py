"""Environment preflight: is Python, R, and every needed package in place?

Run it before the first analysis (or after an install), on bare metal or inside the
container:

    python -m brier_agent.check_env

It checks what the agent actually needs to RUN: the Python interpreter and packages, the
Rscript binary, the R packages the MCP tools load, and the bundled MCP server file. It
does NOT touch the model endpoint: whether a local vLLM or an external API is reachable is
a deployment question, and this script must pass with no model configured at all.

Exit status is 0 when every REQUIRED check passes, 1 otherwise. A missing RECOMMENDED
package (a slower fallback exists) or OPTIONAL package (a v2 input format) prints a warning
and does not fail the run, so a minimal install is not blocked by features it will not use.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Minimum Python. The mcp SDK requires 3.10+.
_MIN_PY = (3, 10)

# (import name, pip name, why). REQUIRED to run the agent at all.
_PY_REQUIRED = [
    ("mcp", "mcp", "speaks MCP to the bundled BRIER server"),
    ("openai", "openai", "the one OpenAI-compatible call to the model"),
]
# The chat UI only. The CLI (python -m brier_agent) runs without it.
_PY_UI = [
    ("gradio", "gradio", "the chat UI (app.py); not needed for the CLI"),
]

# R packages, checked in one Rscript call. Tiers:
#   required    - a tool hard-loads it (library()); nothing runs without it.
#   recommended - used when present, with a working fallback (data.table -> base-R
#                 readers; ggplot2 -> the summary report's plots are skipped).
#   optional    - a v2 input format (xlsx / PLINK); only that input needs it.
_R_REQUIRED = ["BRIER", "Matrix", "jsonlite", "survival"]
_R_RECOMMENDED = ["data.table", "ggplot2"]
_R_OPTIONAL = ["readxl", "genio", "BEDMatrix", "pgenlibr"]

_OK = "[ OK ]"
_MISS = "[MISS]"
_WARN = "[WARN]"


@dataclass
class Check:
    ok: bool
    required: bool
    label: str
    detail: str = ""

    def line(self) -> str:
        mark = _OK if self.ok else (_MISS if self.required else _WARN)
        tail = f"  -> {self.detail}" if self.detail and not self.ok else ""
        return f"  {mark}  {self.label}{tail}"


def _rscript_path() -> str | None:
    """Find Rscript the same way the server does: BRIER_RSCRIPT, else PATH."""
    env = os.environ.get("BRIER_RSCRIPT")
    if env:
        return env if (os.path.isfile(env) or shutil.which(env)) else None
    return shutil.which("Rscript")


def _default_mcp_server() -> str:
    # The server ships next to this package: <repo>/mcp/server.py.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "mcp", "server.py")


def _check_python() -> list[Check]:
    out: list[Check] = []
    v = sys.version_info
    ok = (v.major, v.minor) >= _MIN_PY
    out.append(Check(ok, True, f"Python {v.major}.{v.minor}.{v.micro}",
                     f"need >= {_MIN_PY[0]}.{_MIN_PY[1]}"))
    for mod, pip, why in _PY_REQUIRED:
        found = importlib.util.find_spec(mod) is not None
        out.append(Check(found, True, f"python: {mod} ({why})",
                         f"pip install {pip}"))
    for mod, pip, why in _PY_UI:
        found = importlib.util.find_spec(mod) is not None
        out.append(Check(found, False, f"python: {mod} ({why})",
                         f"pip install {pip}"))
    return out


def _check_r() -> list[Check]:
    rscript = _rscript_path()
    if rscript is None:
        return [Check(False, True, "Rscript on PATH",
                      "install R, or set BRIER_RSCRIPT to its Rscript")]
    out = [Check(True, True, f"Rscript ({rscript})")]

    present = _r_probe(rscript, _R_REQUIRED + _R_RECOMMENDED + _R_OPTIONAL)
    if present is None:
        return out + [Check(False, True, "R package probe",
                            "Rscript failed to run the package probe")]

    def tier(names: list[str], required: bool, note: str) -> None:
        for p in names:
            found = present.get(p, False)
            hint = (f"remotes::install_github('UM-KevinHe/BRIER')"
                    if p == "BRIER"
                    else f"install.packages('{p}')  # {note}")
            out.append(Check(found, required, f"R: {p}", hint))

    tier(_R_REQUIRED, True, "required")
    tier(_R_RECOMMENDED, False, "recommended; a fallback exists")
    tier(_R_OPTIONAL, False, "optional; only for its input format")
    return out


def _r_probe(rscript: str, names: list[str]):
    """Return {package: present?} for each name, via one Rscript call. None on failure."""
    vec = ",".join(f"'{p}'" for p in names)
    prog = (f"for (p in c({vec})) "
            "cat(p, ':', as.integer(requireNamespace(p, quietly=TRUE)), '\\n', sep='')")
    try:
        res = subprocess.run([rscript, "-e", prog],
                             capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    present: dict[str, bool] = {}
    for line in res.stdout.splitlines():
        if ":" in line:
            name, _, val = line.partition(":")
            present[name.strip()] = val.strip() == "1"
    return present


def _check_server() -> list[Check]:
    path = os.environ.get("BRIER_MCP_SERVER") or _default_mcp_server()
    return [Check(os.path.isfile(path), True, f"MCP server file ({path})",
                  "set BRIER_MCP_SERVER to mcp/server.py")]


def collect() -> list:
    """Run every check and return grouped results (no printing).

    The UI-facing entry point: a caller can render the returned Check objects
    however it likes. Runs one Rscript subprocess for the R-package probe.
    """
    return [
        ("Python", _check_python()),
        ("R + BRIER", _check_r()),
        ("Bundled server", _check_server()),
    ]


def status(groups=None) -> tuple:
    """Return (missing_required, missing_optional) across all groups."""
    if groups is None:
        groups = collect()
    mr = sum(1 for _, cs in groups for c in cs if not c.ok and c.required)
    mo = sum(1 for _, cs in groups for c in cs if not c.ok and not c.required)
    return mr, mo


def _format(groups) -> str:
    out = ["BRIER-Agent environment check", ""]
    for title, checks in groups:
        out.append(title)
        out += [c.line() for c in checks]
        out.append("")
    mr, mo = status(groups)
    if mr:
        out.append(f"FAIL: {mr} required check(s) missing. "
                   "Install the items marked [MISS] above.")
    elif mo:
        out.append(f"OK (with {mo} optional/recommended item(s) missing, marked "
                   "[WARN]). The agent will run; those features are unavailable.")
    else:
        out.append("OK: every check passed.")
    return "\n".join(out)


def report_text() -> str:
    """The full environment check as a single text block (for the UI / a log)."""
    return _format(collect())


def install_recommended() -> str:
    """Install the recommended + optional R packages that are currently MISSING.

    Installs only from the fixed recommended/optional lists (no arbitrary input).
    Does NOT touch required packages or BRIER: a missing required package is a real
    setup problem, and BRIER installs from GitHub, not CRAN. Returns a text report.

    Note: inside a container this installs into the running container's R library,
    which is lost when the container is recreated; the permanent fix is the Dockerfile.
    """
    rscript = _rscript_path()
    if rscript is None:
        return ("Rscript not found, so R packages cannot be installed. "
                "Install R (>= 4.0), or set BRIER_RSCRIPT to its Rscript.")

    candidates = _R_RECOMMENDED + _R_OPTIONAL
    before = _r_probe(rscript, candidates)
    if before is None:
        return "Could not probe R packages (Rscript failed)."
    missing = [p for p in candidates if not before.get(p, False)]
    if not missing:
        return "All recommended and optional R packages are already installed."

    vec = ",".join(f"'{p}'" for p in missing)
    prog = (f"install.packages(c({vec}), repos='https://cloud.r-project.org')")
    try:
        # Package compilation can be slow; give it room.
        subprocess.run([rscript, "-e", prog], capture_output=True, text=True,
                       timeout=1800)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"Install attempt failed to run: {e}"

    after = _r_probe(rscript, missing) or {}
    ok = [p for p in missing if after.get(p, False)]
    still = [p for p in missing if not after.get(p, False)]
    lines = [f"Attempted to install {len(missing)} package(s): {', '.join(missing)}.", ""]
    if ok:
        lines.append(f"Installed: {', '.join(ok)}.")
    if still:
        lines.append(f"Still missing: {', '.join(still)} (may need system libraries; "
                     "see each package's install notes).")
    if not still:
        lines.append("All recommended/optional packages are now present.")
    return "\n".join(lines)


def run() -> int:
    """CLI entry: print the report, exit 1 iff a REQUIRED item is missing."""
    groups = collect()
    print(_format(groups))
    mr, _ = status(groups)
    return 1 if mr else 0


if __name__ == "__main__":
    raise SystemExit(run())
