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
from datetime import UTC, datetime

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

#: The message for a failed positive control. It must NOT claim "your data is stale": an empty
#: reference also comes from auth, a wrong URL, a client regression or a changed payload — and a
#: ``total`` of ``None`` means the count could not be read at all, which is NOT "zero entries".
_NO_REFERENCE = (
    "positive control not met; the comparison is not evaluable. Check fixture, config, backend "
    "and client — an empty (or unreadable) reference cannot tell a honoured window from a "
    "dropped one."
)


@pytest.fixture
def client() -> OlogClient:
    url = os.environ["EPICS_MCP_OLOG_URL"]
    return OlogClient(url, timeout=10.0)


def _hits(client: OlogClient, start: str, end: str) -> int | None:
    return client.search_logbook(start=start, end=end, size=200)[2]


def _iso_from_ms(epoch_ms: int) -> str:
    """Epoch milliseconds → the ISO-Z form the search takes. Derived from data, never the clock."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_iso_window_is_honoured(client: OlogClient) -> None:
    """THE regression. Sent raw, this exact window returned 0 of 9 entries — a plausible empty
    answer, not an error. It must now return the logbook's actual content."""
    total = _hits(client, *_WIDE_ISO)
    assert total, "an ISO window spanning 2020-2030 returned nothing — the window is being dropped"


def test_iso_and_wall_clock_windows_agree(client: OlogClient) -> None:
    """The decisive differential: the same window in two notations must give the same answer.

    Stronger than 'ISO returns something' — it pins that ISO is honoured FAITHFULLY, not merely
    that the request survives.

    The guard is load-bearing: ``_hits`` returns ``int | None``, so without it this was green both
    on an empty logbook (``0 == 0``) and on an unreadable count (``None == None``) — two very
    different facts, neither of them "the notations agree".
    """
    iso, wall = _hits(client, *_WIDE_ISO), _hits(client, *_WIDE_WALL)
    assert iso, _NO_REFERENCE
    assert wall is not None, _NO_REFERENCE
    assert iso == wall


def test_relative_window_agrees_with_absolute(client: OlogClient) -> None:
    """A wide relative amount must find what a wide absolute window finds.

    HONEST LIMIT — read before trusting this: the two windows are NOT the same instant ('3650
    days' is clock-relative, the absolute one is fixed) and on a young logbook both simply contain
    EVERY entry, which is why they agree. So this pins that a relative amount PARSES and is not
    rejected — it cannot see a relative window that is dropped entirely, because searching
    everything returns the same total. The boundary itself is pinned by
    test_narrow_window_discriminates; that is where a dropped window goes red.
    """
    relative = client.search_logbook(start="3650 days", size=200)[2]
    absolute = _hits(client, *_WIDE_ISO)
    assert absolute, _NO_REFERENCE
    assert relative is not None, _NO_REFERENCE
    assert relative == absolute


def test_past_window_returns_nothing(client: OlogClient) -> None:
    """The negative control, and it is not optional: every test above would ALSO pass if we
    silently dropped the window entirely and searched everything. This is what proves the window
    is applied rather than ignored."""
    assert _hits(client, *_PAST) == 0


def test_narrow_window_discriminates(client: OlogClient) -> None:
    """A window starting at the NEWEST entry must exclude the older ones — the filter is
    time-accurate, not merely present. This is the test that goes red on a dropped window.

    Two things here are deliberate:

    * ``<`` not ``<=``. The old ``narrow <= wide`` was satisfied by a window that was ignored
      ENTIRELY (both sides return the whole logbook, and ``9 <= 9`` is green) and by an empty
      logbook (``0 <= 0``). It asserted nothing a dropped filter would violate.
    * The boundary is DERIVED FROM THE DATA, not guessed. A hardcoded date passes or fails by luck
      of the fixture, and the old ``"1 hour"`` conflated two causes: a window that is ignored and a
      logbook whose entries are all younger than an hour look identical.
    """
    entries, _, wide = client.search_logbook(start=_WIDE_ISO[0], end=_WIDE_ISO[1], size=200)
    assert wide, _NO_REFERENCE

    stamps = [int(str(e["createdDate"])) for e in entries if e.get("createdDate") is not None]
    assert stamps, "entries carry no createdDate — cannot derive a boundary from the data"
    narrow = _hits(client, _iso_from_ms(max(stamps)), _WIDE_ISO[1])
    assert narrow is not None, _NO_REFERENCE
    assert narrow < wide, (
        f"a window starting at the newest entry returned {narrow} of {wide} — either the window is "
        "ignored, or the newest timestamp is shared by every entry the window would have excluded "
        "(a fixture with no time spread cannot discriminate)"
    )


def test_year_amount_rejected_before_any_request(client: OlogClient) -> None:
    """Olog cannot subtract years from a point in time. Left to the server this is a 400 that an
    anonymous read only ever sees as 401 ('unauthorized') — so it is refused here instead."""
    with pytest.raises(TimeWindowFormatError, match="days or weeks"):
        client.search_logbook(start="1 year")


def _order(client: OlogClient, sort: str) -> list[object]:
    return [e.get("id") for e in client.search_logbook(size=5, sort=sort)[0]]


def test_sort_orders_the_page(client: OlogClient) -> None:
    """The positive control: without it, the probe below could not tell 'sort was applied' from
    'sort does nothing at all'."""
    down, up = _order(client, "down"), _order(client, "up")
    assert down, "no entries — the sort probes below would prove nothing"
    assert down != up


def test_unreadable_sort_silently_reverses_on_the_server(client: OlogClient) -> None:
    """The reason the tool constrains sort to a Literal, measured rather than assumed.

    Olog does not reject an order it cannot read: anything that is not 'down'/'desc' becomes
    ASC — the REVERSE of our documented default — behind a 200 and a well-formed page. 'newest'
    is the sharpest case: the word an operator would reach for to mean newest-first returns
    oldest-first.

    This measures the SERVER, not us: the tool layer rejects these values before they are sent
    (Literal['down','up']), so this asks the client directly. Should a future Olog reject them
    itself, this test goes red and says the constraint may be relaxed — a refusal is only correct
    while its premise holds.

    The inline positive control is not decoration. This test is cited in CLAUDE.md as the model of
    "pin the premise so it goes red when the server improves" — and it could not go red at all: on
    an empty logbook every ``_order`` is ``[]`` and ``[] == []`` passes. The control lived in the
    NEIGHBOURING test, which pytest runs independently, so a red neighbour never stopped this one
    from reporting green. A guard that cannot fail is not a guard.
    """
    down, up = _order(client, "down"), _order(client, "up")
    assert down, "no entries — this probe cannot distinguish any sort order"
    assert down != up, "sort has no effect at all — an unreadable value cannot be shown to collapse"
    for unreadable in ("garbage", "newest", "asc", ""):
        assert _order(client, unreadable) == up, f"{unreadable!r} no longer collapses to ASC"
