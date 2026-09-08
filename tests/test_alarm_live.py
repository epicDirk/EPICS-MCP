"""Live probes for the Alarm Logger time window, the class of bug a mock CANNOT catch.

Opt-in: ``pytest tests/test_alarm_live.py -m live`` with ``EPICS_MCP_ALARM_URL`` and
``EPICS_MCP_LIVE_ALARM_PV`` set. The config-tree probes also need
``EPICS_MCP_LIVE_ALARM_CONFIGURED_PV`` (a PV in the alarm tree, the positive control; without
it those two tests skip) and, if the tree is not "Accelerator", ``EPICS_MCP_LIVE_ALARM_TREE``.

WHY THESE EXIST
---------------
An earlier assessment cleared this plane by reading the source: the Alarm Logger depends on the
real ``core-util`` parser (no vendored copy), so ISO works, true, and it stopped there. Measured,
the same window with and without a zone returned 20+ events vs **0**. The zone-less form matches no
parser and degrades to *now*; the window collapses and the answer is a well-formed empty list. A
mock cannot see this: it only ever knows what the client SENT, never what the server HONOURED.

The assertions are DIFFERENTIAL: the same window expressed several ways must give the same answer,
plus a negative control, because "it returns something" would also pass if the window were dropped
entirely.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from epics_mcp.services._time_window import (
    TimeWindowFormatError,
    classify_time_value,
    format_iso_z,
)
from epics_mcp.services.alarm_client import AlarmClient
from tests.live_gate import assert_live_available, live_demanded

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is
    demanded (EPICS_MCP_REQUIRE_LIVE=1) and the plane is not configured."""
    assert_live_available(
        bool(os.environ.get("EPICS_MCP_ALARM_URL") and os.environ.get("EPICS_MCP_LIVE_ALARM_PV")),
        "live Alarm probe: set EPICS_MCP_ALARM_URL and EPICS_MCP_LIVE_ALARM_PV "
        "(a PV/substring with alarm history on that logger)",
        demanded=live_demanded(os.environ),
    )


# A window far in the past: the negative control. Fixed, not clock-derived, so runs are
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
    """A PV that IS in the alarm tree, the positive control for the config probes."""
    value = os.environ.get("EPICS_MCP_LIVE_ALARM_CONFIGURED_PV")
    assert_live_available(
        bool(value),
        "set EPICS_MCP_LIVE_ALARM_CONFIGURED_PV to a PV present in the alarm tree",
        demanded=live_demanded(os.environ),
    )
    assert value is not None  # narrowed by the gate above
    return value


@pytest.fixture
def alarm_tree() -> str:
    """The config-tree name as spelled on that logger (case matters, that is the point)."""
    return os.environ.get("EPICS_MCP_LIVE_ALARM_TREE", "Accelerator")


#: The per-probe event cap. Deliberately NOT raised to hide truncation: a higher cap only moves the
#: threshold (a busier facility binds at 100 too). The guard is ``capped is False`` in the test that
#: compares, raising alone would relocate the blindness, not remove it.
_MAX_EVENTS = 5

#: The anchor probe's page size, and with it the width of the comparison window below. That
#: window is derived from this page instead of from a date, so it is AT MOST one page wide
#: whatever the facility's event rate happens to be (fewer, when the week holds fewer).
_ANCHOR_PAGE = 20

#: The comparison probe's OWN cap, ten times the page. The window is bounded by construction but
#: not exactly: every event sharing a boundary timestamp comes along, and this logger emits
#: repeats (measured 2026-09-04 against a real ESS alarm logger: 94 events over 27 distinct
#: message_time values). The MAXIMUM multiplicity is not measured, so this factor is margin and
#: not proof, which is why the cap guard below stays the thing that says so out loud.
_COMPARISON_MAX_EVENTS = _ANCHOR_PAGE * 10

#: How far outside the page's own timestamps the window boundaries sit, so the probe does not
#: depend on whether this logger reads its range inclusively; that is NOT measured here, only
#: the client's docstring says ``[start, end]``. TWO server properties are traded for one: the
#: shift only buys the independence if the server compares its range to the MILLISECOND, which
#: is equally unmeasured. It fails in the safe direction either way, because both queries carry
#: the same shifted bounds: a coarser server loses the same events on both sides and the
#: comparison stays valid, at worst the reference guard fires. One millisecond is what the wire
#: format can express, ``format_iso_z`` renders exactly three fractional digits.
_EDGE = timedelta(milliseconds=1)

