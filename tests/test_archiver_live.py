"""Live probes for the Archiver time window + the getPVsForThisAppliance filter premise.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_ARCHIVER_URL``, ``EPICS_MCP_LIVE_ARCHIVER_PV`` and
``EPICS_MCP_LIVE_ARCHIVER_GLOB`` set. Those last two are test-only and deliberately absent from
``EpicsConfig`` and the operator guide — ``test_guide_matches_code`` checks every ``EPICS_MCP_*``
token in the guide against the config and would go red.

WHY THESE EXIST
---------------
Two sibling planes shipped a silent time-window bug that no mocked test could see: a mock knows
only what the client SENT, never what the server HONOURED. The same blind spot hides a LOUD
rejection just as well — this plane 500s on four notations a caller might reasonably send, and the
offline suite was green throughout (it passed "a"/"b" as the window).
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.archiver_client import ArchiverClient, HistoryResult, Sample

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (
            os.environ.get("EPICS_MCP_ARCHIVER_URL")
            and os.environ.get("EPICS_MCP_LIVE_ARCHIVER_PV")
            and os.environ.get("EPICS_MCP_LIVE_ARCHIVER_GLOB")
        ),
        reason=(
            "live Archiver probe: set EPICS_MCP_ARCHIVER_URL, EPICS_MCP_LIVE_ARCHIVER_PV "
            "(an archived PV with history) and EPICS_MCP_LIVE_ARCHIVER_GLOB (a name glob "
            "matching some archived PVs)"
        ),
    ),
]

# A window far in the past — the negative control. Fixed, not clock-derived, so runs reproduce.
_PAST = ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")


@pytest.fixture
def client() -> ArchiverClient:
    # A split deployment serves MGMT and RETRIEVAL on different ports, so the retrieval root must
    # be honoured when configured — passing only the MGMT root makes every history probe a 404.
    return ArchiverClient(
        os.environ["EPICS_MCP_ARCHIVER_URL"],
        timeout=30.0,
        retrieval_url=os.environ.get("EPICS_MCP_ARCHIVER_RETRIEVAL_URL") or None,
    )


@pytest.fixture
def pv() -> str:
    return os.environ["EPICS_MCP_LIVE_ARCHIVER_PV"]


#: The per-probe sample cap. Deliberately NOT raised to paper over truncation: a higher cap only
#: moves the threshold. The guard is ``capped is False`` in the test that compares.
_MAX_POINTS = 50

#: The message for a failed positive control. It must NOT claim "your data is stale": an empty
#: reference also comes from auth, a wrong URL, a client regression, a changed payload — or a
#: ``withheld`` status, which means the history is UNKNOWN, not proven empty.
_NO_REFERENCE = (
    "positive control not met; the comparison is not evaluable. Check fixture, config, backend "
    "and client — an empty reference cannot tell a honoured window from a dropped one."
)


def _history(client: ArchiverClient, pv: str, start: str, end: str) -> HistoryResult:
    return client.get_pv_history(pv, start, end, max_points=_MAX_POINTS)


def _inside_window(samples: list[Sample], start: str, end: str) -> int:
    """How many samples fall STRICTLY inside ``[start, end]`` — the appliance also returns the last
    value from BEFORE *start*, and that carried sample is present whatever window you ask for."""
    lo = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    hi = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
    return sum(1 for s in samples if lo <= s["secs"] <= hi)


def _count(client: ArchiverClient, pv: str, start: str, end: str) -> int:
    return len(_history(client, pv, start, end)["samples"])


def _window() -> tuple[str, str]:
    """A wide, fixed absolute window that should hold samples for any long-archived PV."""
    return ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")


def test_absolute_window_finds_samples(client: ArchiverClient, pv: str) -> None:
    """The baseline — without samples every probe below proves nothing."""
    assert _count(client, pv, *_window()), (
        f"no archived history for {pv!r}: pick a PV that has some"
    )


@pytest.mark.parametrize(
    "start",
    [
        "2026-01-01T00:00:00",  # naive ISO — HTTP 500 unnormalized
        "2026-01-01 00:00:00",  # the wall clock Olog requires — HTTP 500 unnormalized
        "2026-01-01",  # bare date — HTTP 500 unnormalized
    ],
)
def test_sibling_notations_agree_with_iso_z(client: ArchiverClient, pv: str, start: str) -> None:
    """The differential: every notation denotes the same instant and must give the same answer.

    Each of these is an HTTP 500 without the normalization — surfaced, until today, as
    'the Archiver is unreachable'.

    The three guards are load-bearing and were missing. Without the reference guard an aged-out
    window makes this ``0 == 0`` — green, and no longer a test. Without the cap guard both sides
    read as the cap rather than the window. And ``status`` must be ``ok``: the client separates
    ``withheld`` ("the response could not be interpreted") from ``empty`` ("genuinely no samples")
    precisely so a caller cannot read the first as the second — reading only ``["samples"]``
    throws that distinction away and would let an uninterpretable response pass as agreement.
    """
    reference = _history(client, pv, *_window())
    sibling = _history(client, pv, start, "2027-01-01T00:00:00Z")

    for label, result in (("reference", reference), (f"{start!r}", sibling)):
        assert result["status"] == "ok", (
            f"{label}: status={result['status']!r} — the history is unknown, not proven empty; "
            "this comparison is not evaluable"
        )
        assert not result["capped"], (
            f"{label}: the cap truncated the comparison (max_points={_MAX_POINTS}) — a difference "
            "beyond it would be invisible"
        )
    assert reference["samples"], _NO_REFERENCE
    # "Has samples" is NOT "has samples in the window": the appliance carries the last value from
    # BEFORE the window start into the result. A slow PV therefore answers exactly one (carried)
    # sample for EVERY start, and the comparison degenerates to 1 == 1 — green for any window at
    # all. Measured across 24 archived PVs: n minus inside == 1 in every case, and 5 of them had
    # n=1/inside=0. Demand a reference that genuinely spans the window.
    inside = _inside_window(reference["samples"], *_window())
    assert inside >= 2, (
        f"the reference holds {len(reference['samples'])} sample(s) but only {inside} inside the "
        "window — the appliance carries the last value from before the start, so this PV cannot "
        "discriminate between windows. Pick a PV with several samples in the window "
        "(EPICS_MCP_LIVE_ARCHIVER_PV)."
    )
    assert len(sibling["samples"]) == len(reference["samples"])


def test_past_window_returns_nothing(client: ArchiverClient, pv: str) -> None:
    """The negative control, and it is not optional: the test above would ALSO pass if the window
    were dropped and the whole history returned."""
    assert _count(client, pv, *_PAST) == 0


def test_relative_amount_refused_before_any_request(client: ArchiverClient, pv: str) -> None:
    """'7 days' is valid on the alarm/logbook planes and an HTTP 500 here."""
    with pytest.raises(TimeWindowFormatError, match="only an absolute time"):
        client.get_pv_history(pv, "7 days", "now")


def test_this_appliance_endpoint_still_has_no_name_filter(client: ArchiverClient) -> None:
    """THE premise guard for the list_archived_pvs refusal — it measures the SERVER, not us.

    `list_archived_pvs` refuses pattern + this_appliance because getPVsForThisAppliance ignores
    every name filter. A refusal is only correct while its premise holds: if a future appliance
    starts honouring `pv`, this goes red and tells us the refusal can be lifted.

    Our client has no method that sends `pv` to that endpoint (by design — forwarding an ignored
    filter is what caused the bug), so this reaches for the raw endpoint deliberately.
    """
    glob = os.environ["EPICS_MCP_LIVE_ARCHIVER_GLOB"]
    mgmt = f"{client.base_url}/mgmt/bpl"
    unfiltered = client._get(f"{mgmt}/getPVsForThisAppliance", {"limit": "5"})
    filtered = client._get(f"{mgmt}/getPVsForThisAppliance", {"limit": "5", "pv": glob})
    assert filtered == unfiltered, (
        "getPVsForThisAppliance now honours a pv filter — the list_archived_pvs refusal is no "
        "longer needed and should be replaced by forwarding the glob"
    )
    # The sibling endpoint DOES filter — the contrast is what makes the refusal (rather than a
    # blanket 'no filtering here') the right call.
    by_name = client._get(f"{mgmt}/getAllPVs", {"limit": "5", "pv": glob})
    assert by_name != unfiltered
