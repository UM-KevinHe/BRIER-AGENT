"""Pure-logic tests for the environment checker (no R, no subprocess)."""
import os

from brier_agent import check_env as ce


def test_check_line_marks_by_state():
    # ok -> [ OK ]; missing required -> [MISS] with a hint; missing optional -> [WARN].
    assert "[ OK ]" in ce.Check(True, True, "x").line()
    miss = ce.Check(False, True, "x", "do the thing").line()
    assert "[MISS]" in miss and "do the thing" in miss
    assert "[WARN]" in ce.Check(False, False, "x", "hint").line()
    # A passing check never prints its hint (the hint is a fix, shown only on failure).
    assert "do the thing" not in ce.Check(True, True, "x", "do the thing").line()


def test_default_mcp_server_points_at_bundled_file():
    p = ce._default_mcp_server()
    assert p.endswith(os.path.join("mcp", "server.py"))
    assert os.path.isfile(p), "the bundled server should sit at <repo>/mcp/server.py"


def test_rscript_path_prefers_env(monkeypatch=None):
    # BRIER_RSCRIPT wins when it resolves; a bogus one yields None (not a silent PATH fall
    # -through, which would hide a misconfigured override).
    old = os.environ.get("BRIER_RSCRIPT")
    try:
        os.environ["BRIER_RSCRIPT"] = "/definitely/not/a/real/Rscript"
        assert ce._rscript_path() is None
    finally:
        if old is None:
            os.environ.pop("BRIER_RSCRIPT", None)
        else:
            os.environ["BRIER_RSCRIPT"] = old


def test_status_counts_required_and_optional():
    # status() tallies missing required vs missing optional across groups, no R needed.
    groups = [("G", [ce.Check(True, True, "ok"),
                     ce.Check(False, True, "missing-required"),
                     ce.Check(False, False, "missing-optional")])]
    assert ce.status(groups) == (1, 1)


def test_format_renders_lines_and_a_state_appropriate_summary():
    # All present -> the summary is the OK line.
    txt = ce._format([("Python", [ce.Check(True, True, "ok")])])
    assert "BRIER-Agent environment check" in txt
    assert "Python" in txt and "[ OK ]" in txt
    assert "OK: every check passed." in txt
    # A missing REQUIRED item flips the summary to FAIL.
    assert "FAIL:" in ce._format([("Python", [ce.Check(False, True, "x", "install it")])])
    # Only optional missing -> still OK, but the caveat line, not FAIL.
    opt = ce._format([("Python", [ce.Check(False, False, "x", "install it")])])
    assert "FAIL:" not in opt and "optional/recommended" in opt


def test_install_recommended_without_rscript_is_a_clear_message():
    # No R available -> a clear message, not a crash (the no-Rscript branch, no R needed).
    old = os.environ.get("BRIER_RSCRIPT")
    try:
        os.environ["BRIER_RSCRIPT"] = "/definitely/not/a/real/Rscript"
        msg = ce.install_recommended()
        assert "Rscript not found" in msg
    finally:
        if old is None:
            os.environ.pop("BRIER_RSCRIPT", None)
        else:
            os.environ["BRIER_RSCRIPT"] = old


def test_required_r_packages_are_the_hard_loads():
    # Guard against drift: these are the packages the R scripts library() unconditionally
    # (plus survival, which ships with BRIER). If a tool starts hard-loading another, add
    # it here so the checker fails an image that lacks it.
    assert set(ce._R_REQUIRED) == {"BRIER", "Matrix", "jsonlite", "survival"}
