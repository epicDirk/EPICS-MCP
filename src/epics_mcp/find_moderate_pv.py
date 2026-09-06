"""Find a moderately-archived PV to serve as a live-test fixture (read-only diagnostic).

WHY THIS EXISTS
---------------
The archiver live tests need a fixture PV with a handful of samples in a FIXED absolute window,
and it must satisfy every premise that suite asserts, not merely the sample band. Both sides now
read the criteria from ``FIXTURE_*`` below and the extra premises from
:func:`unmet_extra_premises`, so there is one place to change and no second copy to drift
(GQ-289; until then this paragraph claimed the defaults "mirror" the live test, which held for
the window, the counts and the cap and for nothing else, and a "verified" candidate could still
fail four assertions and need a hand-run counter-check).

Blind enumeration cannot find one: an archive population is
often bimodal (fast, capped PVs plus carried-only ones whose single sample predates any window),
so a blind stride finds no middle, measured: five blind strategies over 153 candidates found
none, while the rate-report walk below verified its first five candidates in one pass.

THE MEASURED WALK (appliance 2.2.x, 2026-07-17)
-----------------------------------------------
1. ``GET <mgmt>/mgmt/bpl/getEventRateReport`` WITHOUT a ``limit`` param answers the whole
   report as ``[{pvName, eventRate}]``, sorted by rate descending, ``eventRate`` serialized
   as a string. ``limit`` is NOT a row cap: it behaves as if applied per cluster member with
   the members' slices merged (row counts = limit × member count, measured twice, 3→48 and
   100→1600 on a 16-member cluster; the mechanism is inferred from the counts, not observed).
   Omit it and filter client-side. The no-limit report is near-complete but NOT the whole
   archived set (measured: ~1.5M report rows vs ~1.7M getAllPVs names on the same cluster;
   the gap is unverified, plausibly paused or rate-less PVs).
2. Filter to a rate band (default ``1e-7..1.6e-6`` Hz ≈ 3-50 events/year).
3. COUNTER-VERIFY every band hit against the target window with a real history fetch: the
   report's rate is computed by the appliance over its own recent window, not over the
   caller's target window, a band hit is a hypothesis, never a fixture.

Deliberately NOT a console script (build-once: ``pyproject.toml`` stays untouched), run it as
``python -m epics_mcp.find_moderate_pv``. Facility-agnostic: the appliance URLs come from
``EPICS_MCP_ARCHIVER_URL`` / ``EPICS_MCP_ARCHIVER_RETRIEVAL_URL``, and nothing site-specific
lives in this file. Read-only: it only ever issues GETs.

Exit code: ``0``: at least one candidate verified (recipe printed); ``1``: an HONEST
non-finding (the walk ran, nothing satisfied the precondition; per-stage numbers printed);
``2``: the walk could not run at all (missing URL, transport failure, unreadable report).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

from epics_mcp.cli_common import positive_timeout
from epics_mcp.services.archiver_client import ArchiverClient, HistoryResult, Sample
from epics_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverResponseError,
)

# ---------------------------------------------------------------------------
# The fixture criteria, and this is the ONE place they are written down (GQ-289).
#
# ``tests/test_archiver_live.py`` imports these instead of spelling its own copies. Before that
# the two sides drifted: this module's docstring claimed "the defaults mirror the live test", and
# it was true for the window, the sample counts and the cap and for nothing else. The live suite
# additionally demands that the PV be archived, carry a type record, hold no samples in the 2020
# short window, and answer ``ok`` over 2020-2030, and a candidate this module "verified" could
# still fail all four. Someone then had to counter-check every candidate by hand, which is the
# cost that made the drift visible.
#
# ⚠ Constants only, plus one predicate over the extra premises. The live test keeps its own
# ASSERTIONS: it is the guard that pins the wire schema, and a guard that calls the code it guards
# proves nothing. What is shared is the CRITERIA, not the checking.
# ---------------------------------------------------------------------------

#: The window the fixture must hold a handful of samples in.
FIXTURE_WINDOW = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
#: The past window that must come back EMPTY: the live suite's negative control, without which
#: its positive test would also pass if the window were ignored and the whole history returned.
FIXTURE_PAST_WINDOW = ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")
#: The wide window the live schema anchor reads samples from; it must answer ``ok``.
FIXTURE_SCHEMA_WINDOW = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
#: The per-probe sample cap the fixture must stay UNDER (not merely at).
FIXTURE_MAX_POINTS = 50
#: Minimum samples in the result, carried pre-window sample included.
FIXTURE_MIN_SAMPLES = 3
#: Minimum samples strictly inside the window; the carried sample never counts.
FIXTURE_MIN_INSIDE = 2


class RateEntry(TypedDict):
    """One strictly-parsed row of the event-rate report."""

    pv_name: str
    event_rate: float


def parse_rate_report(payload: object) -> list[RateEntry]:
    """Strictly parse a ``getEventRateReport`` payload (S11 discipline).

    Unreadable input RAISES: never a silent item drop: a search that silently skips junk rows
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

    A zone-less value is read as UTC, the same convention the fetch path applies when it
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
    whatever window is asked for, that carried sample must not count, or a dormant PV looks
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
    and ``empty`` cannot discriminate windows, neither may pass as a fixture.
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