#: The message for a failed positive control. It must NOT claim "your data is stale": an empty
#: reference also comes from auth, a wrong URL, a client regression or a changed payload. Naming a
#: single cause would be the same overclaim this file exists to catch.
_NO_REFERENCE = (
    "positive control not met; the comparison is not evaluable. Check fixture, config, backend "
    "and client, an empty reference cannot tell a honoured window from a dropped one."
)


def _events(
    client: AlarmClient,
    pv: str,
    start: str,
    end: str = "now",
    *,
    max_events: int = _MAX_EVENTS,
) -> tuple[list[dict[str, object]], bool]:
    """Return ``(events, capped)``, the cap flag is returned, never swallowed.

    ``get_alarm_history`` answers newest-first and truncates at *max_events*. A caller that keeps
    only ``len(...)`` compares ``min(n, cap)`` on both sides: two windows that differ ONLY in their
    older tail then present the identical newest page and read as equal. The flag is the signal
    that says the answer is the cap rather than the window.

    *max_events* is a parameter rather than the constant it defaults to because exactly ONE probe
    needs a different cap, and it states its reason at its own call site. Every other caller keeps
    ``_MAX_EVENTS``.
    """
    events, capped = client.get_alarm_history(pv, start=start, end=end, max_events=max_events)
    return events, capped


def _count(client: AlarmClient, pv: str, start: str, end: str = "now") -> int:
    return len(_events(client, pv, start, end)[0])


def _identities(events: list[dict[str, object]]) -> list[tuple[str, str]]:
    """Identify events by ``(message_time, pv)``, the field the server actually filters on.

    Comparing identities rather than counts means a window that returns the right NUMBER of the
    wrong events cannot pass.
    """
    return sorted((str(e.get("message_time")), str(e.get("pv"))) for e in events)


def instant_of(message_time: object) -> datetime:
    """The UTC instant an event's ``message_time`` names, or a LOUD failure.

    Read through the client's own classifier rather than a second time parser: a probe that
    invented its own way of reading a timestamp could disagree with the code it exists to test,
    and nothing would show it. One consequence is deliberate rather than accidental: a raw epoch
    number is REFUSED, because ``classify_time_value`` refuses it. One fixture in
    ``tests/test_alarm.py`` carries that shape, though on a config document rather than on a
    history event this function ever sees; the real logger measured on 2026-09-04 answers with
    zone-explicit ISO (``2026-08-25T09:44:16.935Z``). The refusal follows from reusing the
    client's classifier, and an epoch parser beside it would be a second time-reading truth in
    one repository.

    Shared with ``tests/test_alarm.py``, which drives this probe offline in both directions.
    """
    moment = classify_time_value(str(message_time), param="message_time")
    assert moment is not None, (
        f"message_time={message_time!r} classified as a relative amount, which no timestamp is"
    )
    return moment


def spellings_of(moment: datetime) -> tuple[str, str]:
    """One instant in the two ISO spellings this probe compares: with the zone, and without it.

    The naive form is cut from the ALREADY normalised UTC rendering, never from the raw server
    string. That ``message_time`` always carries ``Z`` is pinned nowhere in this repository, and
    truncating an offset form (``+02:00``) would name a DIFFERENT instant and turn the comparison
    red for a reason that is not the regression it watches.
    """
    zoned = format_iso_z(moment)
    return zoned, zoned.removesuffix("Z")


def test_relative_window_finds_events(client: AlarmClient, pv: str) -> None:
    """The baseline, without events the probes below would prove nothing."""
    assert _count(client, pv, "7 days"), f"no alarm history for {pv!r}: pick a PV that has some"


