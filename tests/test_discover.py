"""Tests for epics_mcp.tools.discover."""

from collections import OrderedDict
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from epics_mcp.errors import EpicsConnectionError, PVNotFoundError, PVTimeoutError
from epics_mcp.tools.discover import _discover_pvs


async def test_discover_wildcard() -> None:
    result = await _discover_pvs("TEST:*")

    assert result["total"] == 0
    note = result["note"]
    assert isinstance(note, str)
    assert "ChannelFinder" in note


async def test_discover_concrete_found() -> None:
    with patch(
        "epics_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        return_value={"pv_name": "TEST:PV", "value": 42.0},
    ):
        result = await _discover_pvs("TEST:PV")

    assert result["total"] == 1
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "found"
    assert pvs[0]["value"] == 42.0


async def test_discover_concrete_not_found() -> None:
    with patch(
        "epics_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        side_effect=PVNotFoundError("PV 'MISSING:PV' not found"),
    ):
        result = await _discover_pvs("MISSING:PV")

    assert result["total"] == 0
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "not_found"


async def test_discover_concrete_timeout_is_distinct_from_not_found() -> None:
    """S3-5: a timeout (IOC down / network) reports status 'timeout', NOT 'not_found'."""
    with patch(
        "epics_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        side_effect=PVTimeoutError("Timeout getting PV 'SLOW:PV'"),
    ):
        result = await _discover_pvs("SLOW:PV")
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "timeout"


async def test_discover_concrete_connection_error_is_status_error() -> None:
    """S3-5: a connection error reports status 'error', distinct from 'not_found'/'timeout'."""
    with patch(
        "epics_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        side_effect=EpicsConnectionError("broken pipe"),
    ):
        result = await _discover_pvs("BAD:PV")
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "error"


# --- MA-2: wildcard discovery delegates to ChannelFinder (query_channels) --------------------
# A wildcard used to return a permanent empty stub even with ChannelFinder configured. It now
# delegates to the existing CF glob search and maps the registered channels into the discover
# shape. Registration is NOT a liveness probe, so the status is a distinct 'registered' (never
# 'found', which means a real p4p connect succeeded).


def _cf_result(channels: list[dict[str, object]], *, capped: bool = False) -> dict[str, object]:
    """A query_channels success payload (enabled) with the given projected channels."""
    return {"enabled": True, "channels": channels, "total": len(channels), "capped": capped}


async def test_discover_wildcard_delegates_to_channelfinder() -> None:
    """MA-2: a wildcard + active ChannelFinder delegates and maps the channels into the discover
    shape with status 'registered'. Mutant (delegation removed) -> the empty stub -> this fails."""
    channels: list[dict[str, object]] = [
        {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"},
        {"name": "SIM:PS-02:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"},
    ]
    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_result(channels),
    ):
        result = await _discover_pvs("SIM:PS-*")

    assert result["total"] == 2
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert {pv["pv_name"] for pv in pvs} == {"SIM:PS-01:Cur-RB", "SIM:PS-02:Cur-RB"}
    assert all(pv["status"] == "registered" for pv in pvs)
    # Registration != liveness, a status a caller could mistake for a live connect must not appear.
    assert all(pv["status"] not in {"found", "not_found", "timeout", "error"} for pv in pvs)


async def test_discover_wildcard_carries_channelfinder_cap() -> None:
    """MA-2: the CF ``capped`` flag (registry truncated) is carried through, not dropped."""
    channels: list[dict[str, object]] = [{"name": "SIM:A", "ioc_name": None, "host_name": None}]
    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_result(channels, capped=True),
    ):
        result = await _discover_pvs("SIM:*")

    assert result["capped"] is True


async def test_discover_wildcard_cf_disabled_keeps_honest_stub() -> None:
    """MA-2: with ChannelFinder NOT configured, the wildcard branch keeps today's honest stub
    (empty + a note naming ChannelFinder), never a bare empty that reads as 'no such PV'."""
    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value={"enabled": False, "channels": [], "total": 0, "note": "cf disabled"},
    ):
        result = await _discover_pvs("TEST:*")

    assert result["total"] == 0
    assert result["pvs"] == []
    note = result["note"]
    assert isinstance(note, str)
    assert "ChannelFinder" in note


