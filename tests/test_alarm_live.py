"""Live probes for the Alarm Logger time window — the class of bug a mock CANNOT catch.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_ALARM_URL`` and ``EPICS_MCP_LIVE_ALARM_PV`` set.

WHY THESE EXIST
---------------
An earlier assessment cleared this plane by reading the source: the Alarm Logger depends on the
real ``core-util`` parser (no vendored copy), so ISO works — true, and it stopped there. Measured,
the same window with and without a zone returned 20+ events vs **0**. The zone-less form matches no
parser and degrades to *now*; the window collapses and the answer is a well-formed empty list. A
mock cannot see this: it only ever knows what the client SENT, never what the server HONOURED.

The assertions are DIFFERENTIAL — the same window expressed several ways must give the same answer —
plus a negative control, because "it returns something" would also pass if the window were dropped
entirely.
"""

from __future__ import annotations

import os

import pytest

from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.alarm_client import AlarmClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("EPICS_MCP_ALARM_URL") and os.environ.get("EPICS_MCP_LIVE_ALARM_PV")),
        reason=(
            "live Alarm probe: set EPICS_MCP_ALARM_URL and EPICS_MCP_LIVE_ALARM_PV "
            "(a PV/substring with alarm history on that logger)"
        ),
    ),
]

# A window far in the past — the negative control. Fixed, not clock-derived, so runs are
# reproducible.
_PAST = ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")


@pytest.fixture
def client() -> AlarmClient:
    return AlarmClient(os.environ["EPICS_MCP_ALARM_URL"], timeout=15.0)


@pytest.fixture
def pv() -> str:
    return os.environ["EPICS_MCP_LIVE_ALARM_PV"]


def _count(client: AlarmClient, pv: str, start: str, end: str = "now") -> int:
    return len(client.get_alarm_history(pv, start=start, end=end, max_events=5)[0])


def test_relative_window_finds_events(client: AlarmClient, pv: str) -> None:
    """The baseline — without events the probes below would prove nothing."""
    assert _count(client, pv, "7 days"), f"no alarm history for {pv!r}: pick a PV that has some"


def test_naive_iso_window_is_honoured(client: AlarmClient, pv: str) -> None:
    """THE regression: a zone-less ISO returned 0 for a window holding 20+. One character."""
    assert _count(client, pv, "2026-07-08T12:45:58") == _count(client, pv, "2026-07-08T12:45:58Z")


def test_past_window_returns_nothing(client: AlarmClient, pv: str) -> None:
    """The negative control, and it is not optional: every test above would ALSO pass if the
    window were silently dropped and the whole history searched."""
    assert _count(client, pv, *_PAST) == 0


def test_misread_amounts_rejected_before_any_request(client: AlarmClient, pv: str) -> None:
    """'500 millis' is the sharpest: live it RETURNS data — for a 500-MINUTE window."""
    for bad in ("500 millis", "5 m", "garbage", "1 year"):
        with pytest.raises(TimeWindowFormatError):
            client.get_alarm_history(pv, start=bad, end="now")
