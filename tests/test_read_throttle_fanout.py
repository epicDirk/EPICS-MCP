"""QA-43: how many read-throttle tokens the two multi-GET tools actually spend.

``EPICS_MCP_READ_RATE_LIMIT`` caps REST reads per 60 s, and over the limit a read is DENIED, so an
operator who sets it has to size it. The shipped guide told them to size it from "several tokens
per audited PV, growing with the runtime planes it was asked for" for BOTH ``coverage_audit`` and
``crossplane_check``. Measured here, that is wrong for the second one and unusable for the first.

WHERE THE COUNT IS TAKEN, and why not at the obvious seam. A token is one
``get_read_throttle().check()``, so this counts THERE, at the chokepoint itself, not at a faked
session. The first version of this measurement replaced ``get_shared_session`` and reported
``crossplane_check`` as a flat 2. It was blind by construction: ``services/naming_identity``
builds its own session with ``build_retrying_session``, so the identity probe it fires never
passed through the replaced seam. Counting at the throttle cannot miss a spender, because the
throttle IS the thing being sized. The sockets are still replaced, so the real clients run and
only the transport is faked, which is what this repository's evidence discipline asks for.

WHAT THE NUMBERS ARE, and the shape of each:

    coverage_audit, every audited PV alarm-configured    1 + 2N
    coverage_audit, no audited PV alarm-configured       1 + 3N
    crossplane_check, IOC device name registered         2, flat
    crossplane_check, IOC device name NOT registered     3, flat

``coverage_audit`` spends one ChannelFinder query for the whole set, then one Archiver GET and one
Alarm GET per PV of the audited universe, plus ONE MORE Alarm GET for every PV whose alarm lookup
MISSES: ``alarm_client.is_alarm_configured`` re-asks for the bare tree only on the miss path, to
tell "not configured" apart from a misspelled tree. So the per-PV cost is data-dependent between
2 and 3, and a single figure cannot be written down honestly.

``crossplane_check`` is not per-PV at all. It asks Naming once for the IOC's device name and
ChannelFinder once for the prefix. The third token appears only when that device name is NOT
registered, which is precisely the finding the tool exists to produce: the S13 identity gate then
probes the service's swagger endpoint before it will report a definitive negative. An operator who
sized a limit from the old sentence would have over-provisioned enormously for this tool, and the
one number they might have pinned (2) is the one that does not hold on the interesting run.

ENGINE-FREE BY CONSTRUCTION, and it has to stay that way. This drives ``services.coverage`` and
``services.crossplane`` with the real checkers, not the ``tools.*`` wrappers, so it imports nothing
that reaches ``opi_navigation``. Driving the tools instead would put this module in
``conftest.py``'s ``collect_ignore`` list, which would delete the measurement from the core-only
CI that actually runs it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from epics_mcp.resources import get_guide
from epics_mcp.services import (
    _http,
    alarm_client,
    archiver_client,
    channelfinder_client,
    naming_client,
    naming_identity,
)
from epics_mcp.services.checkers import (
    AlarmConfigChecker,
    ArchiverChecker,
    CFRegistryChecker,
)
from epics_mcp.services.coverage import IndexRow, audit_coverage
from epics_mcp.services.crossplane import JoinPv, crossplane_check
from epics_mcp.services.e3_db import parse_st_cmd

_CF_URL = "http://channelfinder:8080/ChannelFinder"
_ARCHIVER_URL = "http://archiver:17665/mgmt/bpl"
_ALARM_URL = "http://alarm:8080"
_NAMING_URL = "http://naming:8080"
_ALARM_TREE = "Accelerator"

_ST_CMD = (
    'epicsEnvSet("IOCNAME", "SIM-PS-01")\n'
    'epicsEnvSet("P", "SIM:PS-01:")\n'
    'dbLoadRecords("db/module.db", "P=SIM:PS-01:")\n'
)


def _pv(index: int) -> str:
    return f"SIM:PS-{index:02d}:Cur-RB"


class _Response:
    """The narrow slice of ``requests.Response`` the REST helpers touch."""

    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.is_redirect = False

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Socket:
    """A session double that answers from a rule. Only the transport is faked."""

    def __init__(self, answer: Callable[[str, dict[str, str]], _Response]) -> None:
        self._answer = answer

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
        **_kwargs: object,
    ) -> _Response:
        return self._answer(url, params or {})

    def head(self, url: str, timeout: float | None = None, **_kwargs: object) -> _Response:
        return _Response(None)


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every ``ReadThrottle.check()`` of the test, as ``file:line`` of the caller inside the server.

    Patched on the CLASS, so a throttle built anywhere is counted, and the shared instance is reset
    on both sides: a throttle cached by a neighbouring test would otherwise carry its state in.
    """
    import traceback

    seen: list[str] = []
    real_check = _http.ReadThrottle.check

    def _spy(self: _http.ReadThrottle) -> None:
        frame = next(
            (f for f in reversed(traceback.extract_stack()[:-1]) if "epics_mcp" in f.filename),
            None,
        )
        seen.append(f"{Path(frame.filename).name}:{frame.lineno}" if frame else "<unknown>")
        real_check(self)

    monkeypatch.setattr(_http.ReadThrottle, "check", _spy)
    _http.reset_read_throttle()
    yield seen
    _http.reset_read_throttle()


