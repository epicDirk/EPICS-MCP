"""Offline tests for the fixture-finding walk (``find_moderate_pv``).

The pure logic and the injected-fetch walk are tested here; only the network entry point
stays live-exercised (it reproduced the fixture the archiver live suite runs on). The guards
were proven able to go red via module mutants BEFORE the first commit (Evidence discipline
rule 5); the mutant→node-id mapping lives HERE, because a commit body is not a discoverable
home:

* M1  non-list payload accepted      → test_parse_rejects_a_non_list_payload
* M2  row guards demoted to skips    → the 8 parse-reject node ids (non-object row,
      degenerate pvName ×3, unreadable eventRate ×4)
* M3  band bounds made exclusive     → test_band_bounds_are_inclusive_and_order_is_preserved
* M4  capped check removed           → test_classify_names_each_failing_precondition[capped]
* M5  lower window bound dropped     → test_carried_pre_window_sample_does_not_count,
      test_classify_names_each_failing_precondition[too-few-inside]
* M6  status check narrowed          → test_classify_names_each_failing_precondition[empty]
* M7  glob degenerated to name+*     → test_suggest_glob_targets_the_device_family
* M8  walk want-stop made strict     → test_walk_stops_once_want_is_reached
* M9  walk max-verify made strict    → test_walk_honours_the_verify_budget
* M10 response_error not counted     → test_walk_counts_unreadable_candidates_and_continues
* M11 naive window read as local     → test_naive_window_is_read_as_utc (proven on a
      non-UTC machine; on a UTC machine the mutant is invisible by construction)
* M12 argument validation removed    → test_impossible_argument_combinations_are_usage_errors

All names are synthetic: no facility value is committed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Literal

import pytest

from epics_pv_mcp.find_moderate_pv import (
    RateEntry,
    _parse_args,
    classify_history,
    count_inside,
    filter_band,
    parse_rate_report,
    suggest_glob,
    walk_candidates,
    window_epoch_bounds,
)
from epics_pv_mcp.services.archiver_client import HistoryResult, Sample
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverResponseError,
)

# The test window every history below is classified against.
_WINDOW = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
_LO, _HI = window_epoch_bounds(*_WINDOW)


def _sample(secs: float) -> Sample:
    return Sample(secs=int(secs), nanos=0, val=1.0, severity=0, status=0)


def _history(
    samples: list[Sample],
    *,
    status: Literal["ok", "empty", "withheld"] = "ok",
    capped: bool = False,
    withheld_reason: str | None = None,
) -> HistoryResult:
    return {
        "samples": samples,
        "capped": capped,
        "meta": {},
        "status": status,
        "note": "",
        "withheld_reason": withheld_reason,
    }


def _classify(history: HistoryResult) -> str | None:
    return classify_history(history, lo_ts=_LO, hi_ts=_HI, min_samples=3, min_inside=2)


# --- parse_rate_report: the measured shape parses, everything unreadable RAISES ---


def test_parse_accepts_the_measured_shape() -> None:
    """Measured: rows are {pvName, eventRate-as-string}; numeric rates are also readable."""
    rows = [
        {"pvName": "SIM:PS-01:Cur-RB", "eventRate": "1.5e-06"},
        {"pvName": "SIM:PS-02:Cur-RB", "eventRate": 0.25},
    ]
    assert parse_rate_report(rows) == [
        RateEntry(pv_name="SIM:PS-01:Cur-RB", event_rate=1.5e-06),
        RateEntry(pv_name="SIM:PS-02:Cur-RB", event_rate=0.25),
    ]


def test_parse_rejects_a_non_list_payload() -> None:
    with pytest.raises(ArchiverResponseError, match="expected a JSON array"):
        parse_rate_report({"pvName": "SIM:PS-01:Cur-RB", "eventRate": "1.0"})


def test_parse_rejects_a_non_object_row() -> None:
    with pytest.raises(ArchiverResponseError, match=r"\[1\]: expected an object"):
        parse_rate_report([{"pvName": "SIM:PS-01:Cur-RB", "eventRate": "1.0"}, 42])


@pytest.mark.parametrize("bad_name", [None, "", 7], ids=["missing", "empty", "non-str"])
def test_parse_rejects_a_degenerate_pv_name(bad_name: object) -> None:
    """A row without a real name must not be silently dropped, junk would otherwise wear the
    same face as a healthy, smaller report (the S11 class, degenerate-anchor variant)."""
    row: dict[str, object] = {"eventRate": "1.0"}
    if bad_name is not None:
        row["pvName"] = bad_name
    with pytest.raises(ArchiverResponseError, match="degenerate pvName"):
        parse_rate_report([row])


@pytest.mark.parametrize(
    "bad_rate", [None, "fast", True, [1.0]], ids=["missing", "non-numeric", "bool", "list"]
)
def test_parse_rejects_an_unreadable_event_rate(bad_rate: object) -> None:
    row: dict[str, object] = {"pvName": "SIM:PS-01:Cur-RB"}
    if bad_rate is not None:
        row["eventRate"] = bad_rate
    with pytest.raises(ArchiverResponseError, match="unreadable eventRate"):
        parse_rate_report([row])


# --- filter_band ---


def test_band_bounds_are_inclusive_and_order_is_preserved() -> None:
    entries = [
        RateEntry(pv_name="SIM:PS-01:A", event_rate=2e-6),  # above
        RateEntry(pv_name="SIM:PS-01:B", event_rate=1.6e-6),  # exactly the upper bound
        RateEntry(pv_name="SIM:PS-01:C", event_rate=5e-7),  # inside
        RateEntry(pv_name="SIM:PS-01:D", event_rate=1e-7),  # exactly the lower bound
        RateEntry(pv_name="SIM:PS-01:E", event_rate=5e-8),  # below
    ]
    kept = filter_band(entries, 1e-7, 1.6e-6)
    assert [entry["pv_name"] for entry in kept] == ["SIM:PS-01:B", "SIM:PS-01:C", "SIM:PS-01:D"]


# --- count_inside / classify_history: the fixture precondition, mirrored from the live test ---


def test_carried_pre_window_sample_does_not_count() -> None:
    """The appliance carries the last pre-window value into EVERY result, counting it would
    let a dormant PV (n=1, inside=0) pass as window-discriminating."""
    samples = [_sample(_LO - 86400), _sample(_LO + 10), _sample(_LO + 20)]
    assert count_inside(samples, _LO, _HI) == 2


def test_window_bounds_are_inclusive() -> None:
    assert count_inside([_sample(_LO), _sample(_HI)], _LO, _HI) == 2


def test_classify_verifies_the_exact_precondition_boundary() -> None:
    """Exactly min_samples with exactly min_inside (plus the carried sample) is a fixture."""
    samples = [_sample(_LO - 86400), _sample(_LO + 10), _sample(_LO + 20)]
    assert _classify(_history(samples)) is None


@pytest.mark.parametrize(
    ("history", "reason"),
    [
        (
            _history([], status="withheld", withheld_reason="unexpected_payload"),
            "status:withheld",
        ),
        (_history([], status="empty"), "status:empty"),
        (
            _history([_sample(_LO + i) for i in range(1, 51)], capped=True),
            "capped",
        ),
        (_history([_sample(_LO + 10), _sample(_LO + 20)]), "too_few_samples"),
        (
            _history([_sample(_LO - 86400), _sample(_LO - 3600), _sample(_LO + 10)]),
            "too_few_inside_window",
        ),
    ],
    ids=["withheld", "empty", "capped", "too-few-samples", "too-few-inside"],
)
def test_classify_names_each_failing_precondition(history: HistoryResult, reason: str) -> None:
    """``withheld`` is UNKNOWN (not proven empty) and ``empty`` cannot discriminate windows:
    neither may pass as a fixture; the remaining reasons mirror the live test's guards."""
    assert _classify(history) == reason


