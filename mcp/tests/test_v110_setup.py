"""Tests for v1.1.0 - the install automation scripts.

v1.1.0 adds two shell scripts:
  * setup.sh      - server-side: install uv, uv sync, verify env via
                    --selfcheck, print a ready-to-paste config block
                    (brier-local by default, brier-remote with --remote HOST)
  * setup-ssh.sh  - laptop-side: check passwordless SSH to a host, and if
                    it doesn't work, guide the human through key setup
                    (never typing secrets)

These are shell scripts, so the tests check structural properties rather
than executing the full install (which would need uv/R/SSH side effects):
  * both scripts exist and are executable
  * both pass `bash -n` (syntax check)
  * setup.sh references the key pieces (uv, uv sync, --selfcheck, brier-local,
    brier-remote, --remote)
  * setup-ssh.sh references the key pieces (BatchMode, ssh-copy-id, chmod,
    ssh-keygen) and never embeds a password/passphrase literal
  * neither script contains em/en dashes (project style rule)

Run:
  cd mcp/
  uv run tests/test_v110_setup.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _bash_syntax_ok(path: Path) -> bool:
    try:
        r = subprocess.run(["bash", "-n", str(path)],
                            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def test_scripts_exist_and_executable():
    print("\n--- Test 1: setup scripts exist and are executable ---")
    setup = ROOT / "setup.sh"
    setup_ssh = ROOT / "setup-ssh.sh"
    install = ROOT / "install.sh"
    ok = []
    ok.append(_check("setup.sh exists", setup.is_file()))
    ok.append(_check("setup-ssh.sh exists", setup_ssh.is_file()))
    ok.append(_check("install.sh exists", install.is_file()))
    ok.append(_check("setup.sh is executable",
                      os.access(setup, os.X_OK)))
    ok.append(_check("setup-ssh.sh is executable",
                      os.access(setup_ssh, os.X_OK)))
    ok.append(_check("install.sh is executable",
                      os.access(install, os.X_OK)))
    return all(ok)


def test_scripts_syntax():
    print("\n--- Test 2: all scripts pass bash -n ---")
    ok = []
    ok.append(_check("setup.sh syntax", _bash_syntax_ok(ROOT / "setup.sh")))
    ok.append(_check("setup-ssh.sh syntax",
                      _bash_syntax_ok(ROOT / "setup-ssh.sh")))
    ok.append(_check("install.sh syntax",
                      _bash_syntax_ok(ROOT / "install.sh")))
    return all(ok)


def test_install_content():
    print("\n--- Test 6: install.sh references the expected pieces ---")
    text = (ROOT / "install.sh").read_text()
    ok = [
        _check("clones the BRIER-MCP repo",
               "git clone" in text and "BRIER-MCP.git" in text),
        _check("hands off to setup.sh", "setup.sh" in text),
        _check("supports --update", "--update" in text),
        _check("passes through --remote", "--remote" in text),
        _check("refuses to clobber a non-checkout",
               "Refusing to overwrite" in text),
        _check("defaults target to current dir",
               "pwd" in text),
    ]
    return all(ok)


def test_setup_content():
    print("\n--- Test 3: setup.sh references the expected pieces ---")
    t = (ROOT / "setup.sh").read_text()
    ok = []
    ok.append(_check("installs/locates uv", "astral.sh/uv/install.sh" in t
                      and "find_uv" in t))
    ok.append(_check("runs uv sync", "uv sync" in t or '"$UV" sync' in t
                      or "$UV sync" in t))
    ok.append(_check("verifies via --selfcheck", "--selfcheck" in t))
    ok.append(_check("prints brier-local block", "brier-local" in t))
    ok.append(_check("prints brier-remote block (--remote)",
                      "brier-remote" in t and "--remote" in t))
    ok.append(_check("references the BRIER R package install command",
                      "remotes::install_github" in t))
    ok.append(_check("auto-install is opt-in via --install-brier",
                      "--install-brier" in t))
    ok.append(_check("does NOT auto-install by default (off unless flagged)",
                      "does NOT install" in t or "not install it by default" in t
                      or "By default this script does NOT install" in t))
    ok.append(_check("auto-install re-verifies the package actually loads",
                      "selfcheck" in t and ("Re-verifying" in t
                      or "actually LOAD" in t or "does it actually LOAD" in t)))
    return all(ok)


def test_setup_ssh_content():
    print("\n--- Test 4: setup-ssh.sh guides without typing secrets ---")
    t = (ROOT / "setup-ssh.sh").read_text()
    ok = []
    ok.append(_check("checks passwordless SSH (BatchMode)",
                      "BatchMode=yes" in t))
    ok.append(_check("guides ssh-copy-id", "ssh-copy-id" in t))
    ok.append(_check("guides key generation", "ssh-keygen" in t))
    ok.append(_check("guides permission fix", "chmod go-w" in t))
    ok.append(_check("explicitly states it won't type secrets",
                      "cannot type your password" in t.lower()
                      or "can't and shouldn't" in t.lower()
                      or "never type" in t.lower()
                      or "never stores or transmits secrets" in t.lower()))
    # Guard against accidentally embedding a credential: no sshpass, no
    # hardcoded password flags.
    ok.append(_check("no sshpass / password automation",
                      "sshpass" not in t))
    return all(ok)


def test_no_dashes_in_scripts():
    print("\n--- Test 5: scripts contain no em/en dashes (style rule) ---")
    ok = []
    for name in ("setup.sh", "setup-ssh.sh", "install.sh"):
        t = (ROOT / name).read_text(encoding="utf-8")
        has = ("\u2014" in t) or ("\u2013" in t)
        ok.append(_check(f"{name} dash-free", not has))
    return all(ok)


def main():
    print("BRIER MCP v1.1.0 install-script test suite")
    all_pass = True
    all_pass &= test_scripts_exist_and_executable()
    all_pass &= test_scripts_syntax()
    all_pass &= test_setup_content()
    all_pass &= test_setup_ssh_content()
    all_pass &= test_no_dashes_in_scripts()
    all_pass &= test_install_content()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
