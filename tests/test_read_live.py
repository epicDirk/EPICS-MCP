"""Live probes for the PV READ plane, the surface this whole server exists for.

Opt-in: ``pytest tests/test_read_live.py -m live`` with a PVA search lane in
``EPICS_MCP_LIVE_READ_PVA_ADDR_LIST`` and a readable PV in ``EPICS_MCP_LIVE_READ_PV``.

SCOPE EVERY RUN TO THIS FILE. ``pyproject.toml`` sets ``testpaths = ["tests"]`` and declares no
``addopts``, so a bare ``pytest -m live`` falls back to the whole directory and collects ALL live
modules. Four of those write real logbook entries into a service with no delete. Nothing in this
repository guards against the missing path; the path itself is the guard, and nothing watches this
figure either (CONTRIBUTING.md carries the same one and the rule for re-measuring it).

WHY THESE EXIST
---------------
Nine live modules covered the REST planes and the write path. The read plane, the one every other
answer is built on, had none: no probe reached a real IOC through ``get_pv_value``, ``get_pvs``,
``get_pv_info``, ``monitor_pv``, ``discover_pvs`` or ``diagnose_connection``.

The offline suite already pins what those tools do with a payload they were HANDED. What it cannot
pin is whether a real facility hands them that payload at all, and whether two independent planes
describing the same channel AGREE. That distinction decides the shape of every probe below: each
one either crosses two sources that a test double cannot make contradict each other, or it pins a
decision taken by a FOREIGN server. A mock only ever knows what the test told it, so it can never
disagree with itself; a real plane can, and that disagreement is the finding.

Probes that would merely repeat an offline assertion are deliberately ABSENT. A reach-versus-lane
comparison, for instance, needs no facility at all (``provenance`` reads the environment and opens
no socket) and is already pinned twice in ``tests/test_provenance.py``; adding it here would have
inflated the live count without covering anything. The reach values are PROTOCOLLED from the run
instead.

THE TOOL LAYER, NOT THE SERVICE LAYER
-------------------------------------
Every call goes through ``epics_mcp.server``, which is what an MCP client actually reaches: the
``@with_reach`` and ``@translate_epics_errors`` decorators are part of the answer under test. So a
failed read arrives as ``ToolError("[<CODE>] <message> [reach: ...]")``, not as the domain
exception, and the reach clause on the ERROR path is asserted here against a real plane rather
than against a fake. None of the nine sibling modules sees either decorator: eight call a service
client directly, and the ninth reaches the tool layer at ``tools/write``, which carries neither.

ONE SEARCH ENVIRONMENT PER PROCESS
----------------------------------
``services.epics_client.get_context`` memoises the p4p ``Context`` process-wide, and p4p reads the
search variables ONCE, when that context is built. A probe that swapped the lane mid-run would not
move the socket, but it would point every later probe at a dead address. This module therefore
injects exactly one lane, from the environment, and never varies it.

NO CLIENT DOUBLES IN THIS FILE. ``scripts/guard_audit.py`` counts every test that replaces a
``*Client`` class and pins the total; a live probe has nothing to replace anyway.

The addresses and PV names are facility-agnostic: everything site-specific arrives through the
environment, and the negative controls use the declared synthetic ``ZZZ-FAKE`` prefix.
"""

from __future__ import annotations

import os

import pytest
from fastmcp.exceptions import ToolError

from epics_mcp.server import (
    diagnose_connection,
    discover_pvs,
    get_pv_info,
    get_pv_value,
    get_pvs,
    monitor_pv,
)
from tests.live_gate import assert_live_available, live_demanded

pytestmark = pytest.mark.live

#: Explicit on every call. The process default is a deployment decision (this machine sets
#: EPICS_MCP_DEFAULT_TIMEOUT well above the code default), and a probe whose duration is decided
#: elsewhere cannot state its own cost.
_READ_TIMEOUT = 10.0

#: Shorter, because every negative control spends it in full: a PVA name-server answers an unknown
#: name with silence, never with a not-found, so the wait IS the answer.
_ABSENT_TIMEOUT = 5.0

#: Above five seconds by measurement, not by taste: on this lane a cold channel has taken up to
#: 4.2 s to deliver its first update. A shorter window would collect nothing and report a healthy
#: channel as disconnected, which is a false red about the facility, not about the code.
_MONITOR_DURATION = 6.0

