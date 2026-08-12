"""Tests for epics_mcp.tools.discover."""

from collections import OrderedDict, UserDict, deque
from collections.abc import Collection, Mapping
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from epics_mcp.errors import EpicsConnectionError, PVNotFoundError, PVTimeoutError
from epics_mcp.tools.discover import _discover_pvs


async def test_discover_wildcard() -> None:
    # ⚠️ This one drives the REAL ``query_channels`` (no double), so it measures the MACHINE: with
    # EPICS_MCP_CHANNELFINDER_URL set it leaves the box and fails with EpicsConnectionError.
    # Reproduced against a dead port. conftest.py strips the six EPICS_* search vars and, by its
    # own comment, deliberately not the EPICS_MCP_* ones. Recorded here rather than only in a
    # commit body: the fix is an autouse env strip that touches every test in the suite, which is
    # its own piece of work, and a note nobody reads is not persistence.
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
# EVERY test below asserts ``source == "channelfinder"`` as a BRANCH WITNESS: the
# ChannelFinder-DISABLED branch returns the same ``pvs == [] / total == 0`` and omits ``source``
# entirely (the per-path field inventory in CHANGELOG.md states this), so without it a test is
# satisfiable by the stub path having never executed a guard line at all. Measured, and the number
# matters: THREE of the five would pass that way, the three that expect an EMPTY result. The two
# that expect ``total == 1`` are self-witnessing, because the stub branch cannot produce a hit, so
# for those two the assertion is uniformity rather than evidence. Saying "not decoration" of all
# five would be one claim too wide.
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

    FILED HONESTLY, because a kill matrix over 34 mutants says so: for the LIST CHECK this test
    kills nothing the next test does not already kill. Its one exclusive contribution is a line
    ABOVE the guard, ``result.get("channels")``: turn that into ``result["channels"]`` and only
    this test notices, with a KeyError. It earns its place there, not as an observer of the check
    it sits under.
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

    THE PAYLOAD IS THE POINT, so its properties are asserted rather than described: exactly one
    entry (so nothing below is vacuously true), not a list (only the list check can fire), and
    every entry IS a dict (the element check would not have dropped them). Swap in a dict or a str
    and the last assertion goes red on purpose, because those payloads RESTORE the mask: guarded
    and mutant both answer empty and this test would silently stop observing anything.

    TWO instruments, not one, and that correction was measured rather than tidied. Calling a tuple
    a stand-in for "any non-list iterable of dicts" was wrong in both directions: a tuple alone
    catches ``(list, tuple)``/``Sequence``/``Iterable`` and MISSES ``MutableSequence``, while a
    ``deque`` catches ``MutableSequence`` and misses ``(list, tuple)``. The two mutant sets are
    DISJOINT. An instrument is a point, never a class, so the class is covered by covering it
    twice.

    ⚠️ A GENERATOR CANNOT BE USED HERE, and the annotation now says so mechanically. It is
    non-list and it yields dicts, so it reads like a third instrument, but ``all(...)`` above
    CONSUMES it: the code under test then receives an exhausted iterator, answers empty, and the
    mutant SURVIVES with the test green. ``bool(empty generator)`` is ``True`` as well, so a
    truthiness premise would not have caught it either. Hence ``Collection`` (a generator is not
    one) plus ``len(...) == 1`` (a generator has no ``len``): a generator swap now fails loudly
    instead of going blind.

    NAMED LIMIT, and the reason this test pins rather than endorses: the answer is
    indistinguishable from "the registry knows no such channel". It carries ``source`` and the
    registry note while having read nothing, while the layer below refuses the same SHAPE loudly,
    ``ChannelFinderClient.find_channels`` raising ``ChannelFinderResponseError`` on a non-list body
    (not on this literal object, which is a decoded-JSON layer below and can never be a tuple).
    Aligning discover with its client changes behaviour only under a doubled seam, since no real
    call path reaches these lines, so it is NOT a wire-contract change; it was left undone because
    S29's eleventh target reshaped these lines and deliberately did not promote them. Whoever
    promotes them will find this test red and should be changing it on purpose.
    """
    entry = {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"}
    payloads: tuple[Collection[object], ...] = (tuple([dict(entry)]), deque([dict(entry)]))
    for channels in payloads:
        assert len(channels) == 1, "an empty payload satisfies the premise below vacuously"
        assert all(isinstance(e, dict) for e in channels), "the element check would not drop these"
        # Widened to ``object`` on purpose: against the annotation the check below is statically
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

    Both junk kinds are now asserted rather than only described, and that gap was measured: with
    no premise at all, deleting EITHER entry left this test green over the whole file, and
    deleting the ``[]`` handed ``isinstance(channel, (dict, list))`` a clean pass through the
    suite. A docstring saying "not padding" guarded nothing.

    SECOND-GRADE EVIDENCE like the absent-key test: both kills are tracebacks, not assertions.
    """
    good: dict[str, object] = {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "h"}
    channels: list[object] = [good, "not-a-dict", []]
    assert any(isinstance(e, str) for e in channels), "the str entry catches removal of the check"
    assert any(isinstance(e, list) for e in channels), "the list entry catches the (dict, list) one"

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

    The entries are instruments: a mapping that is NOT a dict does not crash when the check is
    removed, so the mutant produces a WRONG ANSWER (one channel where the guarded code returns
    none) instead of a traceback. That makes this the assertion-grade observer of the element
    check.

    TWO instruments again, for the reason the sibling test above spells out, and here the
    correction was sharper. ``MappingProxyType`` was called "the one non-dict entry that does not
    crash" and credited with catching "every duck-typed weakening". Both were wrong, measured: a
    ``UserDict`` and a bare class answering ``.get`` do not crash either, and because a proxy is
    deliberately IMMUTABLE the weakening ``isinstance(channel, MutableMapping)`` leaves the proxy
    dropped and this test GREEN. ``UserDict`` is mutable and catches it. Proxy catches ``Mapping``
    and ``hasattr(.., "get")``; the two sets are disjoint, so both entries stay.

    ⚠️ This pins an IMPLEMENTATION CHOICE, deliberately: identity of type, not "it answers
    ``.get``". A later, considered widening to ``Mapping`` or ``MutableMapping`` is a legitimate
    change and will find this test red; that is the point of writing the choice down.
    """
    fields = {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"}
    entries: tuple[Mapping[str, object], ...] = (MappingProxyType(dict(fields)), UserDict(fields))
    for entry in entries:
        shape: object = entry
        assert not isinstance(shape, dict), "only the element check can drop this entry"
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

    Honest residue, and the first version of this paragraph got it wrong in the direction that
    flatters: it called ``isinstance(raw_channels, (list, dict))`` an EQUIVALENT mutant, on the
    argument that iterating a dict yields keys and no bare dict can be a key. The argument is
    sound and the conclusion is not. Two payloads separate it from the guarded code, measured:
    a dict whose key is a ``__hash__``-defining dict subclass, and a dict subclass with its own
    ``__iter__`` that yields dicts, which needs no hashing at all and so falsifies the premise
    rather than the corollary. It survives the suite, so it IS residue; it is simply not
    equivalent, and neither payload can come out of a JSON body.
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
