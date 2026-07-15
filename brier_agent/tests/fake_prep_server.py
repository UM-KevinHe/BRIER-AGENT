"""A fake MCP server exposing the prep + fit surface, for preprocessing-only tests.

fake_server.py only has inspect_data + brier_s, which is not enough to exercise
the prep -> fit continuation hook: that hook fires on a prep result carrying
{prepared_path, expr_hints}. This server returns that contract so a test can prove
the hook fires in the NORMAL mode and is SUPPRESSED in preprocessing-only mode.
"""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-prep")


def _pin_upto() -> float:
    """The eta ceiling below which a fit still pins at the grid's top rung."""
    try:
        return float(os.environ.get("FAKE_PIN_UPTO", "10"))
    except ValueError:
        return 10.0


def _ceiling_of(fit_id: str) -> float:
    """Recover a fit's eta ceiling from the id brier_i minted for it."""
    try:
        return float(fit_id.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 10.0


@mcp.tool()
def inspect_user_data(data_path: str) -> dict:
    """Inspect a user data file."""
    return {
        "status": "ok",
        "n_variants": 10000,
        "columns": ["varnames", "CHR", "BP", "REF", "ALT", "coef"],
    }


@mcp.tool()
def prep_auto(shape: str, data_dir: str = "", roles: dict = None,
              standardize: bool = False, ld_ancestry: str = "",
              ld_build: str = "") -> dict:
    """Assemble fit-ready inputs. Returns the prep output contract."""
    return {
        "status": "ok",
        "shape": shape,
        "prepared_path": f"/tmp/prep_auto_{shape}.rds",
        "expr_hints": {
            "X_expr": f"prep_auto_{shape}$X",
            "y_expr": f"prep_auto_{shape}$y",
            "beta_external_expr": f"prep_auto_{shape}$beta_external",
        },
        "report": [f"prepared for {shape}"],
    }


@mcp.tool()
def brier_i(X_expr: str = "", y_expr: str = "", beta_external_expr: str = "",
            eta_list: list = None, eta_ceiling: float = 10.0,
            data_path: str = "") -> dict:
    """Fit BRIERi. Present so a test can prove it is NOT offered in prep-only mode.

    Echoes the RESOLVED eta grid (`eta_list_used`), as the real tool does: the grid
    the fit ran on is what the boundary check and the scorer read, and the agent is
    told to omit `eta_list` entirely.
    """
    grid = eta_list or [0, 0.1, 1, eta_ceiling]
    return {"status": "ok", "fit_id": f"fit_{eta_ceiling:g}", "M_external": 1,
            "eta_list_used": grid}


@mcp.tool()
def brier_i_selection(fit_id: str = "", criteria: str = "", data_path: str = "",
                      X_val_expr: str = "", y_val_expr: str = "") -> dict:
    """Select eta/lambda.

    Emits the real tool's `_notice_eta_boundary` when the selected eta lands on the
    grid's top rung. How far the data's optimum lies is set by FAKE_PIN_UPTO (the
    ceiling below which a fit still pins), so a test can model both an optimum that
    a single widening reaches and one that lies past ANY sane grid (T1_brieri's
    external only starts to help far beyond the ceiling). Default 10: the initial
    grid pins, one widening resolves it.
    """
    ceiling = _ceiling_of(fit_id)
    out = {"status": "ok", "selection_id": f"sel_{fit_id}",
           "eta_grid_values": [0, 0.1, 1, ceiling]}
    if ceiling <= _pin_upto():
        out["selected_eta"] = ceiling
        out["_notice_eta_boundary"] = (
            f"Selected eta ({ceiling:g}) is at the top of the selection grid. The "
            "optimum may lie beyond it; consider refitting with a higher eta_ceiling."
        )
    else:
        out["selected_eta"] = ceiling / 2.0
    return out


@mcp.tool()
def brier_evaluate(selection_id: str = "", newx_expr: str = "",
                   newy_expr: str = "", criteria: str = "",
                   data_path: str = "") -> dict:
    """Score a fitted model on held-out data."""
    return {"status": "ok", "metric": criteria, "value": 0.0142}


if __name__ == "__main__":
    mcp.run()
