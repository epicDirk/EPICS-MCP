"""CLI for the read-only config self-check (``epics-doctor``).

Probes every CONFIGURED plane (a transport probe, refined on success by an identity probe — up to
two requests for a healthy plane; retries on a 5xx add more) and prints whether it is reachable,
whether the CA bundle works, whether the service **identifies itself as the one that URL is
supposed to point at**, and what the ChannelFinder privacy redaction is set to — the ``flutter
doctor`` of this server. Read-only — it probes, never writes — and it touches exactly the planes
that are CONFIGURED (a disabled plane makes no network call).

Exit code (a DELIBERATE convention, unlike the other CLIs where a finding is exit 0 — doctor is
a scriptable pass/fail):

* ``0`` — no configured plane failed (healthy, honestly disabled/info-only, or reachable with its
  identity ``unverified``);
* ``1`` — a configured plane failed (unreachable / ca_error / api_error / wrong_service /
  config_error / probe-disconnect);
* ``2`` — a usage error (bad arguments, or an internal EpicsError).

⚠️ Exit ``0`` means "nothing failed", NOT "everything was confirmed": a plane can be reachable with
its identity unverified and still exit 0 (that is honest, not healthy — see ``doctor.py``). A
machine reader must therefore look at ``verification_complete`` / ``unverified_planes`` in
``--json``, not only at the exit code — and for POSITIVE confirmation assert ``identified_planes``
is non-empty (``verification_complete`` is vacuously true on an empty config).

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

#: One glyph per status for the human-readable render (deterministic). ``unverified`` gets its own
#: mark rather than borrowing ✓ or ✗: it is neither "confirmed" nor "broken", and the whole point of
#: the state is that those two were being conflated.
_STATUS_MARK = {
    "ok": "✓",
    "disabled": "·",
    "info": "i",
    "unverified": "?",
    "wrong_service": "✗",
    "config_error": "✗",
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
    olog_freetext = (
        "withheld"
        if report.privacy.olog_freetext_withheld
        else "FULL (declared local test data — ESS-spec pending)"
    )
    lines.append(f"  Olog free-text:     {olog_freetext}")
    lines.append("")
    if not report.ok:
        verdict = "PROBLEM — a configured plane failed (see above)"
    elif report.verification_complete:
        # verification_complete is "no plane was left unverified" — vacuously true when nothing
        # was probed at all. The strong sentence is earned by actual identifications: with zero,
        # "answered AS ITSELF" would read as a confirmation of probes that never ran (measured:
        # it was printed for a completely empty config). Same source as the JSON's
        # identified_planes, so the human verdict and the machine signal cannot drift.
        verified = len(report.identified_planes)
        if verified:
            verdict = f"OK — every identity-probed plane answered AS ITSELF ({verified} verified)"
        else:
            verdict = (
                "OK — nothing failed, but nothing was identity-verified either "
                "(no REST plane is configured)"
            )
    else:
        # "all configured planes healthy" was the lie: it was printed for a ChannelFinder URL
        # pointing at a week-dead container, because a neighbouring service answered 401. Nothing
        # failed here either — but saying "healthy" would claim a confirmation we do not have.
        unverified = ", ".join(report.unverified_planes)
        verdict = (
            f"OK — no plane failed, but {len(report.unverified_planes)} could not prove its "
            f"identity: {unverified}. Reachable ≠ confirmed; see the '?' lines above."
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
