"""CLI for the read-only config self-check (``epics-doctor``).

Probes every CONFIGURED plane once and prints whether it is reachable, whether the CA bundle works,
whether the service answers, and what the ChannelFinder privacy redaction is set to — the
``flutter doctor`` of this server. Read-only and localhost-isolated by default (a disabled plane
makes no network call).

Exit code (a DELIBERATE convention, unlike the other CLIs where a finding is exit 0 — doctor is
a scriptable pass/fail):

* ``0`` — every configured plane is healthy (or honestly disabled / info-only);
* ``1`` — a configured plane failed (unreachable / ca_error / api_error / probe-disconnect);
* ``2`` — a usage error (bad arguments, or an internal EpicsError).

Usage::

    epics-doctor [--probe-pv DEV-TEST01:Ctrl-EVR-01:12VValue] [--timeout 5] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.doctor import DoctorReport, run_doctor

#: One glyph per status for the human-readable render (deterministic).
_STATUS_MARK = {
    "ok": "✓",
    "disabled": "·",
    "info": "i",
    "ca_error": "✗",
    "api_error": "✗",
    "unreachable": "✗",
    "disconnected": "✗",
}


def _render(report: DoctorReport) -> str:
    """Render a human-readable per-plane report (deterministic)."""
    lines = ["EPICS-MCP doctor — read-only per-plane config check", ""]
    for plane in report.planes:
        mark = _STATUS_MARK.get(plane.status, "?")
        lines.append(f"  {mark} {plane.plane:<14} {plane.status}")
        if plane.detail:
            lines.append(f"      {plane.detail}")
    lines.append("")
    lines.append("Privacy (ChannelFinder redaction):")
    owners = ", ".join(report.privacy.cf_safe_owner_accounts) or "(empty — all owners redacted)"
    props = ", ".join(report.privacy.cf_safe_property_names) or "(empty — all properties redacted)"
    lines.append(f"  owner allowlist:    {owners}")
    lines.append(f"  property allowlist: {props}")
    lines.append("  Olog free-text:     always withheld")
    lines.append("")
    verdict = (
        "OK — all configured planes healthy"
        if report.ok
        else "PROBLEM — a configured plane failed (see above)"
    )
    lines.append(f"Overall: {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the self-check, print the report. Returns 0 (healthy) / 1 (a plane failed) / 2."""
    parser = argparse.ArgumentParser(
        description="Read-only config self-check: is every configured EPICS plane reachable?"
    )
    parser.add_argument(
        "--probe-pv",
        default=None,
        help="probe a live PV to pass/fail the live plane (default: info only, no live read)",
    )
    parser.add_argument(
        "--timeout", type=float, default=None, help="per-plane timeout (default: config, 5.0 s)"
    )
    parser.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    args = parser.parse_args(argv)

    # The report contains Unicode (✓/✗/en-dash); force UTF-8 so a cp1252 console doesn't crash.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        report = asyncio.run(run_doctor(probe_pv=args.probe_pv, timeout=args.timeout))
    except EpicsError as exc:  # gatherers are total, so this is only a genuine internal error
        sys.stderr.write(f"doctor: {exc}\n")
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