# --- S35: the two shape checks on the ChannelFinder payload are OBSERVED ----------------------
# ``_discover_by_channelfinder`` builds ``pvs`` behind two checks: a list check on the whole
# payload and a dict check per entry. Until these tests they were MUTUALLY MASKING, and that was
# measured on this tree, not argued: forcing EITHER check to ``True`` left the full suite green,
# 2023 passed under both mutants. The masking is mechanical, with a dict or a str payload the
# mutant iterates keys or characters and the OTHER check drops every one of them, so guarded and
# mutant answer identically.
#
# Only the ENABLING polarity was unobserved, and the distinction matters when reading these tests
# against the comment in discover.py. Forcing either check to ``False`` empties the result and
# reddens ``test_discover_wildcard_delegates_to_channelfinder`` above, which has covered that half
# all along.
#
# EVERY test below asserts ``source == "channelfinder"``. That is a BRANCH WITNESS, not
# decoration: the ChannelFinder-DISABLED branch returns the same ``pvs == [] / total == 0`` and
# omits ``source`` entirely (the per-path field inventory in CHANGELOG.md states this), so without
# it a test is satisfiable by the stub path having never executed a guard line at all. Measured
# before it was added.
#
# The payloads are built as their own literals rather than through ``_cf_result``: that helper
# takes ``list[dict[str, object]]``, and a tuple, a str and a ``MappingProxyType`` are precisely
# what these tests must pass. Widening it would blunt what the MA-2 tests above document.


class _ListSubclass(list[object]):
    """A real ``list`` subclass: ``isinstance`` accepts it, ``type(x) is list`` does not."""


def _cf_payload(channels: object) -> dict[str, object]:
    """A ``query_channels`` success payload whose ``channels`` slot is deliberately off-contract."""
    return {"enabled": True, "channels": channels, "total": 1, "capped": False}


async def test_absent_channels_payload_is_empty_not_a_crash() -> None:
    """A payload with no ``channels`` key at all answers empty rather than raising.

    ``ChannelQueryResult`` is ``total=False`` and declares ``channels`` nullable, and its two
    ``count_only`` branches omit the key outright, so ``result.get("channels")`` is ``None`` by
    TYPE. ``discover`` never asks for ``count_only``, so that shape does not arrive on today's
    call path; the list check is nonetheless what absorbs it.

    SECOND-GRADE EVIDENCE, said plainly: the mutant kills this by
    ``TypeError: 'NoneType' object is not iterable``, a collateral crash rather than the guard's
    own diagnosis, which is exactly the distinction ``scripts/guard_audit.py::classify`` draws.
    The assertion-grade observer of this same check is the test below.
    """
    payload: dict[str, object] = {"enabled": True, "total": 0, "capped": False}
    assert "channels" not in payload  # the premise in code: this really is the absent-key shape
    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        result = await _discover_pvs("SIM:*")

    assert result["source"] == "channelfinder"  # branch witness: not the CF-disabled stub
    assert result["pvs"] == []
    assert result["total"] == 0


async def test_a_non_list_channels_payload_is_dropped_whole() -> None:
    """A non-list ``channels`` is not read, even when every entry inside it is readable.

    THE PAYLOAD IS THE POINT, so its two properties are asserted rather than described: it is not
    a list (only the list check can fire) and every entry IS a dict (the element check would not
    have dropped them). Swap in a dict or a str and the second assertion goes red on purpose,
    because those payloads RESTORE the mask: guarded and mutant both answer empty and this test
    would silently stop observing anything. The non-emptiness assertion closes the same hole from
    the other side, ``all(...)`` over an empty payload being vacuously true.

    A tuple is one representative of the separating class, not the only member: any non-list
    iterable of dicts does the same (a generator, a ``deque``, a ``UserList``, a ``dict_values``
    view, measured).

    NAMED LIMIT, and the reason this test pins rather than endorses: the answer is
    indistinguishable from "the registry knows no such channel". It carries ``source`` and the
    registry note while having read nothing, and the layer below,
    ``ChannelFinderClient.find_channels``, RAISES ``ChannelFinderResponseError`` on this very
    payload. Aligning discover with its own client is a wire-contract change, deliberately not
    made here (S29's eleventh target reshaped these lines and left them unpromoted). Whoever
    makes it will find this test red and should be changing it on purpose.
    """
    channels: tuple[object, ...] = (
        {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"},
    )
    assert channels, "an empty payload satisfies both premises below vacuously"
    assert all(isinstance(entry, dict) for entry in channels), "the element check would not drop"
    # Widened to ``object`` on purpose: against the tuple annotation the check below is statically
    # provable, and mypy's warn_unreachable rejects a premise it can decide without running.
    shape: object = channels
    assert not isinstance(shape, list), "only the list check can fire on this payload"

    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_payload(channels),
    ):
        result = await _discover_pvs("SIM:*")

    assert result["source"] == "channelfinder"
    assert result["pvs"] == []
    assert result["total"] == 0


