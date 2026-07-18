"""CLI for the read-only config self-check (``epics-doctor``).

Probes every CONFIGURED plane (a transport probe, refined on success by an identity probe — up to
two requests for a healthy plane; retries on a 5xx add more) and prints whether it is reachable,
whether the CA bundle works, whether the service **identifies itself as the one that URL is
supposed to point at**, and what the ChannelFinder privacy redaction is set to — the ``flutter
doctor`` of this server. Read-only — it probes, never writes — and it touches exactly the planes
that are CONFIGURED (a disabled plane makes no network call).

Exit code (a DELIBERATE convention, unlike the other CLIs where a finding is exit 0 — doctor is
a scriptable pass/fail):

* ``0`` — nothing failed and no identity probe failed (healthy, honestly disabled/info-only, or
  reachable with its identity ``unverified`` — a 2xx that just could not be named);
* ``1`` — a configured plane HARD-failed (unreachable / ca_error / api_error / config_error /
  probe-disconnect);
* ``2`` — a usage error (bad arguments, or an internal EpicsError);
* ``3`` — INCONCLUSIVE: a configured plane is reachable but its identity probe FAILED (a served
  non-2xx like a 401/404, a transport error, or a refused redirect on the identity endpoint). Not a
  hard failure (the plane's TOOL endpoints may work), but not a silent all-clear either.

The exit code relates to ``--json`` as: ``0`` = ``ok`` ∧ no ``inconclusive_identity_planes``; ``3``
= ``ok`` ∧ some ``inconclusive_identity_planes``; ``1`` = not ``ok``; ``2`` = usage/internal. So
``ok`` alone is True for BOTH exit 0 and exit 3 — do NOT derive the exit code from ``ok`` alone.

⚠️ Exit ``0`` means "nothing failed", NOT "everything was confirmed": a plane can be reachable with
its identity unverified and still exit 0 (that is honest, not healthy — see ``doctor.py``). A
machine reader must therefore look at ``verification_complete`` / ``unverified_planes`` /
``inconclusive_identity_planes`` in ``--json``, not only at the exit code — and for POSITIVE
confirmation assert ``identified_planes`` is non-empty (``verification_complete`` is vacuously true
on an empty config).

Usage::

    epics-doctor [--probe-pv DEV-TEST01:Ctrl-EVR-01:12VValue] [--timeout 5] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import Literal

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.doctor import DoctorReport, run_doctor

#: One glyph per status for the human-readable render (deterministic). ``unverified`` and
#: ``identity_probe_failed`` get their own marks rather than borrowing ✓ or ✗: they are neither
#: "confirmed" nor "broken", and the whole point of those states is that both were being conflated —
#: ``?`` = answered 2xx but not nameable (exit 0), ``!`` = the identity probe failed (exit 3).
_STATUS_MARK = {
    "ok": "✓",
    "disabled": "·",
    "info": "i",
    "unverified": "?",
    "identity_probe_failed": "!",
    "config_error": "✗",
    "ca_error": "✗",
    "api_error": "✗",
    "unreachable": "✗",
    "disconnected": "✗",
}


def _exit_category(report: DoctorReport) -> Literal["failed", "inconclusive", "clean"]:
    """The ONE verdict precedence, consumed by both :func:`main` (→ exit code) and :func:`_render`
    (→ verdict line) so the two cannot drift. A hard failure dominates an inconclusive identity
    probe, which dominates a clean run: ``failed`` → exit 1, ``inconclusive`` → exit 3, ``clean`` →
    exit 0. ``report.ok`` is False iff a plane HARD-failed; ``inconclusive_identity_planes`` is
    non-empty iff a probe failed (with ``ok`` still True — an inconclusive probe is not a hard
    failure). A ``clean`` run may still be ``unverified`` (answered 2xx, unnameable) — that is exit
    0, honest-not-confirmed."""
    if not report.ok:
        return "failed"
    if report.inconclusive_identity_planes:
        return "inconclusive"
    return "clean"


#: Category → process exit code. The single mapping :func:`main` uses.
_EXIT_CODE: dict[str, int] = {"failed": 1, "inconclusive": 3, "clean": 0}


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
    # One precedence, shared with main() via _exit_category, so the verdict word and the exit code
    # can never drift: failed → PROBLEM (exit 1), inconclusive → INCONCLUSIVE (exit 3), clean → OK.
    category = _exit_category(report)
    if category == "failed":
        verdict = "PROBLEM — a configured plane failed (see above)"
    elif category == "inconclusive":
        # The S4 lie in its exact form: "all configured planes healthy" was printed for a
        # ChannelFinder URL at a week-dead container because a neighbour answered 401. That probe
        # FAILED — it is not a silent OK. Not a hard PROBLEM either (the plane's tool endpoints may
        # work), so it earns its own INCONCLUSIVE verdict and exit 3, never "OK".
        planes = ", ".join(report.inconclusive_identity_planes)
        n = len(report.inconclusive_identity_planes)
        also = (
            f" ({len(report.unverified_planes)} other plane(s) also unverified)"
            if report.unverified_planes
            else ""
        )
        verdict = (
            f"INCONCLUSIVE — {n} identity probe(s) FAILED (reachable, but the identity endpoint "
            f"did not return a usable response): {planes}{also}. Not a confirmed failure, but not "
            "confirmed healthy — see the '!' lines above."
        )
    elif report.verification_complete:
        # verification_complete is "every plane's identity was established" — vacuously true when
        # nothing was probed at all. The strong sentence is earned by actual identifications: with
        # zero, "answered AS ITSELF" would read as confirmation of probes that never ran (measured:
        # it printed for a completely empty config). Same source as the JSON's identified_planes, so
        # the human verdict and the machine signal cannot drift.
        verified = len(report.identified_planes)
        if verified:
            verdict = f"OK — every identity-probed plane answered AS ITSELF ({verified} verified)"
        else:
            verdict = (
                "OK — nothing failed, but nothing was identity-verified either "
                "(no REST plane is configured)"
            )
    else:
        # Clean (exit 0) but some plane answered 2xx without a nameable identity — honest, not
        # confirmed. Saying "healthy" would claim a confirmation we do not have.
        unverified = ", ".join(report.unverified_planes)
        verdict = (
            f"OK — no plane failed, but {len(report.unverified_planes)} could not prove its "
            f"identity: {unverified}. Reachable ≠ confirmed; see the '?' lines above."
        )
    lines.append(f"Overall: {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the self-check, print the report. Returns 0 (clean) / 1 (a plane hard-failed) / 2
    (usage or internal error) / 3 (reachable, but an identity probe failed — inconclusive)."""
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
    # 0 clean / 3 inconclusive / 1 hard-failed — via the same _exit_category _render used, so the
    # printed verdict and the exit code are guaranteed consistent.
    return _EXIT_CODE[_exit_category(report)]


if __name__ == "__main__":
    raise SystemExit(main())
