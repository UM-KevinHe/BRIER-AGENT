"""Tests for v1.1.0 - the --selfcheck flag and remote-launch support.

NOTE on the filename: the older test_v110.py covers v0.11
(brier_auto_tune_eta) from the pre-1.0 numbering scheme. To avoid a
collision, the v1.1.0 selfcheck tests live here in
test_v110_selfcheck.py.

v1.1.0 is a config-and-docs release. The only code change is the
addition of a --selfcheck entry point (and a --version flag) to
server.py, plus an __version__ constant. This suite pins:

  * __version__ exists and matches manifest.json / pyproject.toml
  * _selfcheck() returns the expected shape and never throws
  * _selfcheck() reports status "ok" when R + BRIER + cache are healthy
  * _selfcheck() reports status "error" with a failure list when the
    cache dir is not writable (the one failure we can reliably induce
    in a test without breaking the R install)

Run:
  cd mcp/
  uv run tests/test_v110_selfcheck.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def test_version_constant_exists():
    print("\n--- Test 1: __version__ constant present ---")
    ok = []
    ok.append(_check("server.__version__ exists",
                      hasattr(server, "__version__")))
    ok.append(_check("__version__ is a non-empty string",
                      isinstance(server.__version__, str)
                      and len(server.__version__) > 0,
                      detail=getattr(server, "__version__", "<missing>")))
    return all(ok)


def test_version_matches_manifest_and_pyproject():
    print("\n--- Test 2: __version__ matches manifest.json + pyproject ---")
    manifest = json.loads((HERE.parent / "manifest.json").read_text())
    pyproject = (HERE.parent / "pyproject.toml").read_text()
    pyproj_version = None
    for line in pyproject.splitlines():
        s = line.strip()
        if s.startswith("version"):
            pyproj_version = s.split("=", 1)[1].strip().strip('"')
            break
    ok = []
    ok.append(_check(
        f"manifest.json version == __version__ ({server.__version__})",
        manifest.get("version") == server.__version__,
        detail=f"manifest={manifest.get('version')}"))
    ok.append(_check(
        f"pyproject.toml version == __version__ ({server.__version__})",
        pyproj_version == server.__version__,
        detail=f"pyproject={pyproj_version}"))
    return all(ok)


def test_selfcheck_shape():
    print("\n--- Test 3: _selfcheck() returns the expected keys ---")
    rep = server._selfcheck()
    required = [
        "brier_mcp_version", "python", "platform", "cwd",
        "rscript", "rscript_found", "r_version",
        "brier_package_installed", "cache_dir", "cache_dir_writable",
        "status",
    ]
    ok = []
    for k in required:
        ok.append(_check(f"has key '{k}'", k in rep))
    ok.append(_check("status is 'ok' or 'error'",
                      rep.get("status") in ("ok", "error"),
                      detail=str(rep.get("status"))))
    ok.append(_check("version field matches __version__",
                      rep.get("brier_mcp_version") == server.__version__))
    return all(ok)


def test_selfcheck_ok_when_healthy():
    print("\n--- Test 4: _selfcheck() reports ok in a healthy env ---")
    rep = server._selfcheck()
    if rep.get("rscript_found") and rep.get("brier_package_installed"):
        import re
        ver = rep.get("brier_package_version", "")
        ok = [
            _check("rscript_found is True", rep["rscript_found"] is True),
            _check("brier_package_installed is True",
                   rep["brier_package_installed"] is True),
            _check("cache_dir_writable is True",
                   rep["cache_dir_writable"] is True),
            _check("status is 'ok'", rep["status"] == "ok"),
            _check("r_version is populated", bool(rep.get("r_version"))),
            # Hardened: the version field must be a REAL version, not an
            # error blob (the cubio GLIBCXX bug put an error string here).
            _check("brier_package_version is a real version (digits/dots)",
                   bool(re.fullmatch(r"\d+(\.\d+){1,3}", ver.strip())),
                   detail=repr(ver)),
        ]
        return all(ok)
    else:
        print("    [INFO] R or BRIER not present here; skipping healthy-env "
              f"assertions. selfcheck reported status={rep.get('status')}.")
        return _check("status is 'error' when unhealthy",
                      rep.get("status") == "error")


def test_version_field_never_holds_error_blob():
    print("\n--- Test 4b: a load-failure must not be reported as ok ---")
    # Regression guard for the cubio false-ok: when BRIER is reported
    # installed, the version field must look like a version. If selfcheck
    # ever sets installed=True with a non-version string, that's the bug.
    rep = server._selfcheck()
    import re
    if rep.get("brier_package_installed"):
        ver = rep.get("brier_package_version", "")
        return _check(
            "installed=True implies a valid version string",
            bool(re.fullmatch(r"\d+(\.\d+){1,3}", ver.strip())),
            detail=repr(ver))
    else:
        # installed=False is fine; just confirm status reflects it.
        return _check("installed=False implies status error",
                      rep.get("status") == "error")


def test_selfcheck_error_when_cache_unwritable():
    print("\n--- Test 5: _selfcheck() flags an unwritable cache dir ---")
    saved = os.environ.get("XDG_CACHE_HOME")
    try:
        os.environ["XDG_CACHE_HOME"] = "/proc/nonexistent_brier_selfcheck"
        rep = server._selfcheck()
        ok = []
        ok.append(_check("cache_dir_writable is False",
                          rep.get("cache_dir_writable") is False))
        ok.append(_check("status is 'error'",
                          rep.get("status") == "error"))
        ok.append(_check("failures lists cache_dir_not_writable",
                          "cache_dir_not_writable"
                          in rep.get("failures", [])))
        ok.append(_check("cache_dir_error is populated",
                          bool(rep.get("cache_dir_error"))))
        return all(ok)
    finally:
        if saved is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = saved


def test_selfcheck_never_throws():
    print("\n--- Test 6: _selfcheck() never raises, even with bad env ---")
    saved_rscript = os.environ.get("BRIER_RSCRIPT")
    ok = []
    try:
        os.environ["BRIER_RSCRIPT"] = "/definitely/not/a/real/Rscript"
        try:
            rep = server._selfcheck()
            ok.append(_check("returned a dict", isinstance(rep, dict)))
            ok.append(_check("has a status", "status" in rep))
        except Exception as e:  # noqa: BLE001
            ok.append(_check("did not raise", False,
                              detail=f"raised {type(e).__name__}: {e}"))
    finally:
        if saved_rscript is None:
            os.environ.pop("BRIER_RSCRIPT", None)
        else:
            os.environ["BRIER_RSCRIPT"] = saved_rscript
    return all(ok)


def main():
    print("BRIER MCP v1.1.0 selfcheck + remote-launch test suite")
    all_pass = True
    all_pass &= test_version_constant_exists()
    all_pass &= test_version_matches_manifest_and_pyproject()
    all_pass &= test_selfcheck_shape()
    all_pass &= test_selfcheck_ok_when_healthy()
    all_pass &= test_version_field_never_holds_error_blob()
    all_pass &= test_selfcheck_error_when_cache_unwritable()
    all_pass &= test_selfcheck_never_throws()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
