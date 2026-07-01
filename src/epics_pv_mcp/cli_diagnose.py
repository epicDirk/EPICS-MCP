"""CLI for the live connection diagnosis (``epics-diagnose``).

Diagnoses WHY a PV is (dis)connected and prints a human-readable verdict. Read-only. Exit code is
**0 even when the PV is disconnected** — a disconnect is a normal diagnostic result, not a crash;
only a usage error returns non-zero. The live probe decides connected/disconnected; ChannelFinder
(default on, needs ``EPICS_MCP_CHANNELFINDER_URL``), Naming (``--naming``, needs
``EPICS_MCP_NAMING_URL`` — off by default = no ESS egress), Archiver (``--archiver``) and Alarm
(``--alarm``) only explain it.

Usage::

    epics-diagnose DEV-TEST01:Ctrl-EVR-01:12VValue [--naming] [--archiver] [--alarm] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys

from epics_pv_mcp.services.diagnose import DiagnoseReport, diagnose


def _render(report: DiagnoseReport) -> str:
    """Render a human-readable verdict (deterministic)."""
    lines = [
        f"PV:           {report.pv_name}",
        f"State:        {report.state}",
        f"Likely cause: {report.likely_cause}  (confidence: {report.confidence})",
    ]

    ev = report.evidence
    live = ev.live
    live_detail = (
        f"connected, value={live.value!r}"
        + (f", severity={live.severity}" if live.severity else "")
        if live.connected
        else f"disconnected ({live.error_code or 'internal error'})"
    )
    lines.append(f"  live:         {live_detail}")

    cf = ev.channelfinder
    if cf.consulted:
        prov = f", ioc={cf.ioc_name}" if cf.ioc_name else ""
        pvst = f", pvStatus={cf.pv_status}" if cf.pv_status else ""
        lines.append(f"  channelfinder: registered={cf.registered}{pvst}{prov}")
    if ev.naming.consulted:
        lines.append(f"  naming:        registered={ev.naming.registered} ({ev.naming.status})")
    if ev.archiver.consulted:
        lines.append(f"  archiver:      archived={ev.archiver.archived}")
    if ev.alarm.consulted:
        lines.append(f"  alarm:         configured={ev.alarm.configured}")

    if report.next_steps:
        lines.append("Next steps:")
        lines.extend(f"  - {step}" for step in report.next_steps)
    if report.notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in report.notes)
    if report.withheld:
        lines.append(f"Withheld planes (requested but unavailable): {', '.join(report.withheld)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Diagnose a PV connection and print the verdict. Returns 0 (incl. when disconnected)."""
    parser = argparse.ArgumentParser(
        description="Diagnose why an EPICS PV is (dis)connected (read-only)."
    )
    parser.add_argument("pv_name", help="the PV to diagnose")
    parser.add_argument(
        "--timeout", type=float, default=None, help="live-probe timeout (default: config, 5.0 s)"
    )
    parser.add_argument(
        "--no-channelfinder",
        action="store_true",
        help="skip the ChannelFinder plane (on by default)",
    )
    parser.add_argument(
        "--naming",
        action="store_true",
        help="query the ESS Naming Service (read-only GET; needs EPICS_MCP_NAMING_URL). Off by "
        "default = no ESS egress",
    )
    parser.add_argument("--archiver", action="store_true", help="corroborate with the Archiver")
    parser.add_argument("--alarm", action="store_true", help="corroborate with the Alarm tree")
    parser.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    args = parser.parse_args(argv)

    # The verdict contains Unicode (⇒/en-dash); force UTF-8 so a cp1252 console doesn't crash.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    report = asyncio.run(
        diagnose(
            args.pv_name,
            timeout=args.timeout,
            check_channelfinder=not args.no_channelfinder,
            check_naming=args.naming,
            check_archiver=args.archiver,
            check_alarm=args.alarm,
        )
    )

    if args.json:
        sys.stdout.write(json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
