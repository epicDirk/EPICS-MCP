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

import pytest

from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.archiver_client import ArchiverClient

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


def _count(client: ArchiverClient, pv: str, start: str, end: str) -> int:
    return len(client.get_pv_history(pv, start, end, max_points=50)["samples"])


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
    """
    assert _count(client, pv, start, "2027-01-01T00:00:00Z") == _count(client, pv, *_window())


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
