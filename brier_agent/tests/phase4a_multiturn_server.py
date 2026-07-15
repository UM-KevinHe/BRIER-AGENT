"""Fake BRIER server for the multi-turn test: inspect_data returns real
structure, brier_i/brier_s return a plausible fit result so the model has
something to report on. No R needed.
"""
import json
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fake-brier-multiturn")

@mcp.tool()
def inspect_data(data_path: str) -> str:
    """Describe the structure of a local R data file before fitting."""
    return json.dumps({
        "status": "ok", "data_path": data_path,
        "objects": {
            "geno": {"class": "matrix", "dim": [1000, 50000]},
            "pheno": {"class": "numeric", "length": 1000},
            "ext_beta": {"class": "numeric", "length": 50000},
        },
    })

@mcp.tool()
def brier_i(X_expr: str, y_expr: str, beta_external_expr: str,
            eta_list: list = None) -> str:
    """Fit BRIERi: individual-level target (X,y) + pretrained external coefficients."""
    return json.dumps({
        "status": "ok", "tool": "brier_i",
        "used": {"X": X_expr, "y": y_expr, "beta": beta_external_expr},
        "selected_eta": 0.464, "auc": 0.71, "n_variants": 50000,
        "output_dir": "/tmp/brier_out",
    })

@mcp.tool()
def brier_s(sumstats_expr: str, beta_external_expr: str, family: str = "gaussian") -> str:
    """Fit BRIERs: summary-statistics target + external coefficients."""
    return json.dumps({"status":"ok","tool":"brier_s","selected_eta":0.3,"auc":0.68})

if __name__ == "__main__":
    mcp.run()
