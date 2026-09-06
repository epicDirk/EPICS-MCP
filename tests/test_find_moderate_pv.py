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
* M13 an extra premise not checked    → test_each_live_premise_is_checked_on_its_own (6 ids:
      not-archived, empty-status-string, no-type-record, past-window-carries-a-sample,
      schema-window-empty, schema-window-withheld)
* M14 past window counted with        → test_the_past_window_criterion_counts_the_RESULT_and_
      count_inside instead of len()      not_the_inside_samples
* M15 premise failure not counted     → test_a_failing_premise_keeps_the_candidate_out_and_names_
      into fail_reasons                  the_reason
* M16 premises probed before the      → test_the_extra_premises_are_only_paid_for_by_candidates_
      band filter                        that_passed_the_band
* M17 glob predicate always True      → test_the_glob_predicate_reports_whether_getallpvs_really_
                                         filters[glob-ignored]
* M18 past window checked by count    → test_each_live_premise_is_checked_on_its_own[past-window-
      only, not by status                unreadable],
                                         test_an_unreadable_past_window_is_not_an_empty_one

⚠ M18 is the post-build review's own finding: the first version checked the past window by
sample count alone, and a withheld result has zero samples by construction, so every unreadable
answer passed as a good fixture.

⚠ M13 to M18 arrived with GQ-289 and are APPENDED, never inserted: the numbers are identities,
cited from commit messages and analysis notes, so renumbering would quietly re-point every earlier
citation at a different mutant.

⚠ Two rows the table did NOT have before and still does not claim as mutants: ``too_few_samples``
and the ``withheld`` branch each have a test but no mutant line. They were measured as gaps while
GQ-289 was built and are named here rather than left implicit, because a table that silently
covers less than it appears to is the failure this module is about.

All names are synthetic: no facility value is committed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Literal, cast

import pytest