def unmet_extra_premises(client: ArchiverClient, pv_name: str) -> list[str]:
    """Every live-suite premise beyond the sample band that *pv_name* fails, empty when it holds.

    The premises the walk used to ignore, each named after the live assertion it reproduces:

    * ``not_archived``: the suite asserts ``is_archived(pv)`` is True and the status a non-empty
      string.
    * ``no_type_info``: it asserts ``get_pv_type_info(pv)["found"]`` is True.
    * ``past_window_not_empty``: it asserts the 2020 short window returns no samples.
    * ``past_window_withheld``: it ALSO asserts that window's ``status`` is ``"empty"`` and its
      ``withheld_reason`` ``None``, which is a second, independent condition on the same probe.
    * ``schema_window_not_ok``: it asserts the 2020-2030 window answers ``ok``, so the sample
      schema anchor has something to pin.

    ⛔ The past window needs BOTH of its checks, and leaving the second out was a real defect in
    this function's first version. ``_withheld_history`` always returns ``samples=[]``, so an
    UNREADABLE answer passes a length check by construction: counting alone would verify every
    such PV as a good fixture and hand the live suite one that fails at its status assertion. The
    build that introduced this had proven that exact point one commit earlier, in the live test's
    own comment, and then reproduced only the counting half.

    ⛔ ``past_window_not_empty`` counts the RESULT LENGTH, deliberately, and not
    :func:`count_inside`. The live test counts the same way, and the two differ: the appliance
    carries the last sample from before a window into the result, so a PV whose archive starts
    before 2020-01-02 can return a sample here while having none INSIDE. Counting only the inside
    ones would pass a candidate the live suite then rejects, which is the exact failure this whole
    change is about. The rule is to reproduce the test's criterion, not to invent a better one.

    Every probe is a GET. An unreadable answer is not swallowed: ``ArchiverResponseError``
    propagates to the walk, which counts the candidate as ``response_error`` and moves on, so an
    odd PV is never silently read as a failing premise.
    """
    unmet: list[str] = []

    archived, status = client.is_archived(pv_name)
    if not archived or not status:
        unmet.append("not_archived")
    if client.get_pv_type_info(pv_name).get("found") is not True:
        unmet.append("no_type_info")

    past = client.get_pv_history(pv_name, *FIXTURE_PAST_WINDOW, max_points=FIXTURE_MAX_POINTS)
    if len(past["samples"]) != 0:
        unmet.append("past_window_not_empty")
    elif past["status"] != "empty":
        # ⛔ Counting alone is NOT the test's criterion, and this line is the post-build review's
        # correction of a build that had just proven the same point one commit earlier. The live
        # test asserts three things about this window: no samples, status "empty", and no
        # withheld_reason. A withheld result satisfies the first of those by construction, because
        # _withheld_history always returns samples=[]. Checking only the count therefore verifies
        # every unreadable answer as a good fixture and hands the live suite a PV that fails it.
        unmet.append("past_window_withheld")

    schema = client.get_pv_history(pv_name, *FIXTURE_SCHEMA_WINDOW, max_points=FIXTURE_MAX_POINTS)
    if schema["status"] != "ok":
        unmet.append("schema_window_not_ok")

    return unmet