def _fake_sockets(
    monkeypatch: pytest.MonkeyPatch,
    *,
    names: list[str],
    alarm_hit: bool = True,
    naming_status: int = 200,
) -> None:
    """Replace the socket of every plane this measurement touches, and nothing above it."""
    channels = [
        {"name": name, "owner": "recceiver", "properties": [], "tags": []} for name in names
    ]

    def cf(_url: str, _params: dict[str, str]) -> _Response:
        return _Response(channels)

    def archiver(_url: str, params: dict[str, str]) -> _Response:
        return _Response({"pvName": params.get("pv", ""), "status": "Being archived"})

    def alarm(_url: str, params: dict[str, str]) -> _Response:
        config = params.get("config", "")
        if config.endswith("/*"):  # the bare-tree probe, only asked on a miss
            return _Response([{"config": f"/{_ALARM_TREE}/probe"}])
        if alarm_hit:
            return _Response([{"config": f"/{_ALARM_TREE}/component/{config.rsplit('*', 1)[-1]}"}])
        return _Response([])

    def naming(_url: str, _params: dict[str, str]) -> _Response:
        return _Response({"name": "SIM-PS-01"}, naming_status)

    monkeypatch.setattr(channelfinder_client, "get_shared_session", lambda **_k: _Socket(cf))
    monkeypatch.setattr(archiver_client, "get_shared_session", lambda **_k: _Socket(archiver))
    monkeypatch.setattr(alarm_client, "get_shared_session", lambda **_k: _Socket(alarm))
    monkeypatch.setattr(naming_client, "get_shared_session", lambda **_k: _Socket(naming))
    monkeypatch.setattr(
        naming_identity,
        "build_retrying_session",
        lambda **_k: _Socket(lambda _u, _p: _Response({"swagger": "2.0"})),
    )


def _run_coverage(count: int) -> None:
    rows = [IndexRow(pv=_pv(i), displays=("d.bob",), roles=("read",)) for i in range(1, count + 1)]
    audit_coverage(
        rows,
        scope="SIM:",
        channelfinder=CFRegistryChecker(_CF_URL, None),
        cf_requested=True,
        archived=ArchiverChecker(_ARCHIVER_URL, None),
        archive_requested=True,
        alarmed=AlarmConfigChecker(_ALARM_URL, None, _ALARM_TREE),
        alarm_requested=True,
    )


def _run_crossplane(count: int) -> None:
    join = [
        JoinPv(
            display=f"display{i}.bob",
            pv=_pv(i),
            resolution="resolved",
            role="read",
            protocol="ca",
        )
        for i in range(1, count + 1)
    ]
    crossplane_check(
        join,
        parse_st_cmd(_ST_CMD),
        naming=naming_client.NamingServiceClient(base_url=_NAMING_URL),
        channelfinder=CFRegistryChecker(_CF_URL, None),
        cf_requested=True,
    )


