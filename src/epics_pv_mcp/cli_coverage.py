"""CLI for the cross-plane coverage audit (Display ↔ ChannelFinder ↔ Archiver ↔ Alarm).

Reads a project/dataset ROOT of ``.bob`` displays, joins the macro-expanded display-PV index
(``opi_navigation`` Wedge-0) with the runtime planes: ChannelFinder (delivered PVs), Archiver,
Phoebus Alarm, and writes a Markdown coverage report to stdout. Each runtime plane is queried only
with its flag AND its ``*_URL`` set; without any, only the raw display set is shown.

Usage::

    python -m epics_pv_mcp.cli_coverage --displays <project-root> --scope <prefix> \\
        [--channelfinder] [--archiver] [--alarm] [--context-cap N] [--windows-paths]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from epics_pv_mcp.cli_common import configure_stdout, require_display_engine
from epics_pv_mcp.errors import EpicsError

# The opi_navigation-backed imports live INSIDE main(), below the availability check.
# At module level they make importing this module fail with a ModuleNotFoundError
# traceback wherever the engine is absent, which is every install from a package index.


def main(argv: list[str] | None = None) -> int:
    """Run the coverage audit and print a Markdown report. Returns an exit code."""
    unavailable = require_display_engine("epics-coverage")
    if unavailable is not None:
        return unavailable

    from epics_pv_mcp.services.coverage import render_markdown
    from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
    from epics_pv_mcp.services.orchestration import CoverageRequest, build_coverage_report

    parser = argparse.ArgumentParser(
        description="Cross-plane coverage audit: Display ↔ ChannelFinder ↔ Archiver ↔ Alarm"
    )
    parser.add_argument(
        "--displays",
        required=True,
        type=Path,
        help="project/dataset ROOT of .bob displays (not a narrow per-IOC subdirectory, "
        "macros are bound by the operator top-levels there)",
    )
    parser.add_argument(
        "--scope",
        default="",
        help="record-name prefix narrowing the ChannelFinder query AND the display set "
        "(e.g. DEV-TEST01:Ctrl-EVR-01:); '' = whole site (the CF query then hits the cap, "
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
        default=None,
        help="alarm config-tree name; REQUIRED with --alarm (URL set), no default (site-specific "
        "trees), so --alarm without it is a loud INVALID_INPUT, not a silent scan",
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

    configure_stdout()

    # One request → the SAME orchestrator the MCP tool calls (no duplicated join). Path validation
    # (canonicalize + existence + allowed_roots) happens inside build_coverage_report via
    # resolve_user_path, so the CLI honours the same boundary the tool had (S4-4), a bad path
    # raises EpicsError, which we map to the CLI's exit-2 contract.
    request = CoverageRequest(
        displays_dir=str(args.displays),
        scope=args.scope,
        query_channelfinder=args.channelfinder,
        query_archiver=args.archiver,
        query_alarm=args.alarm,
        alarm_config=args.alarm_config,
        context_cap=args.context_cap,
        windows_paths=args.windows_paths,
    )
    try:
        report = build_coverage_report(request)
    except EpicsError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    sys.stdout.write(render_markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