from epics_mcp.find_moderate_pv import (
    FIXTURE_PAST_WINDOW,
    RateEntry,
    _parse_args,
    classify_history,
    count_inside,
    filter_band,
    glob_discriminates,
    parse_rate_report,
    suggest_glob,
    unmet_extra_premises,
    walk_candidates,
    window_epoch_bounds,
)
from epics_mcp.services.archiver_client import ArchiverClient, HistoryResult, Sample
from epics_mcp.services.archiver_exceptions import (
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
    extra_premises: Callable[[str], list[str]] | None = None,
) -> tuple[list[tuple[RateEntry, int, int]], Counter[str], int]:
    """The band-only walk unless a test says otherwise.

    ``extra_premises`` is REQUIRED by walk_candidates and defaulted here rather than there: the
    tests below are about the band logic, and every one of them would otherwise carry the same
    noise. The production call site has no such default, which is the point (GQ-289).
    """
    return walk_candidates(
        band,
        fetch,
        extra_premises=extra_premises if extra_premises is not None else (lambda _pv: []),
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


# ---------------------------------------------------------------------------
# GQ-289: the four live premises the band cannot see, plus the glob
#
# A fake client rather than a Mock: each probe has to answer a DIFFERENT shape, and naming them as
# fields makes the per-criterion cases below read as the criteria they test.
# ---------------------------------------------------------------------------


class _FakeArchiver:
    """Answers the four probes ``unmet_extra_premises`` makes, each independently steerable."""

    base_url = "http://arch/x"

    def __init__(
        self,
        *,
        archived: bool = True,
        status: str = "Being archived",
        found: bool = True,
        past_samples: int = 0,
        past_status: Literal["ok", "empty", "withheld"] = "empty",
        schema_status: Literal["ok", "empty", "withheld"] = "ok",
        all_pvs: object = ("A", "B", "C"),
        filtered_pvs: object = ("A",),
    ) -> None:
        self.archived = archived
        self.status = status
        self.found = found
        self.past_samples = past_samples
        self.past_status = past_status
        self.schema_status = schema_status
        self.all_pvs = all_pvs
        self.filtered_pvs = filtered_pvs

    def is_archived(self, _pv: str) -> tuple[bool, str]:
        return self.archived, self.status

    def get_pv_type_info(self, _pv: str) -> dict[str, object]:
        return {"found": self.found}

    def get_pv_history(
        self, _pv: str, _start: str, end: str, *, max_points: int = 50
    ) -> HistoryResult:
        # Keyed on the END, not the start: FIXTURE_PAST_WINDOW and FIXTURE_SCHEMA_WINDOW share
        # the same start ("2020-01-01T00:00:00Z") and differ only in where they close. Keying on
        # the start made this fake answer the schema probe with the past probe's history, and the
        # two schema cases below were the ones that caught it.
        if end == FIXTURE_PAST_WINDOW[1]:
            return _history([_sample(_LO + 10)] * self.past_samples, status=self.past_status)
        return _history([_sample(_LO + 10)], status=self.schema_status)

    def _get(self, _url: str, params: dict[str, str]) -> object:
        return self.filtered_pvs if "pv" in params else self.all_pvs


def _premises(client: _FakeArchiver) -> list[str]:
    return unmet_extra_premises(cast(ArchiverClient, client), "SIM:PS-01:Cur-RB")


def test_a_candidate_meeting_every_premise_reports_nothing_unmet() -> None:
    """The positive control. Without it every case below would also pass on a predicate that
    always reports something."""
    assert _premises(_FakeArchiver()) == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"archived": False}, "not_archived"),
        ({"status": ""}, "not_archived"),
        ({"found": False}, "no_type_info"),
        ({"past_samples": 1}, "past_window_not_empty"),
        ({"past_status": "withheld"}, "past_window_withheld"),
        ({"schema_status": "empty"}, "schema_window_not_ok"),
        ({"schema_status": "withheld"}, "schema_window_not_ok"),
    ],
    ids=[
        "not-archived",
        "empty-status-string",
        "no-type-record",
        "past-window-carries-a-sample",
        "past-window-unreadable",
        "schema-window-empty",
        "schema-window-withheld",
    ],
)
def test_each_live_premise_is_checked_on_its_own(kwargs: dict[str, object], expected: str) -> None:
    """One case per criterion the live suite asserts and the walk used to ignore.

    Named after the reason string rather than a number, because the reason is what a run PRINTS in
    its fail_reasons counter: a reader who sees ``past_window_not_empty`` there can find this row.
    """
    assert _premises(_FakeArchiver(**kwargs)) == [expected]  # type: ignore[arg-type]


def test_the_past_window_criterion_counts_the_RESULT_and_not_the_inside_samples() -> None:
    """⛔ The sharp edge of GQ-289, and the one a reasonable person gets wrong.

    The appliance carries the last sample from BEFORE a window into the result, so a PV whose
    archive starts before the short window answers ONE sample there while having none INSIDE it.
    The live test counts the result length, so a walk counting only the inside ones would verify a
    candidate the suite then rejects: green here, red there, which is the whole failure mode this
    change exists to remove.

    The fake below is exactly that case: one sample, carried, none inside the 2020 short window.
    """
    carried_only = _FakeArchiver(past_samples=1)

    assert count_inside([_sample(_LO + 10)], _LO, _HI) == 1  # the sample is not inside 2020
    assert _premises(carried_only) == ["past_window_not_empty"]


def test_several_failing_premises_are_all_reported_not_only_the_first() -> None:
    """A run that fixes one criterion and rediscovers the next is a run wasted, and every probe is
    already paid for by the time the first fails."""
    assert _premises(_FakeArchiver(archived=False, found=False, past_samples=2)) == [
        "not_archived",
        "no_type_info",
        "past_window_not_empty",
    ]