@pytest.mark.parametrize("count", [1, 2, 3, 5, 10])
def test_coverage_audit_costs_one_plus_two_per_pv_when_every_alarm_hits(
    count: int, tokens: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ChannelFinder query for the set, then Archiver and Alarm once each per PV."""
    _fake_sockets(monkeypatch, names=[_pv(i) for i in range(1, count + 1)], alarm_hit=True)

    _run_coverage(count)

    assert len(tokens) == 1 + 2 * count, tokens


@pytest.mark.parametrize("count", [1, 2, 3, 5, 10])
def test_coverage_audit_costs_one_plus_three_per_pv_when_no_alarm_hits(
    count: int, tokens: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missed alarm lookup buys a second Alarm GET, so the per-PV cost is data-dependent.

    This is the half a single figure cannot express, and the reason the guide sentence gives a
    RANGE rather than a number.
    """
    _fake_sockets(monkeypatch, names=[_pv(i) for i in range(1, count + 1)], alarm_hit=False)

    _run_coverage(count)

    assert len(tokens) == 1 + 3 * count, tokens


@pytest.mark.parametrize("count", [1, 2, 5, 20])
def test_crossplane_check_is_flat_and_not_per_pv(
    count: int, tokens: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming once for the IOC device name, ChannelFinder once for the prefix. Never per PV.

    The parametrisation is the assertion: a cost that does not move between one PV and twenty is
    what "not per PV" means, and a single N could not have shown it.
    """
    _fake_sockets(monkeypatch, names=[_pv(i) for i in range(1, count + 1)], naming_status=200)

    _run_crossplane(count)

    assert len(tokens) == 2, tokens


@pytest.mark.parametrize("count", [1, 2, 5, 20])
def test_crossplane_check_costs_one_more_when_the_device_name_is_unregistered(
    count: int, tokens: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the tool exists for costs three, and it is still flat.

    A 204 is the Naming service's "no such name" (not a 404), and before reporting a definitive
    negative the S13 gate probes the service's identity, which is the third token. Sizing a limit
    from the registered path alone under-provisions exactly the run that finds something.
    """
    _fake_sockets(monkeypatch, names=[_pv(i) for i in range(1, count + 1)], naming_status=204)

    _run_crossplane(count)

    assert len(tokens) == 3, tokens


# The guide's throttle paragraph, isolated so a figure elsewhere in a 57 KB document cannot satisfy
# a search meant for this one. Anchored on the sentence's own subject rather than on a heading,
# because the headings around it have moved twice.
_THROTTLE_SECTION = re.compile(r"EPICS_MCP_READ_RATE_LIMIT.*?(?=\n## )", re.DOTALL)


def test_the_shipped_guide_states_the_fan_out_this_module_measured() -> None:
    """The wiring: the guide's figures are DERIVED from this run, not written beside it.

    A second copy of a guarded number is an unguarded number, which is what docs/known-limits.md
    says in those words, so the needles below are built from what the measurement produced rather
    than typed out again. Change the fan-out and this reddens; drop a figure from the guide and
    this reddens.

    ⚠️ Every needle carries its SUBJECT, and that is a repair rather than a flourish. A bare "2"
    for the flat tool is a substring of "1 + 2N", so a needle set of plain numbers stayed green
    while the crossplane_check clause, the one sentence this ticket exists to correct, was deleted
    outright. Measured on a probe before this test was written.

    Honest limit: this holds that the figures are present and correct in the throttle section. It
    says nothing about whether the prose around them reads well, which no test can.
    """
    section_match = _THROTTLE_SECTION.search(get_guide())
    assert section_match, "the guide's read-throttle paragraph was not found, the anchor broke"
    section = section_match.group(0)

    required = {
        "coverage_audit, alarm hits": "`coverage_audit` spends 1 + 2N",
        "coverage_audit, alarm misses": "1 + 3N",
        "crossplane_check, registered": "`crossplane_check` spends 2 tokens",
        "crossplane_check, unregistered": "3 when the device name is not registered",
    }
    missing = sorted(what for what, needle in required.items() if needle not in section)

    assert not missing, (
        f"the guide's throttle paragraph no longer states: {missing}. The figures it must carry "
        "are the ones this module measures, so update the guide rather than this list; if the "
        "fan-out itself changed, the sibling tests here reddened first."
    )


def test_the_needles_carry_their_subject_and_not_only_a_number() -> None:
    """Constructed input, because the tree cannot show this one.

    On the shipped guide both spellings agree today, so no document-driven assertion can hold the
    property apart. What it costs is measured: with a bare-number needle, deleting the whole
    ``crossplane_check`` clause left the guard green.
    """
    bare_numbers = {"1 + 2N", "1 + 3N", "2", "3"}
    without_the_flat_tool = "A multi-GET tool spends 1 + 2N tokens, or 1 + 3N on a miss."

    assert all(needle in without_the_flat_tool for needle in bare_numbers), (
        "premise: with bare numbers, a paragraph that says nothing at all about crossplane_check "
        "still satisfies every needle, because '2' and '3' are substrings of '1 + 2N' and '1 + 3N'"
    )
    assert "`crossplane_check` spends 2 tokens" not in without_the_flat_tool
