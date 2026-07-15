"""Minimal MCP server mimicking a BRIER tool, for client testing."""
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-brier")

@mcp.tool()
def inspect_data(data_path: str) -> str:
    """Describe the structure of a local R data file."""
    return json.dumps({
        "status": "ok",
        "data_path": data_path,
        "n_variants": 96674,
        "n_samples": 1032,
        "has_phenotype": False,
    })

@mcp.tool()
def brier_s(target_sumstats: str, eta_list: list = None) -> str:
    """Fit BRIERs with a summary-statistics target."""
    return json.dumps({"status": "ok", "tool": "brier_s",
                       "eta_list": eta_list or [0]})

if __name__ == "__main__":
    mcp.run()
