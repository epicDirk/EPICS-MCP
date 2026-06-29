"""CLI for the cross-plane coverage audit (Display ↔ ChannelFinder ↔ Archiver ↔ Alarm).

Reads a project/dataset ROOT of ``.bob`` displays, joins the macro-expanded display-PV index
(``opi_navigation`` Wedge-0) with the runtime planes — ChannelFinder (delivered PVs), Archiver,
Phoebus Alarm — and writes a Markdown coverage report to stdout. Each runtime plane is queried only
with its flag AND its ``*_URL`` set; without any, only the raw display set is shown.

Usage::

    python -m epics_pv_mcp.cli_coverage --displays <project-root> --scope <prefix> \\
        [--channelfinder] [--archiver] [--alarm] [--context-cap N] [--windows-paths]
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG
from epics_pv_mcp.services.coverage import audit_coverage, render_markdown
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP, analyze_display_index
from epics_pv_mcp.tools.coverage_audit import _build_alarm_checker, _build_archiver_checker
from epics_pv_mcp.tools.crossplane import _build_cf_checker


def main(argv: list[str] | None = None) -> int:
    """Run the coverage audit and print a Markdown report. Returns an exit code."""
    parser = argparse.ArgumentParser(
        description="Cross-plane coverage audit: Display ↔ ChannelFinder ↔ Archiver ↔ Alarm"
    )
    parser.add_argument(
        "--displays",
        required=True,
        type=Path,
        help="project/dataset ROOT of .bob displays (not a narrow per-IOC subdirectory — "
        "macros are bound by the operator top-levels there)",
    )
    parser.add_argument(
        "--scope",
        default="",
        help="record-name prefix narrowing the ChannelFinder query AND the display set "
        "(e.g. DEV-TEST01:Ctrl-EVR-01:); '' = whole site (the CF query then hits the cap — "
        "sandbox/small-scope only)",
    )
    parser.add_argument(
        "--channelfinder",
        action="store_true",
        help="query ChannelFinder for the delivered PVs (the coverage anchor); needs "
        "EPICS_MCP_CHANNELFINDER_URL. Without it only the raw display set is reported",
    )
    parser.add_argument(
        "--archiver",
        action="store_true",
        help="add the archive plane (per-PV is_archived); needs EPICS_MCP_ARCHIVER_URL "
        "(unset → 'archived' withheld + a note)",
    )
    parser.add_argument(
        "--alarm",
        action="store_true",
        help="add the alarm plane (per-PV is_alarm_configured); needs EPICS_MCP_ALARM_URL "
        "(unset → 'alarmed' withheld + a note)",
    )
    parser.add_argument(
        "--alarm-config",
        default=DEFAULT_ALARM_CONFIG,
        help=f"alarm config-tree name to query (default {DEFAULT_ALARM_CONFIG})",
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

    # The report contains Unicode (emoji/en-dash); force UTF-8 so a cp1252 console doesn't crash.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    if not Path(args.displays).is_dir():
        sys.stderr.write(f"Error: displays directory not found: {args.displays}\n")
        return 2

    index_rows, context_capped, glob_capped_count = analyze_display_index(
        Path(args.displays), context_cap=args.context_cap, windows_paths=args.windows_paths
    )
    channelfinder = _build_cf_checker(args.channelfinder)
    archived = _build_archiver_checker(args.archiver)
    alarmed = _build_alarm_checker(args.alarm, args.alarm_config)
    report = audit_coverage(
        index_rows,
        scope=args.scope,
        channelfinder=channelfinder,
        cf_requested=args.channelfinder,
        archived=archived,
        archive_requested=args.archiver,
        alarmed=alarmed,
        alarm_requested=args.alarm,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
    sys.stdout.write(render_markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