async def test_a_non_dict_channel_entry_is_dropped_and_its_siblings_survive() -> None:
    """A non-dict entry is dropped, the well-formed ones around it still come back.

    Two junk entries, and the second is not padding. A ``str`` entry catches the check being
    removed outright (the mutant reaches ``.get`` on a str and dies). A ``list`` entry catches the
    narrower weakening ``isinstance(channel, (dict, list))``, which survives every other test in
    this file and turns a silent drop into a crash.

    SECOND-GRADE EVIDENCE like the absent-key test: both kills are tracebacks, not assertions.
    """
    good: dict[str, object] = {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "h"}
    channels: list[object] = [good, "not-a-dict", []]

    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_payload(channels),
    ):
        result = await _discover_pvs("SIM:*")

    assert result["source"] == "channelfinder"
    assert result["total"] == 1
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert [pv["pv_name"] for pv in pvs] == ["SIM:PS-01:Cur-RB"]


async def test_the_element_check_is_isinstance_not_duck_typing() -> None:
    """An entry that reads like a channel but is not a ``dict`` is dropped, not read.

    The payload is chosen as an instrument: a ``MappingProxyType`` is the one non-dict entry that
    does NOT crash when the check is removed, so the mutant produces a WRONG ANSWER (one channel
    where the guarded code returns none) instead of a traceback. That makes this the
    assertion-grade observer of the element check, and it is also what catches every duck-typed
    weakening (``hasattr(channel, "get")``, ``isinstance(channel, Mapping)``).

    ⚠️ This pins an IMPLEMENTATION CHOICE, deliberately: identity of type, not "it answers
    ``.get``". A later, considered widening to ``Mapping`` is a legitimate change and will find
    this test red; that is the point of writing it down rather than leaving the choice unstated.
    """
    entry: object = MappingProxyType(
        {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"}
    )
    assert not isinstance(entry, dict), "only the element check can drop this entry"
    assert isinstance(entry, MappingProxyType)
    assert entry.get("name") == "SIM:PS-01:Cur-RB", "readable, so removing the check reads it"

    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_payload([entry]),
    ):
        result = await _discover_pvs("SIM:*")

    assert result["source"] == "channelfinder"
    assert result["pvs"] == []
    assert result["total"] == 0


async def test_subclasses_of_list_and_dict_are_still_read() -> None:
    """Both checks are ``isinstance``, so a subclass on either side is read, not silently dropped.

    The positive side of the pair, and without it two narrowings survive every other test here:
    ``type(raw_channels) is list`` and ``type(channel) is dict`` both keep the whole suite green
    while quietly turning a readable payload into an empty answer. One test catches both because
    the payload is a ``list`` subclass carrying a ``dict`` subclass.

    Not academic on the second half: ``query_channels`` builds its entries with ``dict(channel)``
    today, and any future mapping subclass at that seam would go silently to zero.

    Honest residue, since the rest of this block claims coverage: ``isinstance(raw_channels,
    (list, dict))`` survives all five tests. It is an EQUIVALENT mutant rather than a gap, because
    iterating a dict yields its keys and no bare dict can be a key (dicts are unhashable), so the
    element check drops every one of them. Measured, including the single exception: a dict
    subclass that defines ``__hash__`` can be a key. No JSON payload produces one.
    """
    channels = _ListSubclass([OrderedDict({"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc"})])
    assert isinstance(channels, list) and type(channels) is not list
    assert isinstance(channels[0], dict) and type(channels[0]) is not dict

    with patch(
        "epics_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_payload(channels),
    ):
        result = await _discover_pvs("SIM:*")

    assert result["source"] == "channelfinder"
    assert result["total"] == 1
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert [pv["pv_name"] for pv in pvs] == ["SIM:PS-01:Cur-RB"]
