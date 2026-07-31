"""CLI for the cross-plane coverage audit (Display ↔ ChannelFinder ↔ Archiver ↔ Alarm).

Reads a project/dataset ROOT of ``.bob`` displays, joins the macro-expanded display-PV index
(``opi_navigation`` Wedge-0) with the runtime planes: ChannelFinder (delivered PVs), Archiver,
Phoebus Alarm, and writes a Markdown coverage report to stdout. Each runtime plane is queried only
with its flag AND its ``*_URL`` set; without any, only the raw display set is shown.

Usage::

    python -m epics_mcp.cli_coverage --displays <project-root> --scope <prefix> \\
        [--channelfinder] [--archiver] [--alarm] [--context-cap N] [--windows-paths]
"""

from __future__ import annotations

import sys
from pathlib import Path

from epics_mcp.cli_common import (
    DisplayEngineAwareParser,
    add_version_argument,
    configure_stdout,
    require_display_engine,
)
from epics_mcp.errors import EpicsError

# The opi_navigation-backed imports live INSIDE main(), below the availability check.
# At module level they make importing this module fail with a ModuleNotFoundError
# traceback wherever the engine is absent, which is every install from a package index.


def main(argv: list[str] | None = None) -> int:
    """Run the coverage audit and print a Markdown report. Returns an exit code."""
    # BEFORE the parser is built (QA-8): argparse prints ``--help`` inside ``parse_args``, and the
    # parser description carries U+2194, which a cp1252 console cannot encode. With the reconfigure
    # after parsing, ``--help`` died with a bare UnicodeEncodeError on any non-UTF-8 stdout.
    configure_stdout()

    parser = DisplayEngineAwareParser(
        # prog pinned: argparse's default is interpreter dependent and prints an absolute console
        # script path on 3.14 (QA-41). Same reason at every entry point of this package.
        prog="epics-coverage",
        description="Cross-plane coverage audit: Display ↔ ChannelFinder ↔ Archiver ↔ Alarm",
    )
    # The parser is built BEFORE the engine check and must therefore stay engine-free, so that
    # --help and --version answer on an install that has no engine at all (QA-42). --context-cap is
    # the one option that used to break that, and it now defaults to None; a usage error still gets
    # the engine refusal, through DisplayEngineAwareParser.
    add_version_argument(parser)
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
        # No number here, and no engine import to produce one: the default lives in the display
        # engine, and printing it would put the parser back behind the engine it must not need.
        default=None,
        help="max per-display reachability contexts the PV-inventory explores "
        "(default: the value the display engine defines; higher = more complete, slower)",
    )
    parser.add_argument(
        "--windows-paths",
        action="store_true",
        help="resolve embedded <file> refs case-insensitively (Windows host); default Linux",
    )
    args = parser.parse_args(argv)

    unavailable = require_display_engine("epics-coverage")
    if unavailable is not None:
        return unavailable

    from epics_mcp.services.coverage import render_markdown
    from epics_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
    from epics_mcp.services.orchestration import CoverageRequest, build_coverage_report

    # ``is None`` rather than ``or``: an explicit ``--context-cap 0`` is a caller's decision and has
    # always been passed through untouched, while ``or`` would silently replace it with the engine
    # default. The flag carries no argparse default any more, so this is where the engine's value
    # enters, and it is the only place that reads the constant.
    context_cap = DEFAULT_PV_CONTEXT_CAP if args.context_cap is None else args.context_cap

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
        context_cap=context_cap,
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