#: A name no facility issues. ``ZZZ-FAKE`` is a declared synthetic marker in
#: ``scripts/pv_leak_scan.py``, so it may be committed while a real name may not.
_ABSENT_PV = "ZZZ-FAKE99:Ctrl-X-99"

#: The read-side error codes a missing channel can legitimately produce. All three are accepted
#: rather than one being pinned: under a PVA name server a typo and a dead IOC both time out, and
#: a network fault is a third honest outcome. What is NOT acceptable is a fabricated success, and
#: that is what these probes actually assert.
_ABSENT_CODES = ("PV_TIMEOUT", "PV_NOT_FOUND", "EPICS_CONNECTION_FAILED")

#: ``EPICS_MCP_LIVE_READ_*`` name -> the EPICS variable it feeds. The map carries NAMES only; the
#: addresses live in the environment. Only the first is required: the TCP name-server entry was
#: removed from this project's configuration on 2026-08-07 because it cost over six seconds on the
#: first read of a process, so demanding it would skip a lane that works.
_SEARCH_ENV_MAP = {
    "EPICS_MCP_LIVE_READ_PVA_ADDR_LIST": "EPICS_PVA_ADDR_LIST",
    "EPICS_MCP_LIVE_READ_PVA_AUTO_ADDR_LIST": "EPICS_PVA_AUTO_ADDR_LIST",
    "EPICS_MCP_LIVE_READ_PVA_NAME_SERVERS": "EPICS_PVA_NAME_SERVERS",
    "EPICS_MCP_LIVE_READ_CA_ADDR_LIST": "EPICS_CA_ADDR_LIST",
    "EPICS_MCP_LIVE_READ_CA_AUTO_ADDR_LIST": "EPICS_CA_AUTO_ADDR_LIST",
}
_REQUIRED_SEARCH_VAR = "EPICS_MCP_LIVE_READ_PVA_ADDR_LIST"


def _read_lane_configured() -> bool:
    """A search lane AND a target PV. Either alone cannot produce a reading."""
    return bool(os.environ.get(_REQUIRED_SEARCH_VAR) and os.environ.get("EPICS_MCP_LIVE_READ_PV"))


@pytest.fixture(autouse=True)
def _require_live_read_plane() -> None:
    """Setup-time gate (S30): silent skip by default, loud failure when a live run is demanded."""
    assert_live_available(
        _read_lane_configured(),
        "live read probe: set EPICS_MCP_LIVE_READ_PVA_ADDR_LIST (the PVA search lane) and "
        "EPICS_MCP_LIVE_READ_PV (a readable PV on that lane, carrying an engineering unit)",
        demanded=live_demanded(os.environ),
    )