# --- window_epoch_bounds ---


def test_naive_window_is_read_as_utc() -> None:
    """A zone-less ISO value must mean the same instant as its ``Z`` twin: the fetch path
    normalizes naive values as UTC, so counting against the machine's LOCAL zone would shift
    the counting window against the fetched one by the local offset."""
    assert window_epoch_bounds("2026-01-01T00:00:00", "2027-01-01T00:00:00") == (_LO, _HI)


# --- suggest_glob ---


def test_suggest_glob_targets_the_device_family() -> None:
    assert suggest_glob("SIM:PS-01:Cur-RB") == "SIM:PS-01:*"
    assert suggest_glob("SIMPLE") == "SIMPLE*"


# --- walk_candidates: the injected-fetch walk (exit-code-relevant behaviour) ---


def _entry(name: str) -> RateEntry:
    return RateEntry(pv_name=name, event_rate=1e-6)


_GOOD = [_sample(_LO - 86400), _sample(_LO + 10), _sample(_LO + 20)]


def _walk(
    band: list[RateEntry],
    fetch: Callable[[str], HistoryResult],
    *,
    want: int = 3,
    max_verify: int = 200,
) -> tuple[list[tuple[RateEntry, int, int]], Counter[str], int]:
    return walk_candidates(
        band,
        fetch,
        lo_ts=_LO,
        hi_ts=_HI,
        min_samples=3,
        min_inside=2,
        want=want,
        max_verify=max_verify,
    )


