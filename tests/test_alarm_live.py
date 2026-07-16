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


@pytest.fixture
def configured_pv() -> str:
    """A PV that IS in the alarm tree — the positive control for the config probes."""
    value = os.environ.get("EPICS_MCP_LIVE_ALARM_CONFIGURED_PV")
    if not value:
        pytest.skip("set EPICS_MCP_LIVE_ALARM_CONFIGURED_PV to a PV present in the alarm tree")
    return value


@pytest.fixture
def alarm_tree() -> str:
    """The config-tree name as spelled on that logger (case matters — that is the point)."""
    return os.environ.get("EPICS_MCP_LIVE_ALARM_TREE", "Accelerator")


#: The per-probe event cap. Deliberately NOT raised to hide truncation: a higher cap only moves the
#: threshold (a busier facility binds at 100 too). The guard is ``capped is False`` in the test that
#: compares — raising alone would relocate the blindness, not remove it.
_MAX_EVENTS = 5

#: The message for a failed positive control. It must NOT claim "your data is stale": an empty
#: reference also comes from auth, a wrong URL, a client regression or a changed payload. Naming a
#: single cause would be the same overclaim this file exists to catch.
_NO_REFERENCE = (
    "positive control not met; the comparison is not evaluable. Check fixture, config, backend "
    "and client — an empty reference cannot tell a honoured window from a dropped one."
)


def _events(
    client: AlarmClient, pv: str, start: str, end: str = "now"
) -> tuple[list[dict[str, object]], bool]:
    """Return ``(events, capped)`` — the cap flag is returned, never swallowed.

    ``get_alarm_history`` answers newest-first and truncates at *max_events*. A caller that keeps
    only ``len(...)`` compares ``min(n, cap)`` on both sides: two windows that differ ONLY in their
    older tail then present the identical newest page and read as equal. The flag is the signal
    that says the answer is the cap rather than the window.
    """
    events, capped = client.get_alarm_history(pv, start=start, end=end, max_events=_MAX_EVENTS)
    return events, capped


def _count(client: AlarmClient, pv: str, start: str, end: str = "now") -> int:
    return len(_events(client, pv, start, end)[0])


def _identities(events: list[dict[str, object]]) -> list[tuple[str, str]]:
    """Identify events by ``(message_time, pv)`` — the field the server actually filters on.

    Comparing identities rather than counts means a window that returns the right NUMBER of the
    wrong events cannot pass.
    """
    return sorted((str(e.get("message_time")), str(e.get("pv"))) for e in events)


def test_relative_window_finds_events(client: AlarmClient, pv: str) -> None:
    """The baseline — without events the probes below would prove nothing."""
    assert _count(client, pv, "7 days"), f"no alarm history for {pv!r}: pick a PV that has some"


def test_naive_iso_window_is_honoured(client: AlarmClient, pv: str) -> None:
    """THE regression: a zone-less ISO returned 0 for a window that held 20+ WHEN IT WAS MEASURED.

    That 20-vs-0 is the historical measurement, not what this test sees — it compares the two
    forms against whatever the fixture holds now. Both guards below are load-bearing and were
    missing: without the reference guard an aged-out window makes this ``0 == 0`` — green, and
    silently no longer a test (measured: it was exactly that for the twelve days a sandbox window
    held no events). Without the cap guard both sides read as the cap.
    """
    with_zone, zone_capped = _events(client, pv, "2026-07-08T12:45:58Z")
    naive, naive_capped = _events(client, pv, "2026-07-08T12:45:58")

    assert with_zone, _NO_REFERENCE
    assert not (zone_capped or naive_capped), (
        f"the cap truncated the comparison (max_events={_MAX_EVENTS}): a difference beyond the "
        "newest page would be invisible. Narrow EPICS_MCP_LIVE_ALARM_PV to a single PV."
    )
    assert _identities(naive) == _identities(with_zone)


def test_past_window_returns_nothing(client: AlarmClient, pv: str) -> None:
    """The negative control, and it is not optional: every test above would ALSO pass if the
    window were silently dropped and the whole history searched."""
    assert _count(client, pv, *_PAST) == 0


def test_misread_amounts_rejected_before_any_request(client: AlarmClient, pv: str) -> None:
    """'500 millis' is the sharpest: live it RETURNS data — for a 500-MINUTE window."""
    for bad in ("500 millis", "5 m", "garbage", "1 year"):
        with pytest.raises(TimeWindowFormatError):
            client.get_alarm_history(pv, start=bad, end="now")


def test_unmatched_pv_returns_nothing(client: AlarmClient) -> None:
    """The pv filter's negative control: without it, 'the filter works' and 'the filter is
    ignored and you got the whole history' look the same."""
    assert _count(client, "ZZZ-no-such-pv", "7 days") == 0


def test_alarm_tree_name_is_case_sensitive(
    client: AlarmClient, configured_pv: str, alarm_tree: str
) -> None:
    """THE regression: the server lower-cases config_name to pick the ES index but matches the
    wildcard CASE-PRESERVED against a keyword field, so a mis-cased tree selects the right index
    and matches nothing — reporting exactly like an unconfigured PV. Must be withheld, not False.
    """
    configured, _ = client.is_alarm_configured(configured_pv, config_name=alarm_tree)
    assert configured is True, f"positive control failed: {configured_pv!r} not in {alarm_tree!r}"

    miscased, _ = client.is_alarm_configured(configured_pv, config_name=alarm_tree.lower())
    assert miscased is None  # withheld — NOT False, which is what it used to report


def test_unknown_alarm_tree_is_withheld(client: AlarmClient, configured_pv: str) -> None:
    """An unknown tree is quiet too: the index pattern ends in '*', so Elasticsearch answers
    200 + [] instead of index_not_found."""
    configured, _ = client.is_alarm_configured(configured_pv, config_name="ZZZNoSuchTree")
    assert configured is None


def test_unconfigured_pv_in_a_real_tree_is_still_false(
    client: AlarmClient, alarm_tree: str
) -> None:
    """The other half — the tree probe must not turn every miss into 'withheld'. A live tree
    plus an absent PV is a REAL negative and has to stay False, or the fix would buy honesty by
    never answering."""
    configured, _ = client.is_alarm_configured("ZZZ-no-such-pv", config_name=alarm_tree)
    assert configured is False


# --- S11 schema anchor: the strict client schema, pinned against the REAL payload ---


def test_live_history_satisfies_the_strict_schema(client: AlarmClient, pv: str) -> None:
    """S11 anchor: the record schema (every doc a dict carrying a string ``config`` — measured
    2026-07-16 on BOTH doc types, state: and config:) was derived from this live payload. The
    client now RAISES on anything else, so this run passing IS the proof the real payload matches;
    the explicit per-event assert pins the projected surface too. Goes red if a logger version
    stops matching — then the schema is re-measured, never loosened blindly."""
    events, _capped = client.get_alarm_history(pv, "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
    assert events, (
        "positive control not met: no alarm docs for the fixture PV in a 2020-2030 window — "
        "the schema anchor cannot pin anything. Check EPICS_MCP_LIVE_ALARM_PV."
    )
    assert all(isinstance(event.get("config"), str) and event["config"] for event in events)