def test_a_failing_premise_keeps_the_candidate_out_and_names_the_reason() -> None:
    """The walk's half of the same criterion: the reason reaches fail_reasons under its own name
    instead of being folded into a generic rejection."""
    band = [_entry("SIM:PS-01:Cur-RB")]

    verified, reasons, checked = _walk(
        band, lambda _pv: _history(_GOOD), extra_premises=lambda _pv: ["not_archived"]
    )

    assert verified == []
    assert checked == 1
    assert reasons == Counter({"not_archived": 1})


def test_the_extra_premises_are_only_paid_for_by_candidates_that_passed_the_band() -> None:
    """Four GETs per candidate is a real cost over a 200-candidate walk, and a candidate the band
    already rejected cannot become a fixture whatever the premises say."""
    asked: list[str] = []

    def record(pv_name: str) -> list[str]:
        asked.append(pv_name)
        return []

    band = [_entry("SIM:PS-01:Cur-RB"), _entry("SIM:PS-02:Cur-RB")]
    # The first candidate fails the band (too few samples), the second passes it.
    histories = {"SIM:PS-01:Cur-RB": _history([]), "SIM:PS-02:Cur-RB": _history(_GOOD)}

    _walk(band, lambda pv: histories[pv], extra_premises=record)

    assert asked == ["SIM:PS-02:Cur-RB"]


def test_a_premise_probe_that_answers_unreadably_counts_as_a_response_error() -> None:
    """Same discipline the band fetch already follows: one odd PV must not abort the walk, and it
    must not be silently read as a failing premise either."""

    def raising(_pv: str) -> list[str]:
        raise ArchiverResponseError("unreadable")

    verified, reasons, checked = _walk(
        [_entry("SIM:PS-01:Cur-RB")], lambda _pv: _history(_GOOD), extra_premises=raising
    )

    assert verified == []
    assert checked == 1
    assert reasons == Counter({"response_error": 1})


@pytest.mark.parametrize(
    ("all_pvs", "filtered_pvs", "discriminates"),
    [
        (("A", "B", "C"), ("A",), True),
        (("A", "B", "C"), ("A", "B", "C"), False),
        (("A", "B", "C"), (), True),
    ],
    ids=["glob-filters", "glob-ignored", "glob-matches-nothing"],
)
def test_the_glob_predicate_reports_whether_getallpvs_really_filters(
    all_pvs: object, filtered_pvs: object, discriminates: bool
) -> None:
    """The fifth criterion. ``glob-ignored`` is the failing case the recipe used to print anyway.

    ``glob-matches-nothing`` is True on purpose and is NOT an oversight: an empty answer DOES
    differ from the unfiltered one, which is exactly what the live assertion demands. A glob that
    matches nothing is useless for other reasons, and it is not this predicate's job to say so.
    """
    client = _FakeArchiver(all_pvs=all_pvs, filtered_pvs=filtered_pvs)

    assert glob_discriminates(cast(ArchiverClient, client), "SIM:PS-01:*") is discriminates


def test_an_unreadable_past_window_is_not_an_empty_one() -> None:
    """⛔ The sharpest correction the post-build review made, and it is worth its own test.

    ``_withheld_history`` returns ``samples=[]`` by construction, so an UNREADABLE past window has
    zero samples exactly like a genuinely empty one. A check that only counts therefore verifies
    every such candidate as a good fixture, and the live suite then fails on it at an assertion
    the search never made.

    That is the same conflation of "no data" with "could not read" that GQ-290 removed from the
    archiver client one commit earlier, reappearing one layer up in the tool that consumes it.
    """
    unreadable = _FakeArchiver(past_status="withheld")
    genuinely_empty = _FakeArchiver(past_status="empty")

    # The two are indistinguishable by sample count, which is why counting is not enough.
    assert unreadable.get_pv_history("X", *FIXTURE_PAST_WINDOW)["samples"] == []
    assert genuinely_empty.get_pv_history("X", *FIXTURE_PAST_WINDOW)["samples"] == []

    assert _premises(unreadable) == ["past_window_withheld"]
    assert _premises(genuinely_empty) == []