def glob_discriminates(client: ArchiverClient, glob: str) -> bool:
    """Whether *glob* really filters ``getAllPVs``, the premise the printed recipe depends on.

    The fifth gap, and it was not in the original report of this defect: the walk PRINTS a glob
    into its fixture recipe and never checks it, while the live suite hangs two assertions on it.
    A perfect fixture PV can therefore still leave the suite red.

    ⛔ This does NOT reproduce either live assertion literally, and the first version of this
    docstring claimed it did. Measured: the live test builds ``unfiltered`` from
    ``getPVsForThisAppliance`` and compares ``getAllPVs``-with-glob against THAT, so its second
    assertion spans two different endpoints and is satisfied by any glob whenever the two
    endpoints answer different lists, which on a cluster they routinely do. What is checked here
    is the property that assertion is FOR, stated in the comment above it: that ``getAllPVs``
    really filters. Same endpoint, with and without the glob.

    ⚠ That makes this predicate STRICTER than the assertion it guards, which is the safe
    direction: a glob passing here passes there, and the reverse does not hold. It also means a
    green run here is not a proof about the live assertion's literal text, and the discrepancy in
    that text is reported as its own finding rather than quietly patched from this side.
    """
    unfiltered = client._get(f"{client.base_url}/mgmt/bpl/getAllPVs", {"limit": "5"})
    by_name = client._get(f"{client.base_url}/mgmt/bpl/getAllPVs", {"limit": "5", "pv": glob})
    return bool(by_name != unfiltered)