def test_naive_iso_window_is_honoured(client: AlarmClient, pv: str) -> None:
    """THE regression: a zone-less ISO returned 0 for a window that held 20+ WHEN IT WAS MEASURED.

    That 20-vs-0 is the historical measurement, not what this test sees; it compares the two
    forms against whatever the fixture holds now. Both guards below are load-bearing and were
    missing once: without the reference guard an aged-out window makes this ``0 == 0``, green,
    and silently no longer a test (measured: it was exactly that for the twelve days a sandbox
    window held no events). Without the cap guard both sides read as the cap.

    THE WINDOW IS DERIVED AND CLOSED, and both halves of that are repairs (GQ-288, 2026-09-08):

    * DERIVED. It used to start at a FIXED July timestamp and to demand at most five events
      since, while three other probes demand activity in the last seven days from the same
      value. Nothing satisfied both any more: measured 2026-09-04, across the two alarm trees a
      999-capped week query could see through, not one of 19 PVs met the pair, and the fixed
      start moved the bar further every day. The anchor now comes from the newest page itself,
      so the window is at most one page wide whatever the rate is, and the only thing the
      fixture still has to be is the thing the baseline probe above already asks of it.
    * CLOSED. Both queries used to end at ``now``, and the second ``now`` is later than the
      first: an event arriving between them shows up on one side only and the comparison goes
      red without a defect. A fixed end cannot race.

    What a green run does and does NOT say, because the client normalises before sending: both
    spellings reach the server as ONE string today, so a green run says the two spellings
    produce the same answer. THAT the normalisation is what makes them identical is held by
    ``tests/test_alarm.py``, which asserts it on the recorded wire values; this probe cannot
    tell that apart from a server reading a naive form correctly. Take the normalisation out
    and the naive form goes out raw, the server takes it as *now*, the answer is empty, and
    this comparison is what notices, which the same offline module drives in both directions.
    """
    page, _ = _events(client, pv, "7 days", max_events=_ANCHOR_PAGE)
    assert page, _NO_REFERENCE

    # min/max rather than page[0]/page[-1]: newest-first is a property of the SERVER (the
    # client neither sorts nor asks for a sort order), and an inverted window would report a
    # missing positive control instead of an ordering change. `.get` rather than `[...]` for
    # the same reason: the projection allowlist makes message_time optional, and instant_of
    # exists to say so loudly rather than to be overtaken by a KeyError at the call site.
    moments = [instant_of(event.get("message_time")) for event in page]
    zoned_start, naive_start = spellings_of(min(moments) - _EDGE)
    end = format_iso_z(max(moments) + _EDGE)

    with_zone, zone_capped = _events(
        client, pv, zoned_start, end, max_events=_COMPARISON_MAX_EVENTS
    )
    naive, naive_capped = _events(client, pv, naive_start, end, max_events=_COMPARISON_MAX_EVENTS)

    assert with_zone, _NO_REFERENCE
    assert not (zone_capped or naive_capped), (
        f"the cap truncated the comparison (max_events={_COMPARISON_MAX_EVENTS}): a difference "
        "beyond the newest page would be invisible. The window spans one page of "
        f"{_ANCHOR_PAGE} events, so this means more than {_COMPARISON_MAX_EVENTS} of them share "
        "its boundary timestamps; narrow EPICS_MCP_LIVE_ALARM_PV to a single PV."
    )
    assert _identities(naive) == _identities(with_zone)


def test_past_window_returns_nothing(client: AlarmClient, pv: str) -> None:
    """The negative control, and it is not optional: every test above would ALSO pass if the
    window were silently dropped and the whole history searched."""
    assert _count(client, pv, *_PAST) == 0


