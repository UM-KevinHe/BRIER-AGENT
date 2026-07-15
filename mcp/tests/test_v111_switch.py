"""Tests for v1.1.1 - the deployment switcher (brier-switch.sh).

The switcher manages which BRIER deployment is active in Claude Desktop
when you have more than one (local, plus remote servers). It keeps a
registry (~/.brier-mcp/servers.json) and can list, add, print-to-activate,
or (with --write) edit the config behind backup+validate+restore rails.

These tests run the script for real against a temporary HOME so nothing
touches the user's actual config or registry. They cover:
  * existence, executable, bash -n syntax
  * add registers a deployment (both wrapped and inner-object paste forms)
  * list reflects the registry and active state
  * use (print mode) emits the block without editing anything
  * use --write swaps the active BRIER server, preserves non-BRIER servers,
    leaves valid JSON, and makes a backup
  * the safety invariants (no sshpass, no secret handling) and no dashes

Run:
  cd mcp/
  uv run tests/test_v111_switch.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
SWITCH = ROOT / "brier-switch.sh"


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _run(args, home, stdin_text=None):
    env = dict(os.environ)
    env["HOME"] = home
    return subprocess.run(
        ["bash", str(SWITCH), *args],
        input=stdin_text, capture_output=True, text=True, env=env,
    )


def test_exists_executable_syntax():
    print("\n--- Test 1: brier-switch.sh exists, executable, valid syntax ---")
    ok = [
        _check("exists", SWITCH.is_file()),
        _check("executable", os.access(SWITCH, os.X_OK)),
    ]
    if SWITCH.is_file():
        res = subprocess.run(["bash", "-n", str(SWITCH)],
                             capture_output=True, text=True)
        ok.append(_check("bash -n syntax", res.returncode == 0,
                         detail=res.stderr.strip()[:200]))
    return all(ok)


def test_add_and_list():
    print("\n--- Test 2: add (both forms) + list ---")
    ok = []
    with tempfile.TemporaryDirectory() as home:
        # wrapped form
        wrapped = ('{"brier-remote-psoriasis": '
                   '{"command": "ssh", "args": ["-T", "psoriasis"]}}')
        r = _run(["add", "psoriasis"], home, stdin_text=wrapped)
        ok.append(_check("add wrapped form succeeds",
                         "Registered 'psoriasis'" in r.stdout,
                         detail=r.stdout.strip()[-80:] + r.stderr.strip()[-80:]))
        # inner-object form
        inner = ('{"command": "ssh", '
                 '"args": ["-T", "zrayw@cubio.umhs.med.umich.edu"]}')
        r = _run(["add", "cubio"], home, stdin_text=inner)
        ok.append(_check("add inner-object form succeeds",
                         "Registered 'cubio'" in r.stdout,
                         detail=r.stdout.strip()[-80:] + r.stderr.strip()[-80:]))
        # registry file has both
        stash = Path(home) / ".brier-mcp" / "servers.json"
        ok.append(_check("registry file written", stash.is_file()))
        if stash.is_file():
            data = json.loads(stash.read_text())
            ok.append(_check("both deployments registered",
                             "psoriasis" in data and "cubio" in data))
        # list shows them
        r = _run(["list"], home)
        ok.append(_check("list shows psoriasis and cubio",
                         "psoriasis" in r.stdout and "cubio" in r.stdout))
    return all(ok)


def test_use_print_mode_does_not_edit():
    print("\n--- Test 3: use (print mode) prints block, edits nothing ---")
    ok = []
    with tempfile.TemporaryDirectory() as home:
        _run(["add", "psoriasis"], home,
             stdin_text='{"brier-remote-psoriasis": '
                        '{"command": "ssh", "args": ["-T", "psoriasis"]}}')
        # set up a config we can check is untouched
        cfgdir = Path(home) / ".config" / "Claude"
        cfgdir.mkdir(parents=True)
        cfg = cfgdir / "claude_desktop_config.json"
        original = {"mcpServers": {"other": {"command": "x"}}}
        cfg.write_text(json.dumps(original))
        r = _run(["use", "psoriasis"], home)
        ok.append(_check("print mode emits the server key",
                         "brier-remote-psoriasis" in r.stdout))
        ok.append(_check("print mode did NOT edit the config",
                         json.loads(cfg.read_text()) == original))
    return all(ok)


def test_use_write_swaps_and_preserves():
    print("\n--- Test 4: use --write swaps BRIER, preserves others, valid JSON ---")
    ok = []
    with tempfile.TemporaryDirectory() as home:
        _run(["add", "psoriasis"], home,
             stdin_text='{"brier-remote-psoriasis": '
                        '{"command": "ssh", "args": ["-T", "psoriasis"]}}')
        cfgdir = Path(home) / ".config" / "Claude"
        cfgdir.mkdir(parents=True)
        cfg = cfgdir / "claude_desktop_config.json"
        cfg.write_text(json.dumps({
            "mcpServers": {
                "brier-local": {"command": "uv"},
                "some-other-tool": {"command": "node"},
            },
            "preferences": {"sidebarMode": "chat"},
        }))
        r = _run(["use", "psoriasis", "--write"], home)
        ok.append(_check("write reported success",
                         "now the active BRIER deployment" in r.stdout,
                         detail=r.stdout.strip()[-80:] + r.stderr.strip()[-80:]))
        data = json.loads(cfg.read_text())  # must be valid JSON
        servers = data.get("mcpServers", {})
        ok.append(_check("old brier-local removed",
                         "brier-local" not in servers))
        ok.append(_check("new brier-remote-psoriasis added",
                         "brier-remote-psoriasis" in servers))
        ok.append(_check("non-BRIER server preserved",
                         "some-other-tool" in servers))
        ok.append(_check("preferences preserved",
                         data.get("preferences", {}).get("sidebarMode") == "chat"))
        # a backup was made
        backups = list(cfgdir.glob("claude_desktop_config.json.bak.*"))
        ok.append(_check("backup created", len(backups) >= 1))
    return all(ok)


def test_unknown_name_errors_cleanly():
    print("\n--- Test 5: use of unknown name errors, no traceback ---")
    ok = []
    with tempfile.TemporaryDirectory() as home:
        r = _run(["use", "nonexistent"], home)
        ok.append(_check("nonzero exit", r.returncode != 0))
        ok.append(_check("mentions not in registry",
                         "not in registry" in (r.stdout + r.stderr)))
        ok.append(_check("no python traceback",
                         "Traceback" not in (r.stdout + r.stderr)))
    return all(ok)


def test_safety_and_style():
    print("\n--- Test 6: safety invariants + no dashes ---")
    text = SWITCH.read_text(encoding="utf-8")
    ok = [
        _check("no sshpass", "sshpass" not in text),
        _check("default is print, not write (has --write opt-in)",
               "--write" in text),
        _check("backs up before editing", ".bak." in text),
        _check("validates JSON after edit", "json.load" in text),
        _check("no em-dash", "\u2014" not in text),
        _check("no en-dash", "\u2013" not in text),
    ]
    return all(ok)


def main():
    print("BRIER MCP v1.1.1 switcher test suite")
    all_pass = True
    all_pass &= test_exists_executable_syntax()
    all_pass &= test_add_and_list()
    all_pass &= test_use_print_mode_does_not_edit()
    all_pass &= test_use_write_swaps_and_preserves()
    all_pass &= test_unknown_name_errors_cleanly()
    all_pass &= test_safety_and_style()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
