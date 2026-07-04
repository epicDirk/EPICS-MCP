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

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.crossplane import render_markdown
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
from epics_pv_mcp.services.orchestration import CrossPlaneRequest, run_crossplane


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

    # One request → the SAME orchestrator the MCP tool calls (no duplicated join). Path validation
    # (canonicalize + existence + allowed_roots) happens inside run_crossplane via
    # resolve_user_path, so the CLI now honours the same boundary the tool had (S4-4) — a bad path
    # raises EpicsError, which we map to the CLI's exit-2 contract.
    request = CrossPlaneRequest(
        displays_dir=str(args.displays),
        st_cmd_path=str(args.st_cmd),
        query_naming=args.naming,
        query_channelfinder=args.channelfinder,
        context_cap=args.context_cap,
        windows_paths=args.windows_paths,
        module_db_root=args.module_db_root,
    )
    try:
        report = run_crossplane(request)
    except EpicsError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    sys.stdout.write(render_markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
