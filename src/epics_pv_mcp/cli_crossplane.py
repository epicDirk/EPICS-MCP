"""CLI for the cross-plane PV provenance check (Display ↔ e3 IOC ↔ Naming).

Reads a project/dataset ROOT of ``.bob`` displays and an e3 ``st.cmd`` (both local files), joins
the macro-expanded per-instance display PVs (``opi_navigation`` Wedge-0 inventory) with the IOC
prefix, and writes a Markdown provenance report to stdout. The live ESS Naming Service is queried
only with ``--naming`` (a read-only GET); without it the check is fully offline.

Usage::

    python -m epics_pv_mcp.cli_crossplane --displays <project-root> --st-cmd <st.cmd> \\
        [--naming] [--channelfinder] [--context-cap N] [--windows-paths]
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from epics_pv_mcp.services.crossplane import crossplane_check, render_markdown
from epics_pv_mcp.services.e3_db import load_ioc_db, parse_st_cmd
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP, analyze_display_pvs
from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.tools.crossplane import _build_cf_checker


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
        "--channelfinder",
        action="store_true",
        help="check each concrete linked PV against ChannelFinder (read-only GET) and report "
        "those not registered as cf_unregistered; needs EPICS_MCP_CHANNELFINDER_URL (unset → "
        "honest 'skipped' note). Omit to stay offline",
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
    parser.add_argument(
        "--module-db-root",
        default="",
        help="opt-in: local directory of the IOC's e3 module .db files. When given, concrete "
        "linked PVs are checked against the loaded IOC .db set; a 'broken' verdict is emitted ONLY "
        "if that set is provably complete (else withheld). Omit (or empty) = prefix/Naming level.",
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
    if args.module_db_root and not Path(args.module_db_root).is_dir():
        sys.stderr.write(f"Error: module-db-root directory not found: {args.module_db_root}\n")
        return 2

    join_pvs, context_capped, glob_capped_count = analyze_display_pvs(
        Path(args.displays), context_cap=args.context_cap, windows_paths=args.windows_paths
    )
    st_info = parse_st_cmd(Path(args.st_cmd).read_text(encoding="utf-8"))
    naming = NamingServiceClient() if args.naming else None
    ioc_db: tuple[set[str], set[str]] | None = None
    ioc_db_complete = False
    if args.module_db_root:  # empty string = offline (mirror the MCP tool's truthiness sentinel)
        db_result = load_ioc_db(st_info, Path(args.module_db_root))
        ioc_db = (set(db_result.resolved), set(db_result.unresolved))
        ioc_db_complete = db_result.complete
    channelfinder = _build_cf_checker(args.channelfinder)
    report = crossplane_check(
        join_pvs,
        st_info,
        naming=naming,
        ioc_db=ioc_db,
        ioc_db_complete=ioc_db_complete,
        channelfinder=channelfinder,
        cf_requested=args.channelfinder,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
    sys.stdout.write(render_markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