def test_walk_stops_once_want_is_reached() -> None:
    band = [_entry(f"SIM:PS-0{i}:Cur-RB") for i in range(1, 6)]
    verified, reasons, checked = _walk(band, lambda _pv: _history(_GOOD), want=2)
    assert [v[0]["pv_name"] for v in verified] == ["SIM:PS-01:Cur-RB", "SIM:PS-02:Cur-RB"]
    assert checked == 2
    assert not reasons


def test_walk_honours_the_verify_budget() -> None:
    band = [_entry(f"SIM:PS-0{i}:Cur-RB") for i in range(1, 6)]
    verified, reasons, checked = _walk(band, lambda _pv: _history([], status="empty"), max_verify=4)
    assert not verified
    assert checked == 4
    assert reasons == {"status:empty": 4}


def test_walk_counts_unreadable_candidates_and_continues() -> None:
    """One odd PV must not abort the walk, but it is COUNTED, never silently dropped
    (a dropped one would make a broken backend look like a smaller, healthy band)."""

    def fetch(pv_name: str) -> HistoryResult:
        if pv_name == "SIM:PS-01:Cur-RB":
            raise ArchiverResponseError("unreadable")
        return _history(_GOOD)

    verified, reasons, checked = _walk(
        [_entry("SIM:PS-01:Cur-RB"), _entry("SIM:PS-02:Cur-RB")], fetch
    )
    assert [v[0]["pv_name"] for v in verified] == ["SIM:PS-02:Cur-RB"]
    assert reasons == {"response_error": 1}
    assert checked == 2


def test_walk_lets_a_transport_failure_propagate() -> None:
    """A transport that dies mid-walk is NOT a non-finding, it must reach the caller
    (which exits 2), never be counted down into an honest-looking 'nothing found'."""

    def fetch(_pv_name: str) -> HistoryResult:
        raise ArchiverConnectionError("transport died")

    with pytest.raises(ArchiverConnectionError):
        _walk([_entry("SIM:PS-01:Cur-RB")], fetch)


# --- argument validation: impossible runs are usage errors, not honest-looking non-findings ---


@pytest.mark.parametrize(
    "argv",
    [
        ["--want", "0"],
        ["--max-verify", "0"],
        ["--max-points", "2", "--min-samples", "3"],
        ["--band-min", "1e-5", "--band-max", "1e-7"],
    ],
    ids=["want-zero", "verify-zero", "cap-below-min-samples", "inverted-band"],
)
def test_impossible_argument_combinations_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse_args(argv)
    assert excinfo.value.code == 2