def test_misread_amounts_rejected_before_any_request(client: AlarmClient, pv: str) -> None:
    """'500 millis' is the sharpest: live it RETURNS data, for a 500-MINUTE window."""
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
    and matches nothing, reporting exactly like an unconfigured PV. Must be withheld, not False.
    """
    configured, _ = client.is_alarm_configured(configured_pv, config_name=alarm_tree)
    assert configured is True, f"positive control failed: {configured_pv!r} not in {alarm_tree!r}"

    miscased, _ = client.is_alarm_configured(configured_pv, config_name=alarm_tree.lower())
    assert miscased is None  # withheld, NOT False, which is what it used to report


def test_unknown_alarm_tree_is_withheld(client: AlarmClient, configured_pv: str) -> None:
    """An unknown tree is quiet too: the index pattern ends in '*', so Elasticsearch answers
    200 + [] instead of index_not_found."""
    configured, _ = client.is_alarm_configured(configured_pv, config_name="ZZZNoSuchTree")
    assert configured is None


def test_unconfigured_pv_in_a_real_tree_is_still_false(
    client: AlarmClient, alarm_tree: str
) -> None:
    """The other half, the tree probe must not turn every miss into 'withheld'. A live tree
    plus an absent PV is a REAL negative and has to stay False, or the fix would buy honesty by
    never answering."""
    configured, _ = client.is_alarm_configured("ZZZ-no-such-pv", config_name=alarm_tree)
    assert configured is False


# --- MA-2b(a/c) alarm filters: honoured, ignored, or over-restricting? (two controls each) ---
# (each probe pairs a negative control with a positive control on the filter VALUE, see below)
#
# The alarm-logger IGNORES an unsupported query param (its parser's default branch is a bare
# `break;`) and BROADENS the result. A NEGATIVE control (impossible value -> []) catches that. But a
# negative control ALONE is NOT enough, this repo's server-decided rule (CLAUDE.md) names the exact
# "filter that blocks everything" case: a server that returns [] for EVERY value passes a
# negative-only test yet silently drops real data. So each probe ALSO carries a POSITIVE control on
# the filter VALUE, derived from the unfiltered window itself (the tree lives in each event's
# `config` path, the severity in its `severity` field): a real value must return a NON-EMPTY result.
# BOTH controls must hold before the get_alarm_history tool description may drop "UNVERIFIED" for a
# given server; a negative control alone earns nothing (QA 2026-07-22).


def test_root_filter_is_honoured(client: AlarmClient, pv: str) -> None:
    """root two controls: an UNMATCHED tree returns nothing (negative, catches silent broadening),
    AND the pv's OWN tree (derived from the unfiltered events) returns a non-empty result (positive,
    catches a server that over-restricts / blocks everything)."""
    unfiltered, _ = _events(client, pv, "7 days")
    assert unfiltered, _NO_REFERENCE  # baseline, without events neither control is evaluable
    nonsense, ncap = client.get_alarm_history(
        pv, start="7 days", end="now", max_events=_MAX_EVENTS, root="ZZZNoSuchTree"
    )
    assert not ncap and nonsense == [], (
        "root ignored: an unmatched tree still returned events, the server silently broadened"
    )
    real_tree = str(unfiltered[0]["config"]).split("/")[
        1
    ]  # config = 'state:/<tree>/...' | 'config:/...' | 'command:/<tree>/...'
    matched, _ = client.get_alarm_history(
        pv, start="7 days", end="now", max_events=_MAX_EVENTS, root=real_tree
    )
    assert matched, f"root over-restricts: the pv's own tree {real_tree!r} matched nothing"


def test_severity_filter_is_honoured(client: AlarmClient, pv: str) -> None:
    """severity two controls: an impossible severity returns nothing (negative), AND a severity that
    actually appears in the unfiltered window returns a non-empty result (positive)."""
    unfiltered, _ = _events(client, pv, "7 days")
    assert unfiltered, _NO_REFERENCE  # baseline
    nonsense, ncap = client.get_alarm_history(
        pv, start="7 days", end="now", max_events=_MAX_EVENTS, severity="ZZZNOSUCH"
    )
    assert not ncap and nonsense == [], (
        "severity ignored: an impossible severity still returned events (silent broadening)"
    )
    present = next((str(e["severity"]) for e in unfiltered if e.get("severity")), None)
    if present is None:
        pytest.skip(
            "no event in the window carries a severity, the positive control is not evaluable"
        )
    matched, _ = client.get_alarm_history(
        pv, start="7 days", end="now", max_events=_MAX_EVENTS, severity=present
    )
    assert matched, (
        f"severity over-restricts: {present!r} appears unfiltered but filtered to nothing"
    )


# --- S11 schema anchor: the strict client schema, pinned against the REAL payload ---


def test_live_history_satisfies_the_strict_schema(client: AlarmClient, pv: str) -> None:
    """S11 anchor: the record schema (every doc a dict carrying a string ``config``, measured
    2026-07-16 on BOTH doc types, state: and config:) was derived from this live payload. The
    client now RAISES on anything else, so this run passing IS the proof the real payload matches;
    the explicit per-event assert pins the projected surface too. Goes red if a logger version
    stops matching, then the schema is re-measured, never loosened blindly."""
    events, _capped = client.get_alarm_history(pv, "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
    assert events, (
        "positive control not met: no alarm docs for the fixture PV in a 2020-2030 window, "
        "the schema anchor cannot pin anything. Check EPICS_MCP_LIVE_ALARM_PV."
    )
    assert all(isinstance(event.get("config"), str) and event["config"] for event in events)