def walk_candidates(
    band: list[RateEntry],
    fetch: Callable[[str], HistoryResult],
    *,
    extra_premises: Callable[[str], list[str]],
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
    counts that candidate as ``response_error`` and continues, one odd PV must not abort
    the walk, but it is COUNTED, never silently dropped. Any other exception (a transport
    failure above all) PROPAGATES: the caller must not read a dead transport as a
    non-finding.

    *extra_premises* answers the live-suite premises the sample band cannot see, normally
    :func:`unmet_extra_premises`; each name it returns is counted as its own fail reason. It is
    consulted only AFTER the band criteria pass, so its four GETs are spent on the few candidates
    that got that far rather than on every row of the report.

    ⛔ Required, not defaulted, and deliberately so (GQ-289). A default of "check nothing" is how
    this walk shipped a candidate the live suite then rejected: the omission had no symptom at the
    call site, and the cost surfaced only as a counter-check someone ran by hand. A caller that
    genuinely wants no extra probe says so in its own words, ``lambda _pv: []``, and that is then
    visible in the diff.
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
            reason = classify_history(
                history, lo_ts=lo_ts, hi_ts=hi_ts, min_samples=min_samples, min_inside=min_inside
            )
            if reason is not None:
                reasons[reason] += 1
                continue
            unmet = extra_premises(entry["pv_name"])
        except ArchiverResponseError:
            reasons["response_error"] += 1
            continue
        if unmet:
            reasons.update(unmet)
            continue
        inside = count_inside(history["samples"], lo_ts, hi_ts)
        verified.append((entry, len(history["samples"]), inside))
    return verified, reasons, checked


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m epics_mcp.find_moderate_pv",
        description=(
            "Read-only: find an archived PV with a handful of samples in a fixed window "
            "(rate-report walk + per-candidate counter-verification)."
        ),
    )
    parser.add_argument("--band-min", type=float, default=1e-7, help="band lower bound in Hz")
    parser.add_argument("--band-max", type=float, default=1.6e-6, help="band upper bound in Hz")
    parser.add_argument(
        "--window-start", default=FIXTURE_WINDOW[0], help="target window start (ISO-8601)"
    )
    parser.add_argument(
        "--window-end", default=FIXTURE_WINDOW[1], help="target window end (ISO-8601)"
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=FIXTURE_MAX_POINTS,
        help="history cap the fixture must stay under",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=FIXTURE_MIN_SAMPLES,
        help="minimum samples in the RESULT (includes the carried pre-window sample)",
    )
    parser.add_argument(
        "--min-inside",
        type=int,
        default=FIXTURE_MIN_INSIDE,
        help="minimum samples inside [start, end] (the carried pre-window sample never counts)",
    )
    parser.add_argument(
        "--max-verify", type=int, default=200, help="verify at most this many band candidates"
    )
    parser.add_argument(
        "--want", type=int, default=3, help="stop after this many verified candidates"
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=120.0,
        help="per-request timeout in seconds, > 0",
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

    # A diagnostic reaching for a raw MGMT report endpoint, the same pattern the live premise
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

    def extra_premises(pv_name: str) -> list[str]:
        return unmet_extra_premises(client, pv_name)

    try:
        verified, reasons, checked = walk_candidates(
            band,
            fetch,
            extra_premises=extra_premises,
            lo_ts=lo_ts,
            hi_ts=hi_ts,
            min_samples=args.min_samples,
            min_inside=args.min_inside,
            want=args.want,
            max_verify=args.max_verify,
        )
    except ArchiverConnectionError as exc:
        # A transport that died mid-walk is NOT a non-finding, exit 2, never 1.
        sys.stderr.write(f"find_moderate_pv: transport failed mid-walk: {exc}\n")
        return 2

    for entry, samples, inside in verified:
        out(
            f"VERIFIED {entry['pv_name']}  rate={entry['event_rate']:g} Hz  "
            f"samples={samples}  inside={inside}\n"
        )
    out(f"checked={checked} verified={len(verified)} fail_reasons={dict(reasons)}\n")
    if checked and not verified and reasons.get("response_error", 0) == checked:
        # EVERY checked candidate errored: the walk measured nothing, a wrong history URL
        # (e.g. a split deployment without its RETRIEVAL root) must not wear the face of an
        # honest non-finding.
        sys.stderr.write(
            "find_moderate_pv: every checked candidate answered unreadably, the walk "
            "measured nothing (check the MGMT and RETRIEVAL URLs); this is NOT a non-finding\n"
        )
        return 2
    if not verified:
        # An honest non-finding carries its numbers: what was walked, and why each stage lost.
        out(
            "no candidate satisfied the precondition, widen --band-min/--band-max or raise "
            "--max-verify; every number above is the evidence trail\n"
        )
        return 1

    first = verified[0][0]["pv_name"]
    glob = suggest_glob(first)
    # The glob is VERIFIED before it is printed, not merely derived (GQ-289). A recipe whose glob
    # does not discriminate leaves the live suite red with a perfectly good fixture PV, and the
    # failure then reads as a problem with the PV.
    try:
        discriminates = glob_discriminates(client, glob)
    except (ArchiverConnectionError, ArchiverResponseError) as exc:
        sys.stderr.write(
            f"find_moderate_pv: the glob probe could not run ({exc}); the PV below is verified, "
            f"the glob is NOT\n"
        )
        discriminates = True  # unmeasured, and said so above: never silently reported as failed
    out("fixture recipe (first verified candidate):\n")
    out(f"  export EPICS_MCP_LIVE_ARCHIVER_PV='{first}'\n")
    out(f"  export EPICS_MCP_LIVE_ARCHIVER_GLOB='{glob}'\n")
    if not discriminates:
        sys.stderr.write(
            "find_moderate_pv: the suggested glob does NOT filter getAllPVs (the live premise "
            "test compares a filtered against an unfiltered enumeration and needs them to "
            "differ); widen or shorten it by hand before using this recipe\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
