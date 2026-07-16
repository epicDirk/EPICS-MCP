"""Find a moderately-archived PV to serve as a live-test fixture (read-only diagnostic).

WHY THIS EXISTS
---------------
The archiver live tests need a fixture PV with a handful of samples in a FIXED absolute window
(the defaults mirror ``tests/test_archiver_live.py``: 3–50 samples, at least 2 strictly inside,
not capped at ``max_points=50``). Blind enumeration cannot find one: an archive population is
often bimodal (fast, capped PVs plus carried-only ones whose single sample predates any window),
so a blind stride finds no middle — measured: five blind strategies over 153 candidates found
none, while the rate-report walk below verified its first five candidates in one pass.

THE MEASURED WALK (appliance 2.2.x, 2026-07-17)
-----------------------------------------------
1. ``GET <mgmt>/mgmt/bpl/getEventRateReport`` WITHOUT a ``limit`` param answers the whole
   report as ``[{pvName, eventRate}]``, sorted by rate descending, ``eventRate`` serialized
   as a string. ``limit`` is NOT a row cap: it behaves as if applied per cluster member with
   the members' slices merged (row counts = limit × member count, measured twice — 3→48 and
   100→1600 on a 16-member cluster; the mechanism is inferred from the counts, not observed).
   Omit it and filter client-side. The no-limit report is near-complete but NOT the whole
   archived set (measured: ~1.5M report rows vs ~1.7M getAllPVs names on the same cluster;
   the gap is unverified — plausibly paused or rate-less PVs).
2. Filter to a rate band (default ``1e-7..1.6e-6`` Hz ≈ 3–50 events/year).
3. COUNTER-VERIFY every band hit against the target window with a real history fetch: the
   report's rate is computed by the appliance over its own recent window, not over the
   caller's target window — a band hit is a hypothesis, never a fixture.

Deliberately NOT a console script (build-once: ``pyproject.toml`` stays untouched) — run it as
``python -m epics_pv_mcp.find_moderate_pv``. Facility-agnostic: the appliance URLs come from
``EPICS_MCP_ARCHIVER_URL`` / ``EPICS_MCP_ARCHIVER_RETRIEVAL_URL``, and nothing site-specific
lives in this file. Read-only: it only ever issues GETs.

Exit code: ``0`` — at least one candidate verified (recipe printed); ``1`` — an HONEST
non-finding (the walk ran, nothing satisfied the precondition; per-stage numbers printed);
``2`` — the walk could not run at all (missing URL, transport failure, unreadable report).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

from epics_pv_mcp.services.archiver_client import ArchiverClient, HistoryResult, Sample
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverResponseError,
)


class RateEntry(TypedDict):
    """One strictly-parsed row of the event-rate report."""

    pv_name: str
    event_rate: float


def parse_rate_report(payload: object) -> list[RateEntry]:
    """Strictly parse a ``getEventRateReport`` payload (S11 discipline).

    Unreadable input RAISES — never a silent item drop: a search that silently skips junk rows
    reports "no candidate found" with the same face as a healthy empty band, turning junk into
    a definitive non-finding (exactly the S11 class). Measured shape (appliance 2.2.x): a JSON
    array of ``{pvName, eventRate}`` objects with ``eventRate`` as a string; numeric rates are
    also accepted, bools are not (a ``true`` is junk, not a rate).
    """
    if not isinstance(payload, list):
        raise ArchiverResponseError(
            f"getEventRateReport: expected a JSON array, got {type(payload).__name__}"
        )
    entries: list[RateEntry] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ArchiverResponseError(
                f"getEventRateReport[{index}]: expected an object, got {type(item).__name__}"
            )
        name = item.get("pvName")
        if not isinstance(name, str) or not name:
            raise ArchiverResponseError(
                f"getEventRateReport[{index}]: missing or degenerate pvName: {name!r}"
            )
        rate_raw = item.get("eventRate")
        if isinstance(rate_raw, bool) or not isinstance(rate_raw, str | int | float):
            raise ArchiverResponseError(
                f"getEventRateReport[{index}]: unreadable eventRate: {rate_raw!r}"
            )
        try:
            rate = float(rate_raw)
        except ValueError as exc:
            raise ArchiverResponseError(
                f"getEventRateReport[{index}]: unreadable eventRate: {rate_raw!r}"
            ) from exc
        entries.append(RateEntry(pv_name=name, event_rate=rate))
    return entries


def filter_band(entries: list[RateEntry], band_min: float, band_max: float) -> list[RateEntry]:
    """Keep the entries whose rate lies in the INCLUSIVE band, preserving report order."""
    return [entry for entry in entries if band_min <= entry["event_rate"] <= band_max]


def window_epoch_bounds(start: str, end: str) -> tuple[float, float]:
    """Turn an ISO-8601 window (``Z`` accepted) into epoch-second bounds.

    A zone-less value is read as UTC — the same convention the fetch path applies when it
    normalizes the window; letting ``timestamp()`` guess the machine's LOCAL zone would shift
    the counting window against the fetched one by the local offset, so a candidate near the
    window edge would be classified against a different window than the one fetched.
    """

    def _epoch(value: str) -> float:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()

    return _epoch(start), _epoch(end)


def count_inside(samples: list[Sample], lo_ts: float, hi_ts: float) -> int:
    """Samples inside ``[lo_ts, hi_ts]`` (inclusive bounds).

    The appliance also carries the last value from BEFORE the window start into every result,
    whatever window is asked for — that carried sample must not count, or a dormant PV looks
    window-discriminating when it is not (mirrors the live test's ``_inside_window`` guard).
    """
    return sum(1 for sample in samples if lo_ts <= sample["secs"] <= hi_ts)


def classify_history(
    history: HistoryResult,
    *,
    lo_ts: float,
    hi_ts: float,
    min_samples: int,
    min_inside: int,
) -> str | None:
    """``None`` when the history satisfies the fixture precondition, else the failure reason.

    ``status`` must be ``"ok"``: ``withheld`` means the history is UNKNOWN (not proven empty)
    and ``empty`` cannot discriminate windows — neither may pass as a fixture.
    """
    if history["status"] != "ok":
        return f"status:{history['status']}"
    if history["capped"]:
        return "capped"
    if len(history["samples"]) < min_samples:
        return "too_few_samples"
    if count_inside(history["samples"], lo_ts, hi_ts) < min_inside:
        return "too_few_inside_window"
    return None


def suggest_glob(pv_name: str) -> str:
    """A name glob for the fixture's device family (``EPICS_MCP_LIVE_ARCHIVER_GLOB``).

    The glob must match SOME archived PVs but not the whole population: a live premise test
    compares a filtered against an unfiltered enumeration and needs the two to differ.
    """
    stem, sep, _leaf = pv_name.rpartition(":")
    return f"{stem}:*" if sep else f"{pv_name}*"


def walk_candidates(
    band: list[RateEntry],
    fetch: Callable[[str], HistoryResult],
    *,
    lo_ts: float,
    hi_ts: float,
    min_samples: int,
    min_inside: int,
    want: int,
    max_verify: int,
) -> tuple[list[tuple[RateEntry, int, int]], Counter[str], int]:
    """Verify band candidates in order until *want* verified or *max_verify* checked.

    Returns ``(verified, fail_reasons, checked)`` with ``verified`` as
    ``(entry, samples, inside)`` triples. A ``fetch`` raising ``ArchiverResponseError``
    counts that candidate as ``response_error`` and continues — one odd PV must not abort
    the walk, but it is COUNTED, never silently dropped. Any other exception (a transport
    failure above all) PROPAGATES: the caller must not read a dead transport as a
    non-finding.
    """
    reasons: Counter[str] = Counter()
    verified: list[tuple[RateEntry, int, int]] = []
    checked = 0
    for entry in band:
        if len(verified) >= want or checked >= max_verify:
            break
        checked += 1
        try:
            history = fetch(entry["pv_name"])
        except ArchiverResponseError:
            reasons["response_error"] += 1
            continue
        reason = classify_history(
            history, lo_ts=lo_ts, hi_ts=hi_ts, min_samples=min_samples, min_inside=min_inside
        )
        if reason is None:
            inside = count_inside(history["samples"], lo_ts, hi_ts)
            verified.append((entry, len(history["samples"]), inside))
        else:
            reasons[reason] += 1
    return verified, reasons, checked


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m epics_pv_mcp.find_moderate_pv",
        description=(
            "Read-only: find an archived PV with a handful of samples in a fixed window "
            "(rate-report walk + per-candidate counter-verification)."
        ),
    )
    parser.add_argument("--band-min", type=float, default=1e-7, help="band lower bound in Hz")
    parser.add_argument("--band-max", type=float, default=1.6e-6, help="band upper bound in Hz")
    parser.add_argument(
        "--window-start", default="2026-01-01T00:00:00Z", help="target window start (ISO-8601)"
    )
    parser.add_argument(
        "--window-end", default="2027-01-01T00:00:00Z", help="target window end (ISO-8601)"
    )
    parser.add_argument(
        "--max-points", type=int, default=50, help="history cap the fixture must stay under"
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=3,
        help="minimum samples in the RESULT (includes the carried pre-window sample)",
    )
    parser.add_argument(
        "--min-inside",
        type=int,
        default=2,
        help="minimum samples inside [start, end] (the carried pre-window sample never counts)",
    )
    parser.add_argument(
        "--max-verify", type=int, default=200, help="verify at most this many band candidates"
    )
    parser.add_argument(
        "--want", type=int, default=3, help="stop after this many verified candidates"
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="per-request timeout in seconds"
    )
    args = parser.parse_args(argv)
    # Structurally impossible runs are usage errors, not honest-looking non-findings: with
    # max_points < min_samples every PV loses (>=min_samples ⇒ capped, fewer ⇒ too few), and
    # want=0 "finds nothing" without checking anything.
    if args.want < 1:
        parser.error("--want must be >= 1")
    if args.max_verify < 1:
        parser.error("--max-verify must be >= 1")
    if args.max_points < args.min_samples:
        parser.error("--max-points must be >= --min-samples (success would be impossible)")
    if args.band_min > args.band_max:
        parser.error("--band-min must be <= --band-max")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the walk; print verified candidates and a ready-to-use env recipe."""
    args = _parse_args(argv)
    out = sys.stdout.write

    base = os.environ.get("EPICS_MCP_ARCHIVER_URL", "")
    if not base:
        sys.stderr.write("find_moderate_pv: EPICS_MCP_ARCHIVER_URL is not set\n")
        return 2
    try:
        lo_ts, hi_ts = window_epoch_bounds(args.window_start, args.window_end)
    except ValueError as exc:
        sys.stderr.write(f"find_moderate_pv: unreadable --window-start/--window-end: {exc}\n")
        return 2
    client = ArchiverClient(
        base,
        timeout=args.timeout,
        retrieval_url=os.environ.get("EPICS_MCP_ARCHIVER_RETRIEVAL_URL") or None,
    )

    # A diagnostic reaching for a raw MGMT report endpoint — the same pattern the live premise
    # test uses; the strict parse right below is this module's own response boundary.
    try:
        payload = client._get(f"{client.base_url}/mgmt/bpl/getEventRateReport", {})
        entries = parse_rate_report(payload)
    except (ArchiverConnectionError, ArchiverResponseError) as exc:
        sys.stderr.write(f"find_moderate_pv: the report walk could not run: {exc}\n")
        return 2

    band = filter_band(entries, args.band_min, args.band_max)
    out(
        f"event-rate report: {len(entries)} rows; "
        f"band [{args.band_min:g}..{args.band_max:g}] Hz: {len(band)} candidates\n"
    )

    def fetch(pv_name: str) -> HistoryResult:
        return client.get_pv_history(
            pv_name, args.window_start, args.window_end, max_points=args.max_points
        )

    try:
        verified, reasons, checked = walk_candidates(
            band,
            fetch,
            lo_ts=lo_ts,
            hi_ts=hi_ts,
            min_samples=args.min_samples,
            min_inside=args.min_inside,
            want=args.want,
            max_verify=args.max_verify,
        )
    except ArchiverConnectionError as exc:
        # A transport that died mid-walk is NOT a non-finding — exit 2, never 1.
        sys.stderr.write(f"find_moderate_pv: transport failed mid-walk: {exc}\n")
        return 2

    for entry, samples, inside in verified:
        out(
            f"VERIFIED {entry['pv_name']}  rate={entry['event_rate']:g} Hz  "
            f"samples={samples}  inside={inside}\n"
        )
    out(f"checked={checked} verified={len(verified)} fail_reasons={dict(reasons)}\n")
    if checked and not verified and reasons.get("response_error", 0) == checked:
        # EVERY checked candidate errored: the walk measured nothing — a wrong history URL
        # (e.g. a split deployment without its RETRIEVAL root) must not wear the face of an
        # honest non-finding.
        sys.stderr.write(
            "find_moderate_pv: every checked candidate answered unreadably — the walk "
            "measured nothing (check the MGMT and RETRIEVAL URLs); this is NOT a non-finding\n"
        )
        return 2
    if not verified:
        # An honest non-finding carries its numbers: what was walked, and why each stage lost.
        out(
            "no candidate satisfied the precondition — widen --band-min/--band-max or raise "
            "--max-verify; every number above is the evidence trail\n"
        )
        return 1

    first = verified[0][0]["pv_name"]
    out("fixture recipe (first verified candidate):\n")
    out(f"  export EPICS_MCP_LIVE_ARCHIVER_PV='{first}'\n")
    out(f"  export EPICS_MCP_LIVE_ARCHIVER_GLOB='{suggest_glob(first)}'\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