@pytest.fixture(autouse=True)
def _read_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-inject the read lane AFTER conftest's autouse strip.

    ``tests/conftest.py::_isolate_epics_search_env`` deletes the six EPICS search variables before
    every test so posture assertions measure the code rather than the machine. That strip also
    removes the route to any IOC, so a live read has to put its own lane back; the conftest fixture
    runs first because a higher-scope autouse fixture always does. Same shape as the sibling
    ``loopback_write_env``, with the values coming from the environment instead of a constant.
    """
    for source, target in _SEARCH_ENV_MAP.items():
        value = os.environ.get(source)
        if value:
            monkeypatch.setenv(target, value)


@pytest.fixture
def pv() -> str:
    """The readable target. Needs an engineering unit for the cross-check below."""
    return os.environ["EPICS_MCP_LIVE_READ_PV"]


@pytest.fixture
def second_pv() -> str:
    """A second readable PV, so the batch probe has something to keep while one entry fails."""
    value = os.environ.get("EPICS_MCP_LIVE_READ_PV_2")
    assert_live_available(
        bool(value),
        "set EPICS_MCP_LIVE_READ_PV_2 to a second readable PV for the mixed-batch probe",
        demanded=live_demanded(os.environ),
    )
    assert value is not None  # narrowed by the gate above
    return value


@pytest.fixture
def glob_core() -> str:
    """A name fragment that matches at least one registered channel when wrapped in stars.

    Deliberately a FRAGMENT rather than a whole name: the anchoring probe needs a value that
    matches when surrounded by stars and cannot match when it is not.
    """
    value = os.environ.get("EPICS_MCP_LIVE_READ_GLOB_CORE")
    assert_live_available(
        bool(value),
        "set EPICS_MCP_LIVE_READ_GLOB_CORE to a name fragment that is not a whole channel name "
        "and matches at least one registered channel when wrapped in stars",
        demanded=live_demanded(os.environ),
    )
    assert value is not None  # narrowed by the gate above
    # ⛔ The case-insensitivity probe compares the fragment against its upper-cased self. An
    # already-upper-case fragment would make those two calls byte-identical, and the comparison
    # would hold an answer against itself while looking like a server measurement. Refuse rather
    # than pass vacuously.
    assert_live_available(
        value != value.upper(),
        "EPICS_MCP_LIVE_READ_GLOB_CORE must contain at least one lower-case letter, otherwise "
        "the case-insensitivity probe compares the server's answer with itself",
        demanded=live_demanded(os.environ),
    )
    return value


#: The error codes that mean "the plane did not answer", as opposed to "the server answered
#: something wrong". Only these turn into a mid-run outage notice; anything else is a real defect
#: and must stay an assertion. Without this narrowing the helper below would swallow EVERY failed
#: read into a skip, so a regression that broke reading outright would report green in the default
#: mode, which is the exact defect this module exists to catch.
_OUTAGE_CODES = ("PV_TIMEOUT", "EPICS_CONNECTION_FAILED")


async def _still_answering(pv_name: str, timeout: float) -> dict[str, object]:
    """Read *pv_name* and turn a MID-RUN plane outage into a named skip instead of an assertion.

    Without this, a facility that stops answering halfway through produces an ordinary assertion
    failure, indistinguishable in the report from a defect in this repository. The message says
    which it is, and the demand switch decides whether that is a skip or a red. Same in-probe gate
    the write module uses when a record turns out to lack drive limits: a data-dependent outcome
    inside a running live probe is its own class.

    ⚠️ NARROWED TO THE OUTAGE CODES ON PURPOSE. Catching every ``ToolError`` here would make the
    helper a blanket amnesty: a code change that broke reading altogether raises through the same
    type, and without ``EPICS_MCP_REQUIRE_LIVE`` the gate below turns that into a silent skip.
    Anything outside :data:`_OUTAGE_CODES` is re-raised unchanged, so it fails loudly as it should.

    ⚠️ HONEST REACH: only the FIRST read of a probe goes through here. A plane that dies between
    two reads inside one probe still surfaces as a plain assertion. Widening that would mean
    routing every call through the helper, which would also route the assertions it is meant to
    keep sharp.
    """
    try:
        return await get_pv_value(pv_name, timeout)
    except ToolError as exc:
        message = str(exc)
        if not any(f"[{code}]" in message for code in _OUTAGE_CODES):
            raise
        assert_live_available(
            False,
            f"the live read plane stopped answering mid-run: {exc}",
            demanded=live_demanded(os.environ),
        )
        raise


def _mapping(value: object, what: str) -> dict[str, object]:
    """Narrow one nested answer field to a mapping, saying WHICH field failed if it is not one.

    The read answers are ``dict[str, object]`` on the wire, so a nested access needs a narrowing
    step anyway; doing it through a named helper turns a type-checker requirement into a readable
    assertion about the answer shape.
    """
    assert isinstance(value, dict), f"{what} must be a mapping, got {type(value).__name__}"
    return value


def _display_units(answer: dict[str, object]) -> str | None:
    """The engineering unit out of the display block, or None when the record carries none."""
    display = answer.get("display")
    if not isinstance(display, dict):
        return None
    units = display.get("units")
    return units if isinstance(units, str) and units else None


class TestSingleRead:
    """``get_pv_value`` against a real record."""

    async def test_the_display_block_agrees_with_the_record_field(self, pv: str) -> None:
        """The structured metadata and the raw record field must describe the SAME record.

        Two independent routes to one quantity: the NT display block that the provider synthesises
        for the value channel, and the ``.EGU`` field channel served by the IOC itself. A double
        cannot put those two in conflict because a double supplies both; a real record can, and a
        gateway that synthesises a display block from the wrong record is exactly the failure this
        catches. The probe skips rather than passing vacuously when the target has no unit at all.
        """
        answer = await _still_answering(pv, _READ_TIMEOUT)
        units = _display_units(answer)
        assert_live_available(
            units is not None,
            "EPICS_MCP_LIVE_READ_PV reports no engineering unit, so there is nothing to cross "
            "against .EGU; point it at a record that declares one",
            demanded=live_demanded(os.environ),
        )

        field = await get_pv_value(f"{pv}.EGU", _READ_TIMEOUT)
        assert field["value"] == units, (
            f"the display block says units={units!r} while the record field .EGU says "
            f"{field['value']!r}: the two routes describe different records"
        )

    async def test_an_absent_pv_raises_and_the_error_still_names_its_reach(self) -> None:
        """The negative control, and the half no offline test can reach.

        A read that fails has no payload to carry the reach field, so the decorator appends it to
        the MESSAGE instead. That clause is what tells a reader WHICH world failed to answer, and
        until now nothing had ever seen it produced by a real plane. Asserted together with the
        error code, because a fabricated success would carry neither.
        """
        with pytest.raises(ToolError) as excinfo:
            await get_pv_value(_ABSENT_PV, _ABSENT_TIMEOUT)

        message = str(excinfo.value)
        assert any(f"[{code}]" in message for code in _ABSENT_CODES), (
            f"an unreadable PV must fail with a read error code, got: {message}"
        )
        assert "reach: live-pv=" in message, (
            f"a failed read must still say which plane did not answer, got: {message}"
        )


class TestBatchRead:
    """``get_pvs``: the one path where a real plane decides how a partial failure is reported."""

    async def test_the_native_batch_agrees_with_reading_the_same_pvs_one_by_one(
        self, pv: str, second_pv: str
    ) -> None:
        """The NATIVE batch path, and the only probe in this module that reaches it.

        ⛔ Measured, and it corrects what the sibling probe below used to claim: a batch containing
        an unreadable name can NEVER exercise the native path. ``pv_get_batch`` calls the provider
        with p4p's default ``throw=True``, so one silent channel makes the whole native call raise
        and the code falls back to concurrent single reads. The fallback even signs its own work:
        its error text omits the ``after <t>s`` clause that ``pv_get`` puts in. Only an
        all-readable batch stays on the native path.

        What that path can get wrong is invisible to a mock, because a mock supplies both sides:
        the provider returns a positional LIST, and this code pairs it with the requested names by
        INDEX. A provider reordering its answers, or the pairing drifting, produces values under
        the wrong names, and every count still adds up. So the probe crosses the batch against the
        same PVs read INDIVIDUALLY and requires each name to carry its own value. That also
        exercises the documented promise that units ride inside ``display`` per PV, which is the
        reason the batch tool exists at all.
        """
        batch = await get_pvs([pv, second_pv], _READ_TIMEOUT)
        results = batch.get("results")
        assert isinstance(results, list)
        assert not batch.get("errors"), f"both PVs are readable, so no error is expected: {batch}"
        assert {entry["pv_name"] for entry in results} == {pv, second_pv}

        by_name = {entry["pv_name"]: entry for entry in results}
        for name in (pv, second_pv):
            single = await get_pv_value(name, _READ_TIMEOUT)
            batched = by_name[name]
            # NOT the value: it moves between two reads on a live channel. What must agree is the
            # SHAPE and the identity, which is what a mispaired answer would break.
            assert batched.get("display") == single.get("display"), (
                f"the batch and the single read disagree about the display block of {name!r}: "
                f"{batched.get('display')!r} against {single.get('display')!r}"
            )
            assert type(batched.get("value")) is type(single.get("value"))

    async def test_a_mixed_batch_keeps_the_readable_and_reports_the_unreadable(
        self, pv: str, second_pv: str
    ) -> None:
        """The FALLBACK path, positive and negative control in ONE call.

        ⚠️ This is the fallback, not the native path, and the sibling probe above says why. That
        is not a weakness here: the fallback is exactly the code a partially unreachable batch
        runs in production, and it is the code that decides whether an unreadable channel becomes
        an error entry or a plausible-looking result.

        The wire contract says a per-PV read failure lands in ``errors``. The formatter underneath
        never raises and turns an unconvertible object into a placeholder RESULT, so the failure
        mode worth guarding is an unreadable channel arriving on the wrong side of the answer. The
        count assertion is that guard: results plus errors must account for every name submitted,
        with the readable ones on one side and the absent one on the other.
        """
        names = [pv, second_pv, _ABSENT_PV]
        answer = await get_pvs(names, _ABSENT_TIMEOUT)

        results = answer.get("results")
        errors = answer.get("errors")
        assert isinstance(results, list) and isinstance(errors, list)
        assert len(results) + len(errors) == len(names), (
            f"submitted {len(names)} names but got {len(results)} results and {len(errors)} "
            "errors: a PV was dropped silently"
        )

        read_names = {entry["pv_name"] for entry in results if isinstance(entry, dict)}
        failed_names = {entry["pv_name"] for entry in errors if isinstance(entry, dict)}
        assert read_names == {pv, second_pv}, f"a readable PV went missing: {read_names}"
        assert failed_names == {_ABSENT_PV}, (
            f"the absent PV must be reported as an error, not as a value: {failed_names}"
        )


class TestInfoRead:
    """``get_pv_info``: what it adds over ``get_pv_value``, and what a real record fills in."""

    async def test_info_adds_exactly_one_key_and_the_record_fills_a_metadata_block(
        self, pv: str
    ) -> None:
        """Two halves, and they are NOT equally live. Said plainly rather than oversold.

        ⚠️ The key-set half is a CODE claim, not a facility one, and it cannot be made otherwise:
        both tools call the same ``pv_get`` and the second only assigns a status key, so the
        comparison holds a function against itself and only a code change can break it. It earns
        its place as a cheap pin on a pair that is easy to let drift apart, not as live coverage,
        and it is symmetric on purpose: the one-directional form would miss info LOSING a key.

        The second half is the live one, and a mock cannot make the claim at all: that a real
        facility populates at least one metadata block. Offline every block is whatever the fixture
        author typed. Which blocks arrive is deliberately not pinned, because that is a property of
        the record, not of this server.
        """
        value_answer = await _still_answering(pv, _READ_TIMEOUT)
        info_answer = await get_pv_info(pv, _READ_TIMEOUT)

        # Both answers carry a fresh timestamp, so compare the KEY SETS, never the values. The
        # symmetric difference catches a LOST key as well as an added one.
        assert set(info_answer) ^ set(value_answer) == {"status"}, (
            "get_pv_info and get_pv_value must differ in exactly the status key, they differ in "
            f"{set(info_answer) ^ set(value_answer)}"
        )
        assert info_answer["status"] == "success"

        blocks = [key for key in ("display", "control", "value_alarm") if key in info_answer]
        assert blocks, (
            "a real record must populate at least one metadata block "
            "(display / control / value_alarm); none arrived"
        )

    async def test_an_absent_record_field_raises_instead_of_answering(self, pv: str) -> None:
        """The negative control on the FIELD route specifically.

        A field suffix is just part of the channel name, so a misspelled field is a channel nobody
        serves. It has to fail like any other unreadable channel; an answer here would mean the
        gateway invented a record field, which would make every field-based cross-check worthless.
        """
        with pytest.raises(ToolError) as excinfo:
            await get_pv_info(f"{pv}.ZZZFAKEFIELD", _ABSENT_TIMEOUT)

        message = str(excinfo.value)
        assert any(f"[{code}]" in message for code in _ABSENT_CODES), (
            f"a nonexistent record field must fail like any unreadable channel, got: {message}"
        )


class TestMonitor:
    """``monitor_pv``: whether a real channel emits the notices the state machine is built on."""

    async def test_a_live_channel_reports_connected_with_at_least_one_event(self, pv: str) -> None:
        """The positive control. Two of its four claims are live, and the split is said openly.

        ⚠️ "connected implies at least one event" is CONSTRUCTIVE, not live: the flag is set in the
        same locked block that appends the event. It is asserted because the pair is the readable
        statement, not because it can fail on its own.

        The live claims are the other two, and neither has ever been checked against a facility.
        First, that each collected event carries the SAME metadata blocks the single read carries.
        That is a documented promise of this tool, and offline every event is a fixture the author
        wrote, so the two could not disagree. Second, the ``truncated`` coupling: the cap is
        deliberately over-collected by one so that a stream cut by the cap is distinguishable from
        one that delivered exactly the cap and went quiet. Only a channel that really keeps
        producing can exercise that, and this run's channel does.
        """
        single = await _still_answering(pv, _READ_TIMEOUT)
        cap = 5
        answer = await monitor_pv(pv, _MONITOR_DURATION, cap)

        assert answer["connection"] == "connected", (
            f"a channel that just answered a read must monitor as connected: {answer}"
        )
        events = answer.get("events")
        assert isinstance(events, list) and events, (
            "connected is only reachable once a value arrived, so the event list cannot be empty"
        )
        assert "connection_detail" not in answer, (
            "a healthy monitor must not carry an explanation line"
        )

        # The documented promise: an event is shaped like a single read, minus the envelope keys.
        envelope = {"reach", "status"}
        single_blocks = {key for key in ("alarm", "timestamp", "display") if key in single}
        for event in events:
            assert isinstance(event, dict)
            missing = single_blocks - set(event) - envelope
            assert not missing, (
                f"a monitor event must carry the same best-effort metadata as a single read; "
                f"this one is missing {sorted(missing)}: {event}"
            )

        # The cap coupling. truncated may be either way (it depends on how fast the channel is),
        # but a truncated stream must have been cut at exactly the cap.
        if answer["truncated"]:
            assert len(events) == cap, (
                f"a truncated stream is cut at the cap, got {len(events)} events for cap {cap}"
            )
        else:
            assert len(events) <= cap

    async def test_an_absent_channel_explains_its_silence(self) -> None:
        """The negative control, and it pins ``disconnected`` rather than merely not-connected.

        Zero events used to mean either "quiet PV" or "no such PV". The connection field separates
        them, and this is the first probe that asks a REAL provider to produce the disconnect
        notice the separation rests on.

        ⛔ THE EXACT VALUE IS THE POINT, and the weaker form was a real hole. ``!= "connected"``
        is also satisfied by ``unknown``, which is what the code answers when NO notice arrives at
        all; the detail line is non-empty in that case too, with a different sentence. So the
        weaker assertion survives the removal of ``notify_disconnect``, i.e. of exactly the
        mechanism this probe claims to exercise. ``== "disconnected"`` is reachable only when the
        provider really sent the notice and nothing else went wrong, which is the claim.
        """
        answer = await monitor_pv(_ABSENT_PV, _MONITOR_DURATION, 5)

        assert answer["connection"] == "disconnected", (
            "a channel nobody serves must report disconnected, which means the provider's "
            f"disconnect notice actually arrived; 'unknown' would mean it did not: {answer}"
        )
        assert answer["total_events"] == 0
        detail = answer.get("connection_detail")
        assert isinstance(detail, str) and "not reachable" in detail, (
            "an empty monitor result must say WHY it is empty, in the words the disconnected "
            f"branch uses, otherwise it reads as a quiet PV: {detail!r}"
        )


class TestDiscover:
    """``discover_pvs``: two branches, two planes, and a glob a FOREIGN server interprets."""

    async def test_the_registry_glob_is_anchored_and_case_insensitive(self, glob_core: str) -> None:
        """The three claims the tool description makes about somebody else's server.

        Anchoring and case-insensitivity are decided by the ChannelFinder server, so no test in
        this repository can establish them from our own code, and the description states them as
        measured facts. Here they are re-measured: wrapped in stars the fragment matches; the same
        fragment upper-cased matches the same set; and trailing-star-only, which is still a glob
        and still goes to the registry, matches nothing because the pattern is anchored at the
        front. The negative control is the third one, and it is the reason a bare substring reads
        as "no such channel" rather than as a syntax mistake.
        """
        wrapped = await discover_pvs(f"*{glob_core}*", _READ_TIMEOUT)
        assert wrapped["source"] == "channelfinder"
        # The plane, not its scope: which world the registry lives in is a deployment decision,
        # while "the registry answered this branch" is the claim under test.
        assert set(wrapped["reach"]["planes"]) == {"channelfinder"}
        matches = wrapped["pvs"]
        assert isinstance(matches, list) and matches, (
            "EPICS_MCP_LIVE_READ_GLOB_CORE matched no registered channel when wrapped in stars"
        )
        assert all(entry.get("status") == "registered" for entry in matches), (
            "a registry hit is 'registered', never the live-connect 'found'"
        )

        upper = await discover_pvs(f"*{glob_core.upper()}*", _READ_TIMEOUT)
        assert {entry["pv_name"] for entry in upper["pvs"]} == {
            entry["pv_name"] for entry in matches
        }, "the server glob is documented as case-insensitive and answered differently"

        # Anchoring, and the two halves are needed TOGETHER. "a fragment without a leading star
        # matches nothing" on its own is also satisfied by a fragment that simply is not a prefix
        # of anything, which is a property of the chosen value rather than of the server. So the
        # same query form is run twice: once from a REAL channel name, which must match, and once
        # from the mid-name fragment, which must not. Only a server that anchors answers that way.
        real_name = matches[0]["pv_name"]
        assert isinstance(real_name, str)
        from_the_start = await discover_pvs(f"{real_name}*", _READ_TIMEOUT)
        assert from_the_start["total"] >= 1, (
            "a trailing-star query built from a real channel name must match that channel; if it "
            "does not, the query form itself is broken and the anchoring claim below proves "
            f"nothing: {from_the_start}"
        )

        anchored = await discover_pvs(f"{glob_core}*", _READ_TIMEOUT)
        assert anchored["total"] == 0, (
            "the server glob is anchored at the front: the same trailing-star form that just "
            "matched from a real name must match nothing from a mid-name fragment, yet it "
            f"returned {anchored['total']}"
        )

    async def test_a_concrete_absent_name_is_classified_not_emptied(self) -> None:
        """The live branch, negative control.

        A concrete name is connected rather than looked up, and the classification has to survive:
        a timeout, a not-found and a connection error are three different operational situations,
        and collapsing them into an empty answer would make a diagnosable outage look like a PV
        that never existed. The tool must also keep saying which plane answered.
        """
        answer = await discover_pvs(_ABSENT_PV, _ABSENT_TIMEOUT)

        assert answer["total"] == 0
        entries = answer["pvs"]
        assert isinstance(entries, list) and len(entries) == 1
        assert entries[0]["status"] in {"not_found", "timeout", "error"}, (
            f"an unreachable concrete name must keep its classification: {entries[0]}"
        )
        assert set(answer["reach"]["planes"]) == {"live-pv"}, (
            "a concrete name is answered by the live plane, and the answer must say so"
        )


class TestDiagnose:
    """``diagnose_connection``: whether two real planes tell the same story about one channel."""

    async def test_the_diagnosis_and_the_registry_name_the_same_serving_ioc(self, pv: str) -> None:
        """Two routes into the registry, crossed, plus the live verdict beside them.

        ⛔ SAID PRECISELY, because the obvious reading is wrong and was written here first: this
        does NOT cross the gateway against the registry. The live connect decides ``state`` and
        never reports a serving IOC, so no probe can hold the answering server against the
        registry's opinion of it from here. What IS crossed are two different routes to the same
        registry: the diagnosis takes the exact-name lookup, the independent call takes the glob
        search, and the two run through separate mappings of the payload. A drift between those
        mappings, or a registry that answers differently depending on the query form, shows up as
        two different IOC names for one channel.

        Beside it stands the live verdict, ``connected`` and ``healthy``, which is offline only
        ever produced from handwritten evidence objects. Here it comes from a real connect.
        """
        report = await diagnose_connection(pv, _READ_TIMEOUT)

        assert report["state"] == "connected", f"the target PV must be reachable: {report}"
        assert report["likely_cause"] == "healthy"

        evidence = _mapping(report["evidence"], "the evidence block")
        registry = _mapping(evidence["channelfinder"], "the registry evidence")
        assert_live_available(
            bool(registry.get("consulted")) and not registry.get("withheld"),
            "the channel registry was not consulted, so there is no second plane to cross "
            "against; set EPICS_MCP_CHANNELFINDER_URL for this probe",
            demanded=live_demanded(os.environ),
        )

        independent = await discover_pvs(pv, _READ_TIMEOUT)
        assert independent["pvs"][0]["status"] == "found"

        by_glob = await discover_pvs(f"*{pv}*", _READ_TIMEOUT)
        registered = [entry for entry in by_glob["pvs"] if entry["pv_name"] == pv]
        assert len(registered) == 1, f"the registry must know this channel exactly once: {by_glob}"
        assert registered[0]["ioc_name"] == registry["ioc_name"], (
            f"the diagnosis names IOC {registry['ioc_name']!r} while the registry lookup names "
            f"{registered[0]['ioc_name']!r} for the same channel"
        )

    async def test_an_absent_pv_is_diagnosed_rather_than_raising(self) -> None:
        """The negative control. A disconnected PV is the NORMAL input of a diagnosis tool.

        If this raised, the tool would be unusable for the situation it was written for. The cause
        must also not come back healthy, and it must stay inside the declared vocabulary rather
        than inventing a value for a case the offline tests never saw.
        """
        report = await diagnose_connection(_ABSENT_PV, _ABSENT_TIMEOUT)

        assert report["state"] != "connected"
        assert report["likely_cause"] != "healthy"
        assert report["likely_cause"] in {
            "ioc_down",
            "name_typo",
            "unregistered",
            "indeterminate",
        }, f"the cause must stay inside the declared vocabulary: {report['likely_cause']!r}"
