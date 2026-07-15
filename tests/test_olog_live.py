"""Live probes for the Olog search time window — the class of bug a mock CANNOT catch.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_OLOG_URL`` pointing at a reachable Olog.

WHY THESE EXIST
---------------
The time window was broken from the day it shipped and no offline test could have known: the drop
happens on the SERVER. A mock only ever sees what the client SENT, never what the server HONOURED,
so a fully-mocked suite reports green while `search_logbook` answers "no entries" to a window
containing everything. Two reviewers read the Java and drew opposite (both wrong) conclusions; only
execution settled it.

The assertions are DIFFERENTIAL — the same window expressed several ways must give the same answer —
rather than exact counts, so they hold against any Olog with any data. The windows are fixed
constants, not derived from the clock, so a run is reproducible.
"""

from __future__ import annotations

import os

import pytest

from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.olog_client import OlogClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("EPICS_MCP_OLOG_URL"),
        reason="live Olog probe: set EPICS_MCP_OLOG_URL (e.g. the phoebus-olog compose)",
    ),
]

# A window wide enough to contain any sandbox entry, expressed two ways. Fixed, not clock-derived.
_WIDE_ISO = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
_WIDE_WALL = ("2020-01-01 00:00:00.000", "2030-01-01 00:00:00.000")
# A window that predates any sandbox entry — the negative control.
_PAST = ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")


@pytest.fixture
def client() -> OlogClient:
    url = os.environ["EPICS_MCP_OLOG_URL"]
    return OlogClient(url, timeout=10.0)


def _hits(client: OlogClient, start: str, end: str) -> int | None:
    return client.search_logbook(start=start, end=end, size=200)[2]


def test_iso_window_is_honoured(client: OlogClient) -> None:
    """THE regression. Sent raw, this exact window returned 0 of 9 entries — a plausible empty
    answer, not an error. It must now return the logbook's actual content."""
    total = _hits(client, *_WIDE_ISO)
    assert total, "an ISO window spanning 2020-2030 returned nothing — the window is being dropped"


def test_iso_and_wall_clock_windows_agree(client: OlogClient) -> None:
    """The decisive differential: the same window in two notations must give the same answer.

    Stronger than 'ISO returns something' — it pins that ISO is honoured FAITHFULLY, not merely
    that the request survives.
    """
    assert _hits(client, *_WIDE_ISO) == _hits(client, *_WIDE_WALL)


def test_relative_window_agrees_with_absolute(client: OlogClient) -> None:
    """A wide relative amount must find what a wide absolute window finds."""
    relative = client.search_logbook(start="3650 days", size=200)[2]
    assert relative == _hits(client, *_WIDE_ISO)


def test_past_window_returns_nothing(client: OlogClient) -> None:
    """The negative control, and it is not optional: every test above would ALSO pass if we
    silently dropped the window entirely and searched everything. This is what proves the window
    is applied rather than ignored."""
    assert _hits(client, *_PAST) == 0


def test_narrow_window_discriminates(client: OlogClient) -> None:
    """A narrow window must be a strict subset of a wide one — the filter is time-accurate, not
    merely present."""
    narrow = client.search_logbook(start="1 hour", size=200)[2]
    wide = _hits(client, *_WIDE_ISO)
    assert narrow is not None and wide is not None
    assert narrow <= wide


def test_year_amount_rejected_before_any_request(client: OlogClient) -> None:
    """Olog cannot subtract years from a point in time. Left to the server this is a 400 that an
    anonymous read only ever sees as 401 ('unauthorized') — so it is refused here instead."""
    with pytest.raises(TimeWindowFormatError, match="days or weeks"):
        client.search_logbook(start="1 year")
