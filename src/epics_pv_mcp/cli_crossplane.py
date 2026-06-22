"""CLI for the cross-plane PV provenance check (Display ↔ e3 IOC ↔ Naming).

Reads a project/dataset ROOT of ``.bob`` displays and an e3 ``st.cmd`` (both local files), joins
the macro-expanded per-instance display PVs (``opi_navigation`` Wedge-0 inventory) with the IOC
prefix, and writes a Markdown provenance report to stdout. The live ESS Naming Service is queried
only with ``--naming`` (a read-only GET); without it the check is fully offline.

Usage::

    python -m epics_pv_mcp.cli_crossplane --displays <project-root> --st-cmd <st.cmd> \\
        [--naming] [--context-cap N] [--windows-paths]
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from epics_pv_mcp.services.crossplane import crossplane_check, render_markdown
from epics_pv_mcp.services.e3_db import parse_st_cmd
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP, analyze_display_pvs
from epics_pv_mcp.services.naming_client import NamingServiceClient


def main(argv: list[str] | None = None) -> int:
    """Run the cross-plane check and print a Markdown report. Returns an exit code."""
    parser = argparse.ArgumentParser(description="Cross-plane PV provenance: Display ↔ e3 ↔ Naming")
    parser.add_argument(
        "--displays",
        required=True,
        type=Path,
        help="project/dataset ROOT of .bob displays (not a narrow per-IOC subdirectory — "
        "macros are bound by the operator top-levels there)",
    )
    parser.add_argument("--st-cmd", required=True, type=Path, help="e3 IOC st.cmd file")
    parser.add_argument(
        "--naming",
        action="store_true",
        help="query the live ESS Naming Service (read-only GET); omit to stay offline",
    )
    parser.add_argument(
        "--context-cap",
        type=int,
        default=DEFAULT_PV_CONTEXT_CAP,
        help="max per-display reachability contexts the PV-inventory explores "
        f"(default {DEFAULT_PV_CONTEXT_CAP}; higher = more complete, slower)",
    )
    parser.add_argument(
        "--windows-paths",
        action="store_true",
        help="resolve embedded <file> refs case-insensitively (Windows host); default Linux",
    )
    args = parser.parse_args(argv)

    # The report contains Unicode (emoji/en-dash); force UTF-8 so a cp1252 Windows
    # console doesn't crash on encode.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    if not Path(args.st_cmd).is_file():
        sys.stderr.write(f"Error: st.cmd file not found: {args.st_cmd}\n")
        return 2
    if not Path(args.displays).is_dir():
        sys.stderr.write(f"Error: displays directory not found: {args.displays}\n")
        return 2

    join_pvs, context_capped, glob_capped_count = analyze_display_pvs(
        Path(args.displays), context_cap=args.context_cap, windows_paths=args.windows_paths
    )
    st_info = parse_st_cmd(Path(args.st_cmd).read_text(encoding="utf-8"))
    naming = NamingServiceClient() if args.naming else None
    report = crossplane_check(
        join_pvs,
        st_info,
        naming=naming,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
    sys.stdout.write(render_markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
