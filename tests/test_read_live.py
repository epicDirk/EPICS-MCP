"""Live probes for the PV READ plane, the surface this whole server exists for.

Opt-in: ``pytest tests/test_read_live.py -m live`` with a PVA search lane in
``EPICS_MCP_LIVE_READ_PVA_ADDR_LIST`` and a readable PV in ``EPICS_MCP_LIVE_READ_PV``.

SCOPE EVERY RUN TO THIS FILE. ``pyproject.toml`` sets ``testpaths = ["tests"]`` and declares no
``addopts``, so a bare ``pytest -m live`` falls back to the whole directory and collects ALL live
modules. Three of those write real logbook entries into a service with no delete. Nothing in this
repository guards against the missing path; the path itself is the guard.

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
exception, and the reach clause on the ERROR path is asserted here for the first time against a
real plane. The eight sibling modules call the service layer and therefore never see either.

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
    return value


async def _still_answering(pv_name: str, timeout: float) -> dict[str, object]:
    """Read *pv_name* and turn a MID-RUN plane outage into a named skip instead of an assertion.

    Without this, a facility that stops answering halfway through produces an ordinary assertion
    failure, indistinguishable in the report from a defect in this repository. The message says
    which it is, and the demand switch decides whether that is a skip or a red. Same in-probe gate
    the write module uses when a record turns out to lack drive limits: a data-dependent outcome
    inside a running live probe is its own class.
    """
    try:
        return await get_pv_value(pv_name, timeout)
    except ToolError as exc:
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

    async def test_a_mixed_batch_keeps_the_readable_and_reports_the_unreadable(
        self, pv: str, second_pv: str
    ) -> None:
        """Positive and negative control in ONE call, which is what makes it differential.

        The wire contract says a per-PV read failure lands in ``errors``. Offline, only the
        FALLBACK path was ever exercised with a missing channel: the native batch is faked whole.
        On the native path the provider decides what a missing entry looks like, and the formatter
        underneath never raises, so a mis-shaped entry could arrive as a RESULT carrying a
        placeholder instead of as an error. That would silently turn an unreadable channel into a
        readable-looking one. The count assertion is the guard: results plus errors must account
        for every name submitted, with the readable ones on one side and the absent one on the
        other.
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
        """Both halves are live-only claims, and the second is the one that matters.

        The two tools share their whole implementation; the only thing info adds is a status key.
        Pinning that against a REAL answer keeps the pair honest: a future divergence between them
        shows up here rather than in a reader's expectations. The second half is what a mock cannot
        say at all: that a real facility populates at least one metadata block. Offline, every
        block is whatever the fixture author typed.
        """
        value_answer = await _still_answering(pv, _READ_TIMEOUT)
        info_answer = await get_pv_info(pv, _READ_TIMEOUT)

        # Both answers carry a fresh timestamp, so compare the KEY SETS, never the values.
        extra = set(info_answer) - set(value_answer)
        assert extra == {"status"}, (
            f"get_pv_info is expected to add exactly the status key, it added {extra}"
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
        """The positive control, stated the way the code can actually satisfy it.

        ``connected`` is set in the same locked block that appends the event, so connected implies
        at least one event and the two are asserted together. The complementary half is that a
        healthy run carries NO detail line: the detail key exists to explain an empty or odd
        result, and its presence on a healthy channel would be noise the reader has to discount.
        """
        await _still_answering(pv, _READ_TIMEOUT)
        answer = await monitor_pv(pv, _MONITOR_DURATION, 5)

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

    async def test_an_absent_channel_explains_its_silence(self) -> None:
        """The negative control, and the pairing is the actual claim.

        Zero events used to mean either "quiet PV" or "no such PV". The connection field separates
        them, and this is the first probe that asks a REAL provider to produce the disconnect
        notice the separation rests on. The assertion is the COUPLING: not connected, no events,
        and an explanation present. Which non-connected value arrives is left open, since a
        provider that sends no notice at all honestly yields unknown.
        """
        answer = await monitor_pv(_ABSENT_PV, _MONITOR_DURATION, 5)

        assert answer["connection"] != "connected", (
            f"a channel nobody serves must not report as connected: {answer}"
        )
        assert answer["total_events"] == 0
        assert answer.get("connection_detail"), (
            "an empty monitor result must say WHY it is empty, otherwise it reads as a quiet PV"
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

        anchored = await discover_pvs(f"{glob_core}*", _READ_TIMEOUT)
        assert anchored["total"] == 0, (
            "the server glob is anchored at the front, so a fragment without a leading star "
            f"cannot match, yet it returned {anchored['total']}"
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
        """The cross-plane claim, and the only one of these probes that needs two services.

        The live connect decides the verdict and the registry only explains it, which means the
        two can disagree without anything raising. Offline that disagreement is unreachable: the
        evidence objects are handwritten, so both halves always agree by construction. Here the
        registry entry is fetched INDEPENDENTLY through the other tool and the serving IOC has to
        match. A gateway answering for a channel the registry attributes to a different IOC is a
        real and diagnosable condition, and this is what would surface it.
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
