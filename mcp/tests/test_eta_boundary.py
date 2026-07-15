"""The boundary-optimum diagnostic, with MORE THAN ONE external model.

With M > 1 and multi_method="ind", eta is a VECTOR: one transfer strength per source,
each on its OWN axis (two sources and 7 rungs is a 7 x 7 = 49 point grid). That raises
the question these tests answer: if ONE component pins at its ceiling and the others do
not, is the fit truncated?

YES. ANY, not ALL. Each eta_k is a separate transfer strength, so a pinned eta_1 means
the optimum for source 1 lies outside the grid, and in a joint penalized fit EVERY
coefficient is estimated conditional on that truncated value. Requiring ALL to pin would
silently pass exactly the interesting case: one source wanting unbounded transfer while
another wants a little.

And the comparison has to be PER AXIS. The selection scripts used to `unlist` every
eta_k column into one flat pool, so each component was compared against the largest eta
anywhere in the grid. When the per-source grids differ (BRIER accepts an `eta.list` of M
vectors), a source pinning at the top of its own, SHORTER axis is invisible against the
global maximum -- a false negative in the one check that exists to prevent a truncated
model being reported as converged.

server.py cannot be imported (its module name collides with the MCP SDK's `mcp`
package), so the functions are sliced out of the source, as the loop tests do.

Run:  python3 mcp/tests/test_eta_boundary.py
"""
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1] / "server.py"
_src = _SERVER.read_text()


def _slice(start_marker: str, ns: dict):
    i = _src.index(start_marker)
    j = _src.index("\ndef ", i + 1)
    exec(_src[i:j], ns)


_ns: dict = {}
_slice("def _eta_grid_max(", _ns)
_slice("def _eta_axes(", _ns)
_slice("def _boundary_optimum_notice(", _ns)
_eta_axes = _ns["_eta_axes"]
_notice = _ns["_boundary_optimum_notice"]


# --------------------------------------------------------------- M = 1 (scalar)
def test_single_external_pinned_and_interior():
    grid = [0, 0.1, 1, 10]
    assert _notice(10.0, grid) is not None, "eta at the grid top must fire"
    assert _notice(1.0, grid) is None, "an interior optimum must not fire"
    print("boundary M=1: pinned fires, interior does not: OK")


def test_the_suggested_ceiling_is_5x_the_top():
    out = _notice(10.0, [0, 0.1, 1, 10])
    assert "50" in out, out
    print("boundary: suggests a 5x ceiling: OK")


# ------------------------------------------------------- M > 1 (vector eta, ind)
# Per-source axes, as the selection scripts now emit them: one grid per external.
_AXES_EQUAL = [[0, 0.1, 1, 10], [0, 0.1, 1, 10]]


def test_ANY_component_pinned_fires_not_only_ALL():
    """THE question. Source 1 pins, source 2 is interior: the fit IS truncated."""
    out = _notice([10.0, 1.0], _AXES_EQUAL)
    assert out is not None, "one pinned source must fire (ANY, not ALL)"
    assert "external 1" in out, out
    assert "external 2" not in out, "only the pinned source should be named"
    print("boundary M=2: ONE pinned component fires (ANY, not ALL): OK")


def test_all_components_pinned_names_both():
    out = _notice([10.0, 10.0], _AXES_EQUAL)
    assert "external 1" in out and "external 2" in out, out
    print("boundary M=2: both pinned, both named: OK")


def test_no_component_pinned_does_not_fire():
    assert _notice([1.0, 0.1], _AXES_EQUAL) is None
    print("boundary M=2: all interior -> no notice: OK")


def test_a_source_pinned_on_its_OWN_shorter_axis_is_caught():
    """The false negative the per-axis rewrite fixes.

    Source 1's axis runs to 10000; source 2's only to 10. Source 2 selects 10 -- the top
    of ITS grid, so its transfer is truncated -- while source 1 sits interior at 1.
    Compared against the GLOBAL maximum (10000, which lives on source 1's axis) this
    looks like a comfortable interior optimum, and the old flattened check said nothing.
    """
    axes = [[0, 1, 100, 10000], [0, 0.1, 1, 10]]
    out = _notice([1.0, 10.0], axes)
    assert out is not None, (
        "a source pinned at the top of its own axis was missed against the global max")
    assert "external 2" in out, out
    assert "50" in out, "the suggestion must widen from the PINNED axis (10 -> 50)"
    print("boundary M=2: a pin on a shorter axis is caught (not hidden by the "
          "global max): OK")


def test_axes_are_only_read_when_the_shape_matches():
    """A flat grid must not be misread as per-source axes, or M=1 breaks."""
    assert _eta_axes([0, 0.1, 1, 10], 1) is None
    assert _eta_axes([[0, 1], [0, 10]], 2) == [[0.0, 1.0], [0.0, 10.0]]
    # Shape mismatch (3 components, 2 axes) -> fall back to the flat comparison.
    assert _eta_axes([[0, 1], [0, 10]], 3) is None
    print("boundary: per-source axes read only when the shape matches: OK")


def test_a_flat_grid_still_works_for_a_vector_eta():
    """Back-compat: an OLD selection result carries the flattened pool, and a cached fit
    or a stored trace can still hand us one. It must not crash or silently pass."""
    assert _notice([10.0, 1.0], [0, 0.1, 1, 10]) is not None
    assert _notice([1.0, 0.1], [0, 0.1, 1, 10]) is None
    print("boundary: an old flattened grid still fires correctly: OK")


def main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("\n" + ("ETA-BOUNDARY: ALL TESTS PASS" if not fails
                  else f"ETA-BOUNDARY: {fails} FAILED"))
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
