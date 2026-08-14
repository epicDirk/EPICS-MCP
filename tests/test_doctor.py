"""Offline tests for the read-only config self-check (services/doctor + cli_doctor), no network.

Every test is hermetic: the config is patched to a fresh EpicsConfig and each client class is
replaced by a fake, so the 'not live' suite makes no network call. Covers the 3-bucket classifier
(Plan-QA #1: a served non-2xx is api_error/reachable, not unreachable), the disabled/ok/failing
planes, the single-source privacy report, the live plane's no-default-egress posture (Plan-QA #4),
and the CLI exit-code convention (0 clean / 1 a plane hard-failed / 2 usage / 3 inconclusive: an
identity probe that FAILED, S12).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock

import pytest
import requests

from epics_mcp import cli_doctor
from epics_mcp.config import EpicsConfig
from epics_mcp.errors import (
    EpicsError,
    OlogWriteDeniedError,
    PVWriteDeniedError,
    SafetyConfigError,
)
from epics_mcp.olog_safety import OlogWriteGate, write_target_allowed
from epics_mcp.safety import SafetyLayer
from epics_mcp.services import doctor
from epics_mcp.services._http import url_without_credentials
from epics_mcp.services.doctor import (
    _DEGRADED_STATUSES,
    _FAILING_STATUSES,
    _INCONCLUSIVE_STATUSES,
    _NON_FAILING_STATUSES,
    _REMEDY,
    _REMEDY_IMPERATIVES,
    DoctorReport,
    PlaneCheck,
    PlaneStatus,
    PrivacyReport,
    WriteSafetyReport,
    _check_retrieval_plane,
    _classify_failure,
    _identify,
    _identify_alarm,
    _identify_archiver,
    _identify_naming,
    _identify_retrieval_plane,
    _privacy_report,
    _probe_audit_sink,
    _safe,
    _with_remedy,
    _write_safety_report,
    run_doctor,
)
from epics_mcp.services.rest_exceptions import RestConnectionError, RestResponseError
from epics_mcp.write_posture import (
    _ALLOW_EVERY_PV_NAME,
    olog_write_gate_report,
    pv_write_gate_report,
)


def _set_config(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> EpicsConfig:
    """Point doctor's config read at a fresh EpicsConfig with the given fields."""
    cfg = EpicsConfig(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr("epics_mcp.services.doctor.get_config", lambda: cfg)
    return cfg


def _cause_client(cause: BaseException) -> type:
    """A fake REST client whose check_connectivity raises with *cause* chained."""

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_connectivity(self) -> bool:
            raise RuntimeError("probe failed") from cause

    return _Client


class _OkClient:
    """A fake REST client that reports reachable."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def check_connectivity(self) -> bool:
        return True


def _plane(report: DoctorReport, name: str) -> PlaneCheck:
    return next(p for p in report.planes if p.plane == name)


#: EVERY EPICS_MCP_* field the write block reads, so a test that wants a DEFAULT posture gets one
#: on any machine. ``tests/conftest.py`` strips the six EPICS search vars and deliberately not
#: these, so without an explicit value a developer who exports one for their own sandbox sees this
#: file go red for a reason that is not in it.
#:
#: ⚠️ The list has to be COMPLETE, and the first version was not: it omitted ``olog_url``, which the
#: block reads three times. Measured with ``EPICS_MCP_OLOG_URL`` exported: three of these tests went
#: red, the block under test reported ``target_is_loopback: true`` from a leaked environment, and
#: the suite additionally made real network calls to that URL. Anything the block reads belongs
#: here, and ``test_the_write_gate_defaults_cover_every_field_the_block_reads`` says so mechanically
#: rather than leaving it to the next author to remember.
_WRITE_GATE_DEFAULTS: dict[str, object] = {
    "allow_pv_write": False,
    "pv_write_pattern": "",
    "write_rate_limit": 10,
    "allow_olog_write": False,
    "olog_write_logbooks": "",
    "olog_write_rate_limit": 5,
    "olog_write_url_allowlist": "",
    "olog_write_allow_remote": False,
    "olog_url": "",
    "audit_log_file": "",
}


@contextlib.contextmanager
def _isolated_audit_loggers() -> Iterator[None]:
    """Build a real write gate without leaving a handler on a process-global logger.

    Both gates attach to ``epics_mcp.audit`` / ``epics_mcp.olog_audit`` and dedup with
    ``if not audit.handlers``, so a handler left behind makes a LATER test's own FileHandler never
    be attached, and it is bound to this test's captured stderr, which is gone by then. Every gate
    test in ``test_safety.py`` and ``test_olog_write.py`` does this dance (twelve sites); the first
    version of the tests below did not, and measured, it left a StreamHandler and ``level=INFO`` on
    both loggers where a pristine run has none.
    """
    saved = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).level)
        for name in ("epics_mcp.audit", "epics_mcp.olog_audit")
    }
    for name in saved:
        logging.getLogger(name).handlers.clear()
    try:
        yield
    finally:
        for name, (handlers, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.handlers.extend(handlers)
            logger.setLevel(level)


def _write_config(**overrides: object) -> EpicsConfig:
    """An EpicsConfig whose write-gate fields are PINNED, never inherited from the environment."""
    return EpicsConfig(**{**_WRITE_GATE_DEFAULTS, **overrides})  # type: ignore[arg-type]


def _disarmed_write_safety() -> WriteSafetyReport:
    """The write posture of a default configuration: both gates off, sink on stderr."""
    return _write_safety_report(_write_config())


#: A search environment a write-enabled server is allowed to start in. Both providers, because the
#: write gate demands both (the live plane's own posture line asks only about the active one, which
#: is why the two can disagree and why the write block computes its own answer).
_LOOPBACK_ENV = {"EPICS_PVA_AUTO_ADDR_LIST": "NO", "EPICS_CA_AUTO_ADDR_LIST": "NO"}


# --- _classify_failure (the 3-bucket core) ---


#: The variable a probed plane reads its URL from, as ``_run_probe`` threads it in. Passed
#: explicitly by the four tests below so the observation half of each detail is checked with a
#: KNOWN name in it.
_PROBE_VAR = "EPICS_MCP_CHANNELFINDER_URL"


def test_classify_ssl_error_is_ca_error() -> None:
    exc = RuntimeError("x")
    exc.__cause__ = requests.exceptions.SSLError("bad cert")
    reachable, ca_ok, status, detail = _classify_failure(exc, _PROBE_VAR)
    assert (reachable, ca_ok, status) == (False, False, "ca_error")
    assert "CA_BUNDLE" in detail
    # C1: the remedy reached the message. NOT a second spelling of the assertion above: CA_BUNDLE
    # lives in the remedy now, so this is what tells a reader whether the whole entry arrived or
    # only the variable name a hand-written string happened to keep.
    assert _REMEDY["ca_error"] in detail


def test_classify_served_non2xx_is_api_error_reachable() -> None:
    """Plan-QA #1: a served non-2xx is 'api_error' (reachable), NOT 'unreachable'."""
    http_err = requests.exceptions.HTTPError("404")
    http_err.response = Mock(status_code=404)
    exc = RuntimeError("x")
    exc.__cause__ = http_err
    reachable, ca_ok, status, detail = _classify_failure(exc, _PROBE_VAR)
    assert (reachable, ca_ok, status) == (True, True, "api_error")
    assert "404" in detail
    assert _PROBE_VAR in detail  # WHICH variable served the 404, not just that something did
    # The actionable payload: the webapp hint is what distinguishes api_error from unreachable, and
    # BOTH webapps are named. This used to assert "not retrieval", pinning a remedy that told every
    # plane the mgmt one was correct; on the retrieval plane that named the webapp it had just
    # probed (BG-DFIX). The pair is asserted rather than the direction, so the hint has to stay
    # present without the guard deciding which of the two a given plane should point at.
    assert "mgmt" in detail
    assert "retrieval" in detail
    assert _REMEDY["api_error"] in detail  # C1


def test_classify_retry_error_is_api_error() -> None:
    """A retry-exhausted 502/503/504 (chained RetryError, no .response) is api_error (reachable),
    NOT unreachable: the host answered repeatedly with a 5xx.

    C1 red-proof note: this is the SECOND api_error return, and a remedy guard parametrized over
    statuses would have covered only the first (one row per status). Dropping ``_with_remedy`` from
    this branch alone leaves every other assertion in this file green, so the remedy assertion below
    is the only thing standing between that mutant and a clean suite.
    """
    exc = RuntimeError("x")
    exc.__cause__ = requests.exceptions.RetryError("too many 503 error responses")
    reachable, ca_ok, status, detail = _classify_failure(exc, _PROBE_VAR)
    assert (reachable, ca_ok, status) == (True, True, "api_error")
    assert "5xx" in detail
    assert _PROBE_VAR in detail
    assert _REMEDY["api_error"] in detail  # C1


def test_classify_transport_failure_is_unreachable() -> None:
    exc = RuntimeError("x")
    exc.__cause__ = requests.exceptions.ConnectionError("refused")
    reachable, ca_ok, status, detail = _classify_failure(exc, _PROBE_VAR)
    assert (reachable, ca_ok, status) == (False, None, "unreachable")
    assert "could not reach" in detail
    # C1: an unreachable plane is the one case where the reader cannot guess which of the seven
    # variables to look at, so the name is part of the finding, not only of the remedy.
    assert _PROBE_VAR in detail
    assert _REMEDY["unreachable"] in detail


# --- run_doctor: disabled / reachable / failing planes ---


@pytest.fixture(autouse=True)
def _identity_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the identity probes for every run_doctor test, AUTOUSE, deliberately.

    The identity probe issues its own GET (it does not go through the mocked client classes), so
    without this a single reachable plane in an offline test would resolve a hostname for real. The
    wiring tests below care about the transport classification, not the identity logic, so they get
    a benign "identified" stub; the identity logic itself is unit-tested against a patched
    ``rest_get_json`` further down, where the full result matrix lives.
    """

    def _identified(plane: str, *_args: object, **_kwargs: object) -> PlaneCheck:
        return PlaneCheck(
            plane=plane, configured=True, reachable=True, ca_ok=True, status="ok", identified=True
        )

    # EVERY identity probe must be stubbed. Each one issues its own GET, so a single unstubbed
    # entry point silently reintroduces network into the offline suite.
    #
    # rest_get_json is stubbed too, and that one is not belt-and-braces: the retrieval plane calls
    # it DIRECTLY as its transport probe, outside the mocked client classes. Measured before this
    # line existed: test_archiver_api_error_is_reachable_not_unreachable spent 12.1s of the suite's
    # 17s resolving a fake hostname, passing, silently, over the network. A hermetic test that is
    # merely slow is how "no network" rots.
    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", lambda *_a, **_k: {})
    monkeypatch.setattr("epics_mcp.services.doctor._identify", _identified)
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify_alarm",
        lambda *_a, **_k: _identified("alarm"),
    )
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify_archiver",
        lambda *_a, **_k: _identified("archiver"),
    )
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify_naming",
        lambda *_a, **_k: _identified("naming"),
    )
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify_retrieval_plane",
        lambda *_a, **_k: _identified("archiver_retrieval"),
    )


async def test_all_disabled_is_ok_and_makes_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty config → every REST plane disabled, live=info, ok=True, and NO client is ever built."""
    _set_config(monkeypatch)  # all URLs empty
    boom = Mock(side_effect=AssertionError("no client must be built when disabled"))
    for name in (
        "ChannelFinderClient",
        "ArchiverClient",
        "AlarmClient",
        "NamingServiceClient",
        "OlogClient",
    ):
        monkeypatch.setattr(f"epics_mcp.services.doctor.{name}", boom)
    report = await run_doctor()
    assert report.ok is True
    assert {p.plane for p in report.planes} == {
        "live",
        "channelfinder",
        "archiver",
        "archiver_retrieval",
        "alarm",
        "naming",
        "olog",
    }
    for plane in report.planes:
        assert plane.status == ("info" if plane.plane == "live" else "disabled")
    # Nothing was left unproven because nothing was probed at all, verification_complete is
    # VACUOUSLY true here, and identified_planes carries the machine-readable difference between
    # "all confirmed" and "nothing ran": it must be empty.
    assert report.verification_complete is True
    assert report.unverified_planes == []
    assert report.identified_planes == []


async def test_reachable_plane_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, channelfinder_url="http://cf:8080/ChannelFinder")
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    report = await run_doctor()
    cf = _plane(report, "channelfinder")
    assert (cf.status, cf.reachable, cf.ca_ok) == ("ok", True, True)
    assert report.ok is True


# --- the identity probe: the full result matrix, offline (S4) ---
#
# WHY THIS EXISTS: "ok" used to mean only "check_connectivity did not raise". check_connectivity is
# a HEAD and counts ANY HTTP response as reachable, so a ChannelFinder URL pointing at a DEAD
# container reported "✓ channelfinder ok", because a different service on that port answered 401
# (its blanket auth answers 401 for every path, so the status said nothing about CF at all).
# These drive the REAL _identify against a patched rest_get_json: no network, full matrix.


def _payload(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    """Make the identity probe's GET return *value* (no network)."""
    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", lambda *a, **k: value)


def _raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make the identity probe's GET fail with *exc* (no network)."""

    def _boom(*_a: object, **_k: object) -> object:
        raise exc

    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", _boom)


def test_identity_exact_name_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"name": "Olog Service", "version": "6.0.4"})
    check = _identify("olog", "http://olog.example/Olog", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)


def test_identity_of_a_different_known_service_is_unverified_with_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S14: a foreign service name is "cannot confirm", never a hard failure.

    The earlier ``wrong_service``+exit-1 verdict rested on "a misconfiguration that is
    unambiguous at any site", refuted by measurement (2026-07-16): a path-based reverse
    proxy served the REAL ChannelFinder API while the base GET answered as ``Olog Service``,
    so the doctor failed a WORKING configuration. The found name must still surface in the
    detail (it is the actionable clue when the config IS wrong).
    """
    _payload(monkeypatch, {"name": "Olog Service"})
    check = _identify("channelfinder", "http://olog.example/Olog", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)
    assert "Olog Service" in (check.detail or "")
    assert "ChannelFinder Service" in (check.detail or "")
    assert "the name of the olog service" in (check.detail or "")  # the plane mapping survives
    # C1, on the OUTPUT: this state carries a remedy of its OWN ("if the config IS wrong, the name
    # here is the clue"), so none of the status-wide ones may be pasted on top of it.
    assert not any(remedy in (check.detail or "") for remedy in _REMEDY.values())
    assert check.status in _NON_FAILING_STATUSES  # honest doubt, exit 0
    # And the vocabulary itself is gone: a re-added dead Literal value (paired with its glyph)
    # would survive every functional test, since nothing emits it anymore.
    assert "wrong_service" not in get_args(PlaneStatus)


def test_identity_substring_is_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """A substring match would let this pass as Olog. The comparison is exact for that reason."""
    _payload(monkeypatch, {"name": "Not Olog Service"})
    check = _identify("olog", "http://olog.example/Olog", None, 5.0)
    assert check.status == "unverified"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"name": ""}, id="empty-name"),
        pytest.param({"name": 42}, id="non-string-name"),
        pytest.param({"version": "1.0"}, id="no-name-field"),
        pytest.param(["not", "a", "dict"], id="json-list"),
        pytest.param("<html>login</html>", id="html-body"),
        pytest.param(None, id="null-body"),
    ],
)
def test_identity_unusable_body_is_unverified(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """No usable name → unverified. NEVER ok, and never a failure either, it is a "don't know"."""
    _payload(monkeypatch, payload)
    check = _identify("alarm", "http://alarm.example", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)
    assert check.reachable is True  # the transport DID work; only identity is unproven
    assert check.status in _NON_FAILING_STATUSES


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(requests.exceptions.HTTPError("401"), id="auth-wall"),
        pytest.param(requests.exceptions.HTTPError("404"), id="not-found"),
        pytest.param(requests.exceptions.HTTPError("500"), id="server-error"),
        pytest.param(requests.exceptions.SSLError("bad cert"), id="tls-after-head"),
        pytest.param(requests.exceptions.ConnectionError("gone"), id="transport-after-head"),
        pytest.param(RuntimeError("refused to follow a redirect"), id="redirect-refused"),
    ],
)
def test_identity_failed_probe_is_identity_probe_failed(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """S12: a FAILED identity probe (a served non-2xx, a transport error, or a refused redirect) is
    ``identity_probe_failed``: NOT the honest ``unverified`` (that is for a 2xx answered-but-not-
    nameable). This is exactly where the 401 of the dead-container case lands: rest_get_json raises
    on a non-2xx BEFORE parsing, so an auth wall can never reach the name check, and it must no
    longer collapse to a silent exit 0. A TLS/transport failure DURING the identity GET is re-homed
    here too (the transport HEAD already proved reachability+CA to the same host).

    Red-proof: on the pre-fix code every case here was ``unverified`` (exit 0).
    """
    _raises(monkeypatch, exc)
    check = _identify("channelfinder", "http://cf.example/ChannelFinder", None, 5.0)
    assert (check.status, check.identified) == ("identity_probe_failed", False)
    assert check.status in _INCONCLUSIVE_STATUSES
    assert check.status not in _NON_FAILING_STATUSES  # NOT a silent all-clear
    # C1: through the REAL constructor, which is where the remedy is appended. This is the seam the
    # run_doctor test for this status cannot reach: that one patches _identify to return a
    # hand-built PlaneCheck, so it never executes _identity_probe_failed at all.
    assert _REMEDY["identity_probe_failed"] in (check.detail or "")


def test_identity_unreadable_2xx_body_stays_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """S12 boundary (FLAW A): a 200 whose body is NOT JSON (e.g. an HTML login page) is honest
    ``unverified``, NOT ``identity_probe_failed``. rest_get_json calls raise_for_status() BEFORE
    resp.json(), so reaching resp.json() means the status WAS 2xx; a non-JSON body surfaces as a
    ``JSONDecodeError`` (a ValueError subclass) chained via ``from exc``. The chained ValueError is
    how ``_beacon_reached_but_unreadable`` tells "the service ANSWERED, just not nameably" (exit 0)
    from "the probe FAILED" (exit 3).

    Red-proof: a mutant dropping the ValueError carve-out from ``_identity_fetch_failure`` makes
    this ``identity_probe_failed`` instead. Positive control for S14/anonymous: must NOT go red.
    """
    cause = json.JSONDecodeError("Expecting value", "<html>login</html>", 0)
    exc = requests.exceptions.RequestException("Request failed (http://cf.example): bad JSON body")
    exc.__cause__ = cause  # what rest_get_json chains on a 2xx-but-unparseable body
    _raises(monkeypatch, exc)
    check = _identify("channelfinder", "http://cf.example/ChannelFinder", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)
    assert check.reachable is True  # the endpoint answered 2xx; only identity is unproven
    assert check.status in _NON_FAILING_STATUSES  # honest, exit 0, never a failed probe


def test_identity_unreadable_2xx_raw_valueerror_stays_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S12 min-version robustness (diff-review R1): on the ``requests>=2.25`` floor a bad-JSON 2xx
    raises the STDLIB ``json.JSONDecodeError``, a ``ValueError`` but NOT a ``RequestException``, so
    ``rest_get_json`` does not wrap it and it arrives RAW (``__cause__`` is None). It must still be
    ``unverified`` (the service answered 2xx), so the discriminator checks the exception ITSELF.

    Red-proof: a discriminator that only inspects ``__cause__`` makes this identity_probe_failed.
    """
    raw = json.JSONDecodeError("Expecting value", "<html>login</html>", 0)  # __cause__ is None
    _raises(monkeypatch, raw)
    check = _identify("channelfinder", "http://cf.example/ChannelFinder", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)
    assert check.status in _NON_FAILING_STATUSES


# --- the archiver plane: identity, then INGEST (QA-35) ---
#
# WHY A SECOND SEAM HELPER: _payload above is `lambda *a, **k: value`, so it answers EVERY address
# with the same body. That was harmless while a plane made ONE request. The archiver now makes two
# over this same seam (getApplianceInfo, then getApplianceMetrics), and under _payload a probe that
# asked the WRONG route would still be handed the right-looking body and stay green. CLAUDE.md
# records that exact shape as a measured sham guard: faking at the right seam still does not say the
# right ADDRESS was requested (pointing list_tags at the properties URL left the whole suite green).
# So the address is recorded and asserted on its own.

_INFO_ROUTE = "/mgmt/bpl/getApplianceInfo"
_METRICS_ROUTE = "/mgmt/bpl/getApplianceMetrics"


class _RoutedGet:
    """A URL-keyed stand-in for ``rest_get_json`` that RECORDS what was asked for, and how.

    Routes are matched by URL suffix. A mapped value that is an ``Exception`` is raised, so a single
    route can fail while the other answers, which is the only way to exercise "the metrics call
    failed" (``_raises`` cannot: it fails the FIRST request, so the plane never reaches the ingest
    probe at all and the test would measure ``identity_probe_failed`` instead).

    An unknown URL returns ``None`` rather than raising, deliberately: the production code is TOTAL
    by design and would swallow the exception, producing the same verdict as a wrong CONDITION and
    hiding which of the two broke.
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = routes
        self.urls: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        url = str(args[1])
        self.urls.append(url)
        self.kwargs.append(dict(kwargs))
        for suffix, value in self._routes.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        return None

    def asked_for(self, suffix: str) -> bool:
        """Was a route ending in *suffix* actually requested? The address assertion."""
        return any(url.endswith(suffix) for url in self.urls)


def _routed(monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> _RoutedGet:
    """Install a URL-keyed ``rest_get_json`` and hand back the recorder."""
    fake = _RoutedGet(routes)
    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", fake)
    return fake


def _metrics_row(**overrides: object) -> dict[str, object]:
    """One ``getApplianceMetrics`` row in the REAL shape: every value a string.

    Field values follow the live payload measured 2026-07-29 (sandbox + a 16-member production
    cluster): counts are plain digit runs, ``eventRate`` is LOCALE formatted on a busy appliance
    (``"15,431.54"``), and ``status`` is ``"Working"`` when all three internal webapps answered.
    """
    row: dict[str, object] = {
        "instance": "appliance0",
        "pvCount": "5",
        "connectedPVCount": "5",
        "disconnectedPVCount": "0",
        "eventRate": "1.5",
        "status": "Working",
    }
    row.update(overrides)
    return row


def _archiver_check(monkeypatch: pytest.MonkeyPatch, metrics: object, **info: object) -> PlaneCheck:
    """Drive the REAL _identify_archiver over a routed seam, metrics route serving *metrics*."""
    identity: dict[str, object] = {"identity": "appliance0"}
    identity.update(info)
    _routed(monkeypatch, {_INFO_ROUTE: identity, _METRICS_ROUTE: metrics})
    return _identify_archiver("http://arch.example:17665", None, 5.0)


def test_archiver_identity_requires_the_identity_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_connectivity accepts ANY parseable 2xx JSON, an empty {} passes it. The appliance's
    own 'identity' field is what makes it an Archiver rather than "something served JSON here".

    Uses the ROUTED seam, not _payload: since the ingest probe exists, _payload would hand the
    getApplianceInfo body to the metrics call as well, so this test would silently exercise the
    "no unambiguous row" branch while claiming to be about identity only.
    """
    _routed(monkeypatch, {_INFO_ROUTE: {}})
    assert _identify_archiver("http://arch.example:17665", None, 5.0).status == "unverified"

    check = _archiver_check(monkeypatch, [_metrics_row()], engineURL="http://arch.example:17666")
    assert (check.status, check.identified) == ("ok", True)
    assert "appliance0" in (check.detail or "")


def test_archiver_holding_pvs_with_none_connected_is_no_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The QA-35 case, live-measured on the local sandbox: the appliance names itself, and archives
    nothing. Identity stays PROVEN, the plane is not a failure, and the reason is in the detail.

    Red-proof (pre-change code): _identify_archiver returned a bare ok PlaneCheck for exactly this
    body, which is the false all-clear this change removes. Mutants that survive the healthy case
    but die here: none, this is the positive case. See the mutant table in the sibling tests.
    """
    check = _archiver_check(
        monkeypatch,
        [_metrics_row(pvCount="5", connectedPVCount="0", disconnectedPVCount="5", eventRate="0")],
    )
    assert check.status == "no_ingest"
    assert check.identified is True  # identity WAS established; the failure is the ingest
    assert check.status in _NON_FAILING_STATUSES  # exit 0, by product decision
    detail = check.detail or ""
    assert "appliance0" in detail  # the identifying evidence survives the finding
    assert "pvCount=5" in detail and "connectedPVCount=0" in detail and "eventRate=0" in detail
    # C1, on the OUTPUT: a freshly commissioned appliance is legitimately in this state, and the
    # wiring that is missing sits inside it, so no EPICS_MCP_* advice belongs here.
    assert not any(remedy in detail for remedy in _REMEDY.values())


@pytest.mark.parametrize(
    ("row", "why"),
    [
        pytest.param(_metrics_row(), "every PV connected", id="healthy"),
        pytest.param(
            _metrics_row(pvCount="0", connectedPVCount="0", disconnectedPVCount="0"),
            "an empty or fully paused appliance holds no channels, which is not a fault",
            id="empty-appliance",
        ),
        pytest.param(
            _metrics_row(pvCount="5", connectedPVCount="3", disconnectedPVCount="2"),
            "PARTIAL connectivity is not no_ingest: production runs with 111..8939 disconnected",
            id="partial-connectivity",
        ),
    ],
)
def test_archiver_ok_cases(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, object], why: str
) -> None:
    """The three shapes that must NOT be reported as no_ingest.

    Red-proof (mutants killed here, each measured to SURVIVE without this case):
    ``connectedPVCount == 0`` -> ``< pvCount`` and a signal taken from ``disconnectedPVCount > 0``
    both die on "partial-connectivity"; ``pvCount > 0`` -> ``>= 0`` dies on "empty-appliance"
    (which is why that fixture carries an explicit connectedPVCount, without it the row never
    reaches the condition at all); ``and`` -> ``or`` dies on "healthy". Under the first two the
    tool would have reported ALL 16 production appliances as no_ingest.
    """
    check = _archiver_check(monkeypatch, [row])
    assert check.status == "ok", why
    assert check.identified is True


def test_archiver_engine_down_is_no_ingest_though_the_counts_are_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WORST archiver state, and the counts cannot see it.

    The connection counts are produced by the engine webapp and merged in by mgmt over HTTP. When
    that merge fails the counts VANISH from the row while pvCount survives, the appliance sets its
    own ``status`` to a "Stopped - ..." string, and it still serves HTTP 200. Without the status
    arm the guard could never fire in the state it matters most.

    Red-proof (mutation): drop the status arm and this goes green-as-``ok``, because the absent
    counts land in the "not measured" branch.
    """
    check = _archiver_check(
        monkeypatch,
        [{"instance": "appliance0", "pvCount": "5", "status": "Stopped - engine "}],
    )
    assert check.status == "no_ingest"
    detail = check.detail or ""
    assert "Stopped - engine" in detail
    assert "connectedPVCount=absent" in detail  # named as absent, never silently omitted


def test_archiver_picks_its_own_row_out_of_a_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-member body is the PRODUCTION NORM (16 rows measured), not an exception.

    The row is matched by ``instance`` against the identity that getApplianceInfo just reported
    (n=2: on the sandbox, and on a real 16-member cluster where the reported identity hit exactly
    one of the sixteen rows). Here OUR member is starved while the neighbours are healthy, so a
    positional read of the body would report ok.
    """
    cluster: list[object] = [
        _metrics_row(instance=f"appliance{n}", eventRate="15,431.54") for n in range(1, 17)
    ]
    cluster.insert(
        7, _metrics_row(instance="appliance0", connectedPVCount="0", disconnectedPVCount="5")
    )
    check = _archiver_check(monkeypatch, cluster)
    assert check.status == "no_ingest"
    assert "this member only" in (check.detail or "")  # the scope is stated, not implied


def test_archiver_locale_formatted_rate_never_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``eventRate`` is quoted, never parsed. On production it is locale formatted ("15,431.54"),
    and ``float()`` on that raises a ValueError that NOTHING in the call chain catches: _run_probe
    calls the identify callable outside its try block and cli_doctor.main catches only EpicsError,
    so it would surface as a raw traceback with no exit-code convention.

    Red-proof (mutation): parse eventRate with float() and this raises instead of returning.
    """
    check = _archiver_check(monkeypatch, [_metrics_row(eventRate="15,431.79")])
    assert check.status == "ok"
    assert "eventRate=15,431.79" in (check.detail or "")


@pytest.mark.parametrize(
    ("metrics", "expected_note"),
    [
        pytest.param(
            RestConnectionError("archiver timed out"), "ingest not measured", id="request-failed"
        ),
        pytest.param(
            [_metrics_row(instance="somebody-else")], "no unambiguous row", id="foreign-row"
        ),
        pytest.param(
            [_metrics_row(instance="appliance0"), _metrics_row(instance="appliance0")],
            "no unambiguous row",
            id="ambiguous-rows",
        ),
        pytest.param({"pvCount": "5"}, "no unambiguous row", id="not-a-list"),
    ],
)
def test_archiver_unmeasurable_ingest_stays_ok_but_leaves_a_trace(
    monkeypatch: pytest.MonkeyPatch, metrics: object, expected_note: str
) -> None:
    """Every way of NOT getting an answer leaves the plane ok, and says so in the detail.

    ok, because this REFINES a verdict that already stands: an older appliance without the route,
    or a cluster that timed out, must not fail a plane whose identity is proven. But never SILENTLY
    ok: without the note, "measured and ingesting" would be indistinguishable from "never measured",
    and on a real cluster the not-measured branch is the one that actually fires.

    Red-proof (mutation): return the plain identity detail here and the assertion on the note fails.
    """
    check = _archiver_check(monkeypatch, metrics)
    assert check.status == "ok"
    assert check.identified is True
    detail = check.detail or ""
    assert "appliance0" in detail  # identity is still the evidence
    assert expected_note in detail


def test_archiver_asks_the_metrics_route_and_refuses_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ADDRESS assertion, and the redirect posture, on their own.

    A faked seam serves every endpoint the same body, so the result slot proves nothing about which
    URL was requested (CLAUDE.md, evidence discipline: a route pointed at the wrong address left a
    whole suite green). ``allow_redirects=False`` matters for the same reason it does on the
    identity fetch: the RESPONDING host is the whole point, and a redirect would let another host
    answer for the appliance we just identified.

    Red-proof (mutation): point the ingest probe at getApplianceInfo and ``asked_for`` goes False;
    drop the allow_redirects kwarg and the kwargs assertion fails.
    """
    fake = _routed(
        monkeypatch,
        {_INFO_ROUTE: {"identity": "appliance0"}, _METRICS_ROUTE: [_metrics_row()]},
    )
    _identify_archiver("http://arch.example:17665", None, 5.0)
    assert fake.asked_for(_METRICS_ROUTE), "the ingest probe must request getApplianceMetrics"
    assert all(kw.get("allow_redirects") is False for kw in fake.kwargs)


def test_no_ingest_is_exit_zero_by_decision() -> None:
    """The exit CLASS is a product decision, and it is pinned here because nothing else pins it.

    Measured: with ``no_ingest`` moved into _FAILING_STATUSES (exit 1), the three guards one would
    expect to notice all stay GREEN, test_unknown_status_fails_closed included (it only reacts to
    the set growing). This assertion and the report-level one in the wiring lock are the whole
    mechanical record that a non-ingesting archiver must not fail a doctor run.
    """
    assert "no_ingest" in _NON_FAILING_STATUSES
    assert "no_ingest" not in _FAILING_STATUSES
    assert _DEGRADED_STATUSES <= _NON_FAILING_STATUSES  # degraded is a strict subset, never failing


def test_naming_identifies_via_its_swagger_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Naming Service DOES have an identity beacon, an earlier pass claimed it had none.

    That claim came from three probed paths plus an all-quantifier ("structurally unverifiable"),
    while the refuting evidence sat in the workspace the whole time. /rest/swagger.json is an
    anonymous static 200 and it discriminates (measured: Olog answers 401 there, CF 404).
    """
    _payload(monkeypatch, {"info": {"title": "Naming service API documentation"}})
    check = _identify_naming("http://naming.example", 5.0)
    assert (check.status, check.identified) == ("ok", True)


def test_naming_unfamiliar_title_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The title is documentation prose and may be reworded, so an unfamiliar one means "cannot
    confirm", honest doubt, exit 0 (since S14 that is the ONLY verdict any unconfirmed
    identity can earn; the harder wrong_service verdict was refuted by measurement)."""
    _payload(monkeypatch, {"info": {"title": "Some other API"}})
    assert _identify_naming("http://naming.example", 5.0).status == "unverified"


def test_retrieval_identifies_via_getversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieval serves /retrieval/bpl, probing /mgmt/bpl there 404s and proves nothing, which is
    exactly how an earlier pass concluded (wrongly) that retrieval had no identity endpoint."""
    probe = _identify_retrieval_plane
    _payload(monkeypatch, {"version": "Archiver Appliance Version 2.2.1"})
    check = probe("http://arch.example:17668", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)

    # The release number must NOT be pinned: an upgrade is not a misconfiguration.
    _payload(monkeypatch, {"version": "Archiver Appliance Version 9.9.9"})
    assert probe("http://arch.example:17668", None, 5.0).status == "ok"

    _payload(monkeypatch, {"version": "Some Other Product 1.0"})
    assert probe("http://arch.example:17668", None, 5.0).status == "unverified"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("Not Archiver Appliance Version 1.0", id="prefixed-name"),
        pytest.param("Archiver ApplianceX Version 1", id="suffixed-name"),
        pytest.param("Archiver Appliances Anonymous 1.0", id="longer-word"),
    ],
)
def test_retrieval_identity_is_anchored_at_a_word_boundary(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """S18(a): a version string whose NAME merely contains the product name must NOT identify.

    ``_identify`` matches the service name EXACTLY and its docstring says why, a substring would
    let a service calling itself "Not Olog Service" pass. Three functions later the retrieval probe
    shipped a containment check anyway (`_ARCHIVER_PRODUCT in version`), fail-open: the reasoning
    was written down and then ignored within the same file. The match is anchored at the START
    *and at a word boundary*, a bare ``startswith`` closed only the left side ("Archiver
    ApplianceX" still passed; the adversarial review of the first fix caught it). Only the release
    number after the full product name is variable (measured live on two real deployments:
    "Archiver Appliance Version 2.2.1").

    Red-proof: on the pre-fix code (84018ec) the first case reported ``ok`` (containment); the
    suffixed cases reported ``ok`` on the bare-startswith intermediate too.
    """
    _payload(monkeypatch, {"version": version})
    check = _identify_retrieval_plane("http://arch.example:17668", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)


# --- the alarm plane also checks its Elasticsearch backend (MA-2b(e)) ---
#
# The alarm logger's GET / beacon reports elastic.status ALONGSIDE its name. The transport probe is
# a blind HEAD (check_connectivity), so it reports "reachable" even when ES is dead, and the search
# history tools would then fail while the doctor said "✓ ok". _identify_alarm reads elastic.status
# from the SAME body the name check already parses (no second request). The healthy sentinel is
# EXACTLY "Connected"; a dead ES yields a string starting "Failed to connect to elastic " (measured
# from the Phoebus source SearchController.info(); GET / returns HTTP 200 either way, so the failure
# is body-only and a HEAD can never see it).


def test_alarm_elastic_down_is_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable + identified, but ES is down → a hard failure, not a silent ok.

    Red-proof: the pre-change alarm path used the shared name-only ``_identify``, which returns
    ``ok`` for this exact body, the blind-HEAD lie this change closes.
    """
    _payload(
        monkeypatch,
        {
            "name": "Alarm logging Service",
            "elastic": {"status": "Failed to connect to elastic boom"},
        },
    )
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert check.status == "backend_down"
    assert check.reachable is True
    assert check.identified is True  # identity WAS established; the failure is the backend
    assert check.status in _FAILING_STATUSES  # exit 1
    assert "Failed to connect to elastic boom" in (check.detail or "")
    # C1: the observation names the dead backend, the remedy says what to do about it. Both halves,
    # because "elastic is down" alone leaves the reader to guess whether the config is at fault.
    assert _REMEDY["backend_down"] in (check.detail or "")


def test_alarm_elastic_connected_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The healthy sentinel is EXACTLY "Connected" → ok/identified, unchanged from before."""
    _payload(monkeypatch, {"name": "Alarm logging Service", "elastic": {"status": "Connected"}})
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": "Alarm logging Service"}, id="no-elastic-key"),
        pytest.param({"name": "Alarm logging Service", "elastic": {}}, id="no-status-field"),
        pytest.param(
            {"name": "Alarm logging Service", "elastic": {"status": 42}}, id="non-string-status"
        ),
        pytest.param(
            {"name": "Alarm logging Service", "elastic": "not-a-dict"}, id="non-dict-elastic"
        ),
    ],
)
def test_alarm_missing_elastic_status_falls_back_to_ok(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """We never invent a failure we cannot prove: a missing / unreadable ``elastic.status`` is
    ``ok``, not ``backend_down`` (the withheld-≠-no discipline of every other identity path)."""
    _payload(monkeypatch, body)
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"name": "", "elastic": {"status": "Failed to connect to elastic x"}}, id="empty-name"
        ),
        pytest.param(
            {"name": "Olog Service", "elastic": {"status": "Failed to connect to elastic x"}},
            id="foreign-name",
        ),
    ],
)
def test_alarm_name_check_precedes_the_elastic_check(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """The identity gate runs FIRST: an unusable / foreign name is ``unverified`` (an honest "don't
    know"), never ``backend_down``, even when ``elastic.status`` says the backend is down. We do
    not report a backend failure for a service we cannot confirm IS the alarm logger."""
    _payload(monkeypatch, body)
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert check.status == "unverified"
    assert check.status in _NON_FAILING_STATUSES


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(requests.exceptions.HTTPError("401"), id="auth-wall"),
        pytest.param(requests.exceptions.ConnectionError("gone"), id="transport"),
        pytest.param(RuntimeError("refused to follow a redirect"), id="redirect-refused"),
    ],
)
def test_alarm_failed_probe_is_identity_probe_failed(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """_identify_alarm has its OWN fetch-failure branch (it is the alarm plane's PRODUCTION identity
    probe since MA-2b(e), not the shared _identify): a served non-2xx / transport error / refused
    redirect on the GET / beacon must be ``identity_probe_failed`` (exit 3), never a silent
    all-clear. Mirrors the shared-_identify S12 guard so the invariant holds on the new entry point.

    Red-proof (mutation): deleting the ``if isinstance(payload, Exception)`` branch in
    _identify_alarm lets the exception fall through to _classify_phoebus_name(exc) → name=None →
    unverified (exit 0), reintroducing the S12 silent-exit-0 regression on the alarm plane, this
    test then goes red.
    """
    _raises(monkeypatch, exc)
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert (check.status, check.identified) == ("identity_probe_failed", False)
    assert check.status in _INCONCLUSIVE_STATUSES
    assert check.status not in _NON_FAILING_STATUSES


def test_alarm_unreadable_2xx_body_stays_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx alarm beacon whose body is not JSON is honest ``unverified`` (answered, not nameable),
    NOT ``identity_probe_failed``, the same ValueError carve-out as the shared _identify path, now
    exercised through _identify_alarm's OWN fetch-failure branch (the alarm production entry
    point)."""
    cause = json.JSONDecodeError("Expecting value", "<html>login</html>", 0)
    exc = requests.exceptions.RequestException(
        "Request failed (http://alarm.example): bad JSON body"
    )
    exc.__cause__ = cause  # what rest_get_json chains on a 2xx-but-unparseable body
    _raises(monkeypatch, exc)
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)
    assert check.status in _NON_FAILING_STATUSES


async def test_alarm_backend_down_flows_through_run_doctor_to_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end WIRING lock: a reachable alarm logger with a dead Elasticsearch must reach
    run_doctor as ``backend_down`` → ``report.ok`` False (exit 1), THROUGH the real _identify_alarm.

    This is the one test that pins _check_alarm to _identify_alarm rather than the old name-only
    _identify. The four unit tests above call _identify_alarm directly, and the autouse fixture
    stubs BOTH _identify and _identify_alarm to the same identified=True value, so reverting the
    wiring line (_check_alarm._id → _identify("alarm", ...)) is otherwise invisible to every test.
    Here the REAL _identify_alarm and a real rest_get_json body are restored over the autouse stubs;
    with the wiring reverted, _check_alarm would call the STILL-STUBBED _identify → status 'ok' →
    this fails.

    Red-proof (mutation): point _check_alarm._id back at _identify and this test goes red (status
    'ok', report.ok True), the exact pre-MA-2b(e) blind-HEAD behaviour the change removes.
    """
    _set_config(monkeypatch, alarm_url="http://alarm.example")
    monkeypatch.setattr("epics_mcp.services.doctor.AlarmClient", _OkClient)
    # Restore the REAL probe + a GET / body over the autouse stubs so the real chain runs.
    monkeypatch.setattr("epics_mcp.services.doctor._identify_alarm", _identify_alarm)
    monkeypatch.setattr(
        "epics_mcp.services.doctor.rest_get_json",
        lambda *_a, **_k: {
            "name": "Alarm logging Service",
            "elastic": {"status": "Failed to connect to elastic boom"},
        },
    )
    report = await run_doctor()
    alarm = _plane(report, "alarm")
    assert (alarm.status, alarm.identified) == ("backend_down", True)
    assert report.ok is False  # backend_down ∈ _FAILING_STATUSES → exit 1


def _wire_starved_archiver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the archiver plane and route its two GETs at an appliance that archives nothing.

    Shared by the report-level lock and its CLI twin below (they are split only because
    ``cli_doctor.main`` calls ``asyncio.run``, which cannot run inside an async test).
    """
    _set_config(monkeypatch, archiver_url="http://arch.example:17665")
    monkeypatch.setattr("epics_mcp.services.doctor.ArchiverClient", _OkClient)
    # Restore the REAL probe over the autouse stub, then route the two GETs it makes.
    monkeypatch.setattr("epics_mcp.services.doctor._identify_archiver", _identify_archiver)
    _routed(
        monkeypatch,
        {
            _INFO_ROUTE: {"identity": "appliance0"},
            _METRICS_ROUTE: [_metrics_row(connectedPVCount="0", disconnectedPVCount="5")],
        },
    )


def test_no_ingest_reaches_the_cli_without_the_confirmation_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI half of the wiring lock: a starved archiver prints its own glyph, drops the
    strongest confirmation sentence, and still exits 0.

    Red-proof (mutation): delete the degraded branch in _render and "AS ITSELF" is printed directly
    under the "~ archiver" line. Move no_ingest into _FAILING_STATUSES and the exit code becomes 1.
    """
    _wire_starved_archiver(monkeypatch)
    assert cli_doctor.main([]) == 0  # the decision: a starved archiver never fails a doctor run
    out = capsys.readouterr().out
    assert "~ archiver" in out
    assert "AS ITSELF" not in out  # the strongest confirmation must not sit under a "~" line
    assert "NOT doing their job" in out


async def test_no_ingest_flows_through_run_doctor_to_the_report_and_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end WIRING lock for QA-35, the archiver twin of the alarm one above.

    It pins the whole chain at once because each link is invisible on its own: the autouse fixture
    stubs _identify_archiver to a plain identified=True, so reverting the ingest call inside
    _identify_archiver would be unobservable to every unit test above. Restoring the REAL probe
    plus a routed seam over that stub is the only way to see it.

    Four things are pinned here, and NOTHING else pins them together:
      1. the plane arrives in the report as no_ingest with identity still proven;
      2. it is listed in degraded_planes, the ONLY machine-readable trace it leaves;
      3. ok / verification_complete stay True and the process exit stays 0, the product decision;
      4. it is ALSO in identified_planes, which is why a script reading that list alone would
         count a non-archiving appliance as positively confirmed.

    Red-proof (mutation): drop the _archiver_ingest_verdict call from _identify_archiver and the
    plane arrives as plain ok, failing 1 and 2. Move no_ingest into _FAILING_STATUSES and 3 fails.
    """
    _wire_starved_archiver(monkeypatch)

    report = await run_doctor()
    archiver = _plane(report, "archiver")
    assert (archiver.status, archiver.identified) == ("no_ingest", True)
    assert report.degraded_planes == ["archiver"]
    assert report.ok is True and report.verification_complete is True
    assert report.unverified_planes == []
    assert "archiver" in report.identified_planes  # identified is not healthy


def test_unknown_status_fails_closed() -> None:
    """The allowlist is the point: a new or mistyped status must FAIL, not slip through as exit 0.

    With the previous failure DENYLIST, a typo like "wrong-service" was simply absent from it and
    therefore counted as healthy, fail-open, in the one tool whose job is to catch bad config.

    ⚠️ Note what this pin does and does NOT do. It goes red when the set GROWS, i.e. on a correct
    build that classifies a new status here, so it is a deliberate hand-updated record of a product
    decision, NOT a guard against forgetting to classify. Forgetting is caught by
    ``test_status_partition_is_total_and_disjoint``; putting a status in the WRONG set is caught by
    neither, which is why the exit class of ``no_ingest`` is pinned separately (see
    ``test_no_ingest_is_exit_zero_by_decision``).
    """
    assert "wrong-service" not in _NON_FAILING_STATUSES  # the typo'd twin of a former status
    assert {"ok", "disabled", "info", "unverified", "no_ingest"} == _NON_FAILING_STATUSES


def test_status_partition_is_total_and_disjoint() -> None:
    """S12: the three status sets tile ``PlaneStatus`` exactly. This is what keeps ``ok``'s
    allowlist union fail-CLOSED: a new status added to the Literal but forgotten from all three sets
    is (a) not clean, not inconclusive → ``ok`` counts it a failure, and (b) caught here as red.

    Red-proof: a mutant that adds a Literal value without classifying it (or double-lists one)
    breaks totality/disjointness here.
    """
    all_statuses = set(get_args(PlaneStatus))
    assert _NON_FAILING_STATUSES.isdisjoint(_INCONCLUSIVE_STATUSES)
    assert _NON_FAILING_STATUSES.isdisjoint(_FAILING_STATUSES)
    assert _INCONCLUSIVE_STATUSES.isdisjoint(_FAILING_STATUSES)
    assert all_statuses == _NON_FAILING_STATUSES | _INCONCLUSIVE_STATUSES | _FAILING_STATUSES


@pytest.mark.parametrize(
    ("plane", "url_field", "client_name"),
    [
        ("channelfinder", "channelfinder_url", "ChannelFinderClient"),
        ("olog", "olog_url", "OlogClient"),
        ("alarm", "alarm_url", "AlarmClient"),
        ("archiver", "archiver_url", "ArchiverClient"),
        ("naming", "naming_url", "NamingServiceClient"),
    ],
)
async def test_every_rest_plane_is_actually_identity_probed(
    monkeypatch: pytest.MonkeyPatch, plane: str, url_field: str, client_name: str
) -> None:
    """The WIRING guard, the identity logic being correct is worthless if nobody calls it.

    Measured with a mutant: deleting the identity argument from the plane gatherers (i.e. exactly
    the pre-S4 state) left the whole gate chain green, 47/48 tests, ruff, mypy, while the doctor
    went back to reporting "✓ channelfinder ok" for a dead container, now under the even bolder
    "every configured plane answered AS ITSELF". Only this assertion notices.
    """
    _set_config(monkeypatch, **{url_field: "http://service.example/x"})
    monkeypatch.setattr(f"epics_mcp.services.doctor.{client_name}", _OkClient)
    report = await run_doctor()
    checked = _plane(report, plane)
    assert checked.identified is True, (
        f"{plane}: reachable but never identity-probed, a transport probe alone is what let a "
        "dead container report ok"
    )


async def test_retrieval_falls_back_to_the_archiver_url_like_the_client_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-JVM appliance leaves EPICS_MCP_ARCHIVER_RETRIEVAL_URL empty and serves retrieval on
    the archiver port; ArchiverClient resolves `retrieval_url or base_url` (archiver_client.py) and
    get_pv_history queries it. Reporting that plane as "disabled" would be the same false all-clear
    this check exists to remove, only wearing a more reassuring word.
    """
    _set_config(monkeypatch, archiver_url="http://arch.example:17665")  # retrieval URL empty
    monkeypatch.setattr("epics_mcp.services.doctor.ArchiverClient", _OkClient)
    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", lambda *a, **k: {})
    report = await run_doctor()
    retrieval = _plane(report, "archiver_retrieval")
    assert retrieval.status != "disabled", "retrieval is live via fallback, not reported as off"
    assert retrieval.configured is True


@pytest.mark.parametrize(
    ("retrieval_url", "expected_var"),
    [
        pytest.param("", "EPICS_MCP_ARCHIVER_URL", id="single-jvm-fallback"),
        pytest.param(
            "http://arch.example:17668",
            "EPICS_MCP_ARCHIVER_RETRIEVAL_URL",
            id="split-port-deployment",
        ),
    ],
)
async def test_unreachable_retrieval_names_the_variable_the_url_came_from(
    monkeypatch: pytest.MonkeyPatch, retrieval_url: str, expected_var: str
) -> None:
    """C1: the retrieval plane reads its URL from EITHER variable, so the remedy has to name the one
    that actually carried the failing URL.

    This is the plane where naming the wrong one is most likely to be believed: an operator who
    followed the split-port instructions in docs/deployment.md set
    EPICS_MCP_ARCHIVER_RETRIEVAL_URL, and being told to check EPICS_MCP_ARCHIVER_URL sends them to a
    URL that did not fail. Both rows are here because the fallback case must KEEP naming the mgmt
    variable, which is the one that carried the URL there; a fix in either direction alone is wrong.

    Faked at the TRANSPORT seam (``rest_get_json``, which this plane's own probe calls) rather than
    by doubling a client class, and calling the plane check directly rather than through
    ``run_doctor``. Both for the same reason: this asserts what ``_check_retrieval_plane`` passes as
    ``url_var``, so every other plane is noise, and a class-level double would additionally be the
    seam this repository's own audit counts and pins.

    Red-proof: pinning ``url_var`` to either variable unconditionally fails one of these two rows
    (measured on the pre-fix code, which named the mgmt variable always: the split-port row failed).
    """
    cfg = _set_config(
        monkeypatch, archiver_url="http://arch.example:17665", archiver_retrieval_url=retrieval_url
    )
    monkeypatch.setattr(
        "epics_mcp.services.doctor.rest_get_json",
        Mock(side_effect=RestConnectionError("refused")),
    )

    check = await _check_retrieval_plane(cfg, 5.0)

    assert check.status == "unreachable"
    # The OBSERVATION half, not mere presence of the name anywhere in the detail. Presence is what
    # a later change can satisfy accidentally: a post-build audit measured that pinning ``url_var``
    # to the wrong variable kept this green once another clause in the same detail happened to
    # mention the right one. What this test is about is which variable carried the failing URL, and
    # the sentence that says so is the one asserted.
    assert f"could not reach the service at {expected_var}" in (check.detail or ""), (
        f"the observation must name {expected_var}, the variable that carried the failing URL: "
        f"{check.detail!r}"
    )


def _served_404(*_args: object, **_kwargs: object) -> object:
    """A rest_get_json that fails the way a host WITH a webapp but WITHOUT this one fails."""

    class _NotFound(Exception):
        response = SimpleNamespace(status_code=404)

    try:
        raise _NotFound("404")
    except _NotFound as cause:
        raise RestResponseError("wrong webapp") from cause


async def _probe_retrieval(
    monkeypatch: pytest.MonkeyPatch, *, retrieval_url: str, payload: object
) -> PlaneCheck:
    """Run the retrieval plane against a faked TRANSPORT, with the real identity probe restored.

    The restore matters: the module's autouse ``_identity_never_touches_the_network`` replaces
    ``_identify_retrieval_plane`` for every test here, and under it an identity row measures a plain
    ``ok`` while READING as though it exercised the identity path. The real function comes from the
    module-level import, which holds the object captured before any stubbing.
    """
    cfg = _set_config(
        monkeypatch, archiver_url="http://arch.example:17665", archiver_retrieval_url=retrieval_url
    )
    if callable(payload):
        probe: object = payload
    elif isinstance(payload, Exception):
        probe = Mock(side_effect=payload)
    else:
        probe = Mock(return_value=payload)
    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", probe)
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify_retrieval_plane", _identify_retrieval_plane
    )
    return await _check_retrieval_plane(cfg, 5.0)


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        pytest.param(_served_404, "api_error", id="wrong-webapp"),
        pytest.param({"version": "Some Other Service 1.0"}, "unverified", id="not-retrieval"),
    ],
)
async def test_the_fallback_finding_names_the_variable_that_would_help(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected_status: str
) -> None:
    """BG-DFIX(b): with EPICS_MCP_ARCHIVER_RETRIEVAL_URL empty this plane probes the MGMT URL, and
    a finding then sent the operator to edit EPICS_MCP_ARCHIVER_URL, the variable that got a ✓ on
    the line above (the archiver plane answered; only its retrieval webapp did not).

    Following that advice breaks the half that works and leaves the broken half broken. The one
    setting that helps, a retrieval URL of its own, was not named at all. The state is real rather
    than constructed for a test: it was reproduced against a local HTTP server that serves the MGMT
    routes and 404s every /retrieval/ route, which is a split deployment with the retrieval variable
    left unset.

    Both rows are states in which the HOST ANSWERED, which is the condition for the note at all: a
    served 404 and a 2xx from something that is not the retrieval webapp. They also fail
    differently, and both ways matter: ``api_error`` carries a remedy promising "the variable to
    edit is named at the start of this finding", while ``unverified`` carries NO remedy by design,
    so there the observation is the only thing that can name a variable at all.

    The ORDER is the assertion, not the presence. EPICS_MCP_ARCHIVER_URL keeps being named, because
    it is the URL that was really probed and dropping it would trade one dishonest sentence for
    another; what changes is which variable the reader meets first.

    Red-proof on the pre-fix code: the first variable named is EPICS_MCP_ARCHIVER_URL in both rows,
    and EPICS_MCP_ARCHIVER_RETRIEVAL_URL appears nowhere.
    """
    check = await _probe_retrieval(monkeypatch, retrieval_url="", payload=payload)
    detail = check.detail or ""
    named = re.findall(r"EPICS_MCP_[A-Z_]+", detail)

    assert check.status == expected_status, f"expected {expected_status}: {detail!r}"
    assert named[:1] == ["EPICS_MCP_ARCHIVER_RETRIEVAL_URL"], (
        "the fallback finding must lead with the variable that would fix it, not with the one that "
        f"just passed its own probe: {detail!r}"
    )
    assert "EPICS_MCP_ARCHIVER_URL" in named, (
        f"the URL that was actually probed must still be named: {detail!r}"
    )


@pytest.mark.parametrize(
    ("retrieval_url", "expected_first", "reason"),
    [
        pytest.param(
            "",
            "EPICS_MCP_ARCHIVER_URL",
            "the host never answered, so a retrieval URL of its own cannot help",
            id="host-never-answered",
        ),
        pytest.param(
            "http://arch.example:17668",
            "EPICS_MCP_ARCHIVER_RETRIEVAL_URL",
            "there was no fallback: this URL came from the retrieval variable",
            id="split-port-untouched",
        ),
    ],
)
async def test_the_fallback_note_stays_out_where_it_would_mislead(
    monkeypatch: pytest.MonkeyPatch, retrieval_url: str, expected_first: str, reason: str
) -> None:
    """The other half of BG-DFIX(b), and it is the half the first version got WRONG.

    The note is only true, and only useful, when the HOST ANSWERED: then "which webapp does this
    URL serve" is a live question and giving retrieval a URL of its own is the fix. When the host
    did not answer at all, the same note leads with an EMPTY variable while the thing to repair is
    the address, or the service, behind EPICS_MCP_ARCHIVER_URL. Measured on the first version of
    this change, against a closed port: BOTH archiver planes failed, nothing had earned a ✓, and
    the retrieval finding still opened with EPICS_MCP_ARCHIVER_RETRIEVAL_URL. That is the same
    misdirection the ticket is about, moved into a different configuration rather than removed.

    ``reachable`` is the discriminator rather than a list of statuses, because it is the question
    itself: measured, it is False for exactly the transport and TLS failures (``unreachable``,
    ``ca_error``) and True for every state in which the host produced a response.

    The second row guards the other exit: with a retrieval URL of its own there was no fallback, so
    a note claiming one would be false. Deleting the ``cfg.archiver_retrieval_url or`` half of the
    condition leaves this row red and the rest of the suite green.

    Red-proof: dropping the reachable test reddens the first row; dropping the retrieval-URL test
    reddens the second.
    """
    check = await _probe_retrieval(
        monkeypatch, retrieval_url=retrieval_url, payload=RestConnectionError("refused")
    )
    detail = check.detail or ""
    named = re.findall(r"EPICS_MCP_[A-Z_]+", detail)

    assert check.status == "unreachable", f"expected a transport failure: {detail!r}"
    assert named[:1] == [expected_first], f"{reason}, so the finding must lead with it: {detail!r}"
    assert "fell back" not in detail, f"a fallback is claimed where {reason}: {detail!r}"


#: The measured wrong sentence: the api_error remedy told EVERY plane that the right webapp of an
#: Archiver Appliance is the mgmt one. Pinned as the exact string it used to be, because that is a
#: claim about ONE sentence that was wrong rather than about how the remedy should be worded, which
#: section 13 of docs/known-limits.md deliberately leaves free.
_WEBAPP_DIRECTIVE = "mgmt port and not retrieval"


async def test_the_api_error_remedy_does_not_send_the_retrieval_plane_to_mgmt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BG-DFIX, found while measuring (b): ``_REMEDY`` is keyed by STATUS and shared by every plane,
    and its ``api_error`` entry named one webapp as the correct one.

    On the plane whose whole reason to exist is the OTHER webapp, that is the wrong instruction: a
    404 from the retrieval endpoint is the signature of a split deployment, and the operator was
    told to point at mgmt, which is where the probe already was. Measured with a chained HTTP 404,
    both with the retrieval URL set and on the fallback, where the finding since (b) opens by saying
    it fell back to mgmt and then closed by advising mgmt.

    The remedy stays status-keyed and static, as its own docstring requires; what changes is that it
    describes the QUESTION (which webapp does this plane read, and from which variable) instead of
    answering it for one plane.

    Red-proof on the pre-fix code: the phrase is present in the rendered finding.
    """
    cfg = _set_config(
        monkeypatch,
        archiver_url="http://arch.example:17665",
        archiver_retrieval_url="http://arch.example:17668",
    )

    class _NotFound(Exception):
        response = SimpleNamespace(status_code=404)

    def _served_404(*_args: object, **_kwargs: object) -> object:
        try:
            raise _NotFound("404")
        except _NotFound as cause:
            raise RestResponseError("wrong webapp") from cause

    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", _served_404)

    check = await _check_retrieval_plane(cfg, 5.0)
    detail = check.detail or ""

    assert check.status == "api_error", f"expected a served non-2xx: {detail!r}"
    assert _WEBAPP_DIRECTIVE not in detail, (
        "the shared api_error remedy tells the RETRIEVAL plane to use the mgmt webapp, which is "
        f"the one it just probed: {detail!r}"
    )


async def test_retrieval_url_without_archiver_url_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S18(b): EPICS_MCP_ARCHIVER_RETRIEVAL_URL set while EPICS_MCP_ARCHIVER_URL is empty.

    Every archiver tool gates on EPICS_MCP_ARCHIVER_URL (tools/archiver.py, checkers.py), so that
    retrieval URL is never used by anything, yet the fallback fix reported the STRONGEST all-clear
    the tool knows for it (measured against a live retrieval endpoint: ``ok=True,
    verification_complete=True`` while every archiver tool was disabled). A fix against false-green
    that produced false-green. The pair is dead config and must FAIL, loudly, without probing:
    an ``ok`` next to a config error would only muddy what the operator has to change.

    Red-proof: on the pre-fix code this test FAILS, the plane is probed instead of refused
    (live it reported ``ok``; under this test's boom-mock the probe errors, either way the
    status is not ``config_error``).
    """
    _set_config(monkeypatch, archiver_retrieval_url="http://arch.example:17668")
    boom = Mock(side_effect=AssertionError("dead config must not be probed"))
    monkeypatch.setattr("epics_mcp.services.doctor.rest_get_json", boom)
    report = await run_doctor()
    retrieval = _plane(report, "archiver_retrieval")
    assert retrieval.status == "config_error"
    assert retrieval.configured is True
    assert "EPICS_MCP_ARCHIVER_URL" in (retrieval.detail or "")
    assert retrieval.status not in _NON_FAILING_STATUSES  # it must drive exit 1
    # C1: the one status reached without any network call, so its remedy also has to say that no
    # probe result is coming; the inline PlaneCheck here is the only site that produces it.
    assert _REMEDY["config_error"] in (retrieval.detail or "")
    assert report.ok is False
    # The archiver plane itself stays honestly disabled, the ERROR is the inconsistent pair.
    assert _plane(report, "archiver").status == "disabled"


async def test_retrieval_plane_is_actually_identity_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring guard for the SIXTH plane, the matrix above covers five and left retrieval out.

    Not a matrix row, because retrieval has no client class (its transport probe calls
    rest_get_json directly) and a lone retrieval URL is a ``config_error`` since S18(b), so the
    wired path to guard is the fallback one (archiver URL set). Mutant-proof: removing the
    identity argument from the ``_run_probe`` call in ``_check_retrieval_plane`` leaves every
    other test green; only this assertion notices (``identified`` stays None).
    """
    _set_config(monkeypatch, archiver_url="http://arch.example:17665")
    monkeypatch.setattr("epics_mcp.services.doctor.ArchiverClient", _OkClient)
    report = await run_doctor()
    retrieval = _plane(report, "archiver_retrieval")
    assert retrieval.identified is True, (
        "archiver_retrieval: reachable but never identity-probed, the same gap the matrix "
        "guards against for the other five planes"
    )


def test_credentials_in_a_url_are_never_echoed() -> None:
    """doctor output is what gets pasted into a ticket; requests' error text embeds the full URL."""
    leaky = "Failed to connect to http://admin:hunter2@olog.example/Olog: timed out"
    assert "hunter2" not in _safe(leaky)
    assert "***@olog.example" in _safe(leaky)
    # A URL without credentials must survive untouched.
    plain = "Failed to connect to http://olog.example/Olog: timed out"
    assert _safe(plain) == plain


async def test_unverified_plane_does_not_fail_but_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest middle: reachable, identity unproven → exit 0, but NOT "healthy".

    That a healthy service answers its info endpoint anonymously is measured at ONE site (n=1), so
    turning "cannot prove it" into a hard failure would be the very overclaim we keep finding.
    """
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify",
        lambda plane, *_a, **_k: PlaneCheck(
            plane=plane,
            configured=True,
            reachable=True,
            ca_ok=True,
            status="unverified",
            identified=False,
            detail="transport reachable, identity unverified",
        ),
    )
    report = await run_doctor()
    cf = _plane(report, "channelfinder")
    assert (cf.status, cf.identified, cf.reachable) == ("unverified", False, True)
    assert report.ok is True  # honest, not a failure
    assert report.verification_complete is False  # ...but NOT confirmed
    assert report.unverified_planes == ["channelfinder"]
    assert report.inconclusive_identity_planes == []  # unverified is NOT a failed probe


async def test_inconclusive_plane_keeps_ok_and_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S12: a FAILED identity probe (identity_probe_failed) is reachable-but-suspect. ``ok`` stays
    True (it is NOT a hard failure), so the exit code cannot be derived from ``ok`` alone, it lands
    in ``inconclusive_identity_planes`` (the field a machine reader must check ALONGSIDE
    ``unverified_planes``), and ``verification_complete`` is False.

    Red-proof (the FLAW-B trap): a naive ``ok = all(status in _NON_FAILING_STATUSES)`` (leaving the
    old line) flips ``ok`` to False here, which would collapse exit 3 into exit 1. Pins the union.
    """
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify",
        lambda plane, *_a, **_k: PlaneCheck(
            plane=plane,
            configured=True,
            reachable=True,
            ca_ok=True,
            status="identity_probe_failed",
            identified=False,
            detail="transport reachable, but the identity probe FAILED: 401",
        ),
    )
    report = await run_doctor()
    cf = _plane(report, "channelfinder")
    assert cf.status == "identity_probe_failed"
    assert report.ok is True  # NOT a hard failure, pins the ok-union
    assert report.verification_complete is False
    assert report.inconclusive_identity_planes == ["channelfinder"]
    assert report.unverified_planes == []


async def test_ca_error_plane_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, channelfinder_url="http://cf")
    monkeypatch.setattr(
        "epics_mcp.services.doctor.ChannelFinderClient",
        _cause_client(requests.exceptions.SSLError("self-signed")),
    )
    report = await run_doctor()
    cf = _plane(report, "channelfinder")
    assert cf.status == "ca_error"
    assert cf.ca_ok is False
    assert report.ok is False


async def test_archiver_api_error_is_reachable_not_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan-QA #1 end-to-end: a served 404 (wrong webapp) → api_error/reachable, ok=False."""
    _set_config(monkeypatch, archiver_url="http://arch:17665")
    http_err = requests.exceptions.HTTPError("404")
    http_err.response = Mock(status_code=404)
    monkeypatch.setattr("epics_mcp.services.doctor.ArchiverClient", _cause_client(http_err))
    report = await run_doctor()
    arch = _plane(report, "archiver")
    assert arch.status == "api_error"
    assert arch.reachable is True  # NOT falsely unreachable
    assert report.ok is False


async def test_unreachable_plane_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, alarm_url="http://alarm:8081")
    monkeypatch.setattr(
        "epics_mcp.services.doctor.AlarmClient",
        _cause_client(requests.exceptions.ConnectionError("refused")),
    )
    report = await run_doctor()
    alarm = _plane(report, "alarm")
    assert alarm.status == "unreachable"
    assert report.ok is False


# --- privacy report (single source with the CF client) ---


async def test_privacy_report_reflects_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch)
    report = await run_doctor()
    assert report.privacy.cf_safe_owner_accounts == ["recceiver"]
    assert "iocName" in report.privacy.cf_safe_property_names


async def test_privacy_report_reflects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, channelfinder_safe_owner_accounts="svc_a,svc_b")
    report = await run_doctor()
    assert report.privacy.cf_safe_owner_accounts == ["svc_a", "svc_b"]


def test_privacy_report_carries_no_olog_field() -> None:
    """The doctor makes no Olog READ-redaction claim: reads are whole (decision PI, 2026-08-01),
    so a field that could say "withheld" would only be a place for a future lie.

    Scoped to the redaction on purpose. Since BG-DSAFE the report DOES carry an Olog posture, the
    write gate's, in a model of its own; what must stay absent is a privacy field suggesting a read
    is filtered when it is not.
    """
    report = _privacy_report(EpicsConfig(olog_url="http://localhost:8080/Olog"))
    assert "olog" not in {name.split("_")[0] for name in type(report).model_fields}


# --- live plane (Plan-QA #4: no default egress) ---


async def test_live_plane_info_only_makes_no_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --probe-pv the live plane is INFO-only and pv_get is NEVER called."""
    _set_config(monkeypatch)
    pv_get = Mock(side_effect=AssertionError("pv_get must not be called without --probe-pv"))
    monkeypatch.setattr("epics_mcp.services.doctor.pv_get", pv_get)
    report = await run_doctor()  # no probe_pv
    live = _plane(report, "live")
    assert live.status == "info"
    assert live.reachable is None
    assert report.ok is True
    # Direct teeth for the no-egress guarantee, independent of _probe_live_pv's broad except.
    pv_get.assert_not_called()


async def test_live_plane_probe_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch)

    async def _ok(pv_name: str, timeout: float) -> dict[str, object]:
        return {"value": 1, "alarm": {"severity_text": "NO_ALARM"}}

    monkeypatch.setattr("epics_mcp.services.doctor.pv_get", _ok)
    report = await run_doctor(probe_pv="SIM:PS-01:Cur-RB")
    live = _plane(report, "live")
    assert live.status == "ok"
    assert live.reachable is True
    assert report.ok is True


async def test_live_plane_probe_disconnected_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch)

    async def _down(pv_name: str, timeout: float) -> dict[str, object]:
        raise EpicsError("timeout", error_code="PV_TIMEOUT")

    monkeypatch.setattr("epics_mcp.services.doctor.pv_get", _down)
    report = await run_doctor(probe_pv="SIM:PS-01:Cur-RB")
    live = _plane(report, "live")
    assert live.status == "disconnected"
    assert live.reachable is False
    assert report.ok is False
    # C1: the live plane has no URL variable, so its remedy points at the three things that CAN be
    # wrong (name, IOC, search path) instead of at a setting.
    assert _REMEDY["disconnected"] in (live.detail or "")


async def test_live_plane_probe_generic_exception_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-EpicsError from the probe (internal failure) is still caught → disconnected, keeping
    doctor total; the exception's type name flows into the detail."""
    _set_config(monkeypatch)

    async def _boom(pv_name: str, timeout: float) -> dict[str, object]:
        raise ValueError("boom")

    monkeypatch.setattr("epics_mcp.services.doctor.pv_get", _boom)
    report = await run_doctor(probe_pv="SIM:PS-01:Cur-RB")
    live = _plane(report, "live")
    assert live.status == "disconnected"
    assert live.reachable is False
    assert report.ok is False
    assert live.detail is not None and "ValueError" in live.detail


# --- live plane posture (BG14): the isolation claim must be TRUE, not a default ---
# conftest's _isolate_epics_search_env strips every search var first; each test then sets
# exactly the environment it asserts about.


async def test_live_posture_sees_name_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """BG14 red proof 1: `EPICS_PVA_NAME_SERVERS` alone is a search path: TCP unicast to the
    named servers, NOT subnet-bound (pvxs client.cpp startNS()). Pre-fix the posture ignored
    the var entirely and claimed `localhost-isolated` while the client dialed out."""
    _set_config(monkeypatch)
    monkeypatch.setenv("EPICS_PVA_NAME_SERVERS", "192.0.2.55:5075")
    report = await run_doctor()
    live = _plane(report, "live")
    assert live.detail is not None
    assert "localhost-isolated" not in live.detail
    # The active search path is NAMED, not just vaguely "not isolated".
    assert "EPICS_PVA_NAME_SERVERS" in live.detail


async def test_live_posture_honours_auto_addr_list_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BG14 red proof 2 (the stronger one): with NOTHING set, pvxs still broadcasts PV
    searches into the local subnets, autoAddrList defaults to true (pvxs pvxs/client.h).
    The unconditional `localhost-isolated (no address list set)` claim was wrong even for
    the null environment; this test kills the unconditional formulation, not just one
    forgotten variable."""
    _set_config(monkeypatch)
    report = await run_doctor()
    live = _plane(report, "live")
    assert live.detail is not None
    assert "localhost-isolated" not in live.detail
    assert "auto-addr" in live.detail  # the default-on broadcast path is named


async def test_live_posture_isolated_only_when_auto_addr_explicitly_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the isolation claim survives in exactly the one state where it is
    true, every search list unset AND the auto-addr search explicitly disabled."""
    _set_config(monkeypatch)
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_CA_AUTO_ADDR_LIST", "NO")
    report = await run_doctor()
    live = _plane(report, "live")
    assert live.detail is not None
    assert "localhost-isolated" in live.detail


async def test_live_posture_names_every_set_search_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All set search vars appear in the posture, none is masked by another (pre-fix the
    `or` fallback reported ONLY the PVA list and swallowed the rest)."""
    _set_config(monkeypatch)
    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "192.0.2.255")
    monkeypatch.setenv("EPICS_CA_ADDR_LIST", "192.0.2.254")
    monkeypatch.setenv("EPICS_PVA_NAME_SERVERS", "192.0.2.55:5075")
    report = await run_doctor()
    live = _plane(report, "live")
    assert live.detail is not None
    for var in ("EPICS_PVA_ADDR_LIST", "EPICS_CA_ADDR_LIST", "EPICS_PVA_NAME_SERVERS"):
        assert var in live.detail


@pytest.mark.parametrize("value", ["false", "FALSE ", "0 ", " no", "off"])
async def test_live_posture_rejects_off_spellings_pvxs_does_not_parse(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """BG14-QA: pvxs' parse_bool accepts ONLY case-insensitive "NO" or exactly "0":
    untrimmed (PickOne passes the raw getenv value); anything else is a parse error that
    keeps the DEFAULT, and the default is broadcast (pvxs src/config.cpp, pvxs/client.h).
    Claiming isolation for a spelling the real parser rejects would be exactly the false
    claim BG14 removed, "false" is the likeliest real-world case, since this repo's own
    env convention is `EPICS_MCP_*=false`."""
    _set_config(monkeypatch)
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", value)
    report = await run_doctor()
    live = _plane(report, "live")
    assert live.detail is not None
    assert "localhost-isolated" not in live.detail


@pytest.mark.parametrize(
    ("value", "isolated"),
    [
        ("no", True),
        ("NO", True),
        ("nope", True),  # strstr: any substring "no" disables, pinned so the semantics stay honest
        ("No", False),  # mixed case matches neither strstr("no") nor strstr("NO")
        ("false", False),
        ("0", False),
    ],
)
async def test_live_posture_ca_off_is_substring_case_sensitive(
    monkeypatch: pytest.MonkeyPatch, value: str, isolated: bool
) -> None:
    """libca disables the auto search only when the value CONTAINS "no" or "NO" as a
    case-sensitive substring (epics-base modules/ca/src/client/iocinf.cpp), "false",
    "0" and even "No" keep broadcasting on a ca provider."""
    _set_config(monkeypatch, provider="ca")
    monkeypatch.setenv("EPICS_CA_AUTO_ADDR_LIST", value)
    report = await run_doctor()
    live = _plane(report, "live")
    assert live.detail is not None
    assert ("localhost-isolated" in live.detail) is isolated


# --- cli_doctor.main: exit codes + render (the deliberate 0/1/2 convention) ---


def test_cli_all_disabled_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_config(monkeypatch)
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "Overall: OK" in out
    assert "disabled" in out


@pytest.mark.parametrize("bad", ["-1", "0"])
def test_cli_nonpositive_timeout_is_usage_error(bad: str) -> None:
    """F22: --timeout <= 0 is a usage error (exit 2), rejected at parse time before any probe:
    a <=0 timeout would otherwise flow into run_doctor and make a healthy plane look unreachable."""
    with pytest.raises(SystemExit) as exc:
        cli_doctor.main(["--timeout", bad])
    assert exc.value.code == 2


def test_cli_failing_plane_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_config(monkeypatch, alarm_url="http://alarm:8081")
    monkeypatch.setattr(
        "epics_mcp.services.doctor.AlarmClient",
        _cause_client(requests.exceptions.ConnectionError("refused")),
    )
    code = cli_doctor.main([])
    assert code == 1
    assert "PROBLEM" in capsys.readouterr().out


def test_cli_json_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_config(monkeypatch)
    code = cli_doctor.main(["--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "planes" in payload
    assert "privacy" in payload
    assert payload["ok"] is True


def test_cli_render_glyphs_and_privacy_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human render shows per-status glyphs and the privacy block incl. the empty-owner line."""
    _set_config(monkeypatch, alarm_url="http://alarm:8081", channelfinder_safe_owner_accounts="")
    monkeypatch.setattr(
        "epics_mcp.services.doctor.AlarmClient",
        _cause_client(requests.exceptions.ConnectionError("refused")),
    )
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "✗ alarm" in out  # failing-plane glyph
    assert "· archiver" in out  # disabled-plane glyph
    assert "i live" in out  # info live-plane glyph
    assert "owner allowlist:" in out
    assert "property allowlist:" in out
    assert "(empty, all owners redacted)" in out  # the empty-owner fallback line
    # No Olog posture line at all: reads are whole (decision PI, 2026-08-01).
    assert "Olog free-text" not in out


def test_cli_config_error_renders_failing_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S18(b), CLI side: the dead retrieval-without-archiver pair must exit 1 with a ✗ line.

    Red-proof: on the pre-fix code this configuration exited 0 under the tool's strongest
    all-clear sentence.
    """
    _set_config(monkeypatch, archiver_retrieval_url="http://arch.example:17668")
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "✗ archiver_retrieval" in out
    assert "config_error" in out
    assert "EPICS_MCP_ARCHIVER_URL" in out  # the actionable part of the detail line


def test_cli_inconclusive_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S12: a reachable plane whose identity probe FAILED exits 3 (INCONCLUSIVE), never a silent
    exit 0 with "Overall: OK". This is the S4 dead-container-behind-a-neighbour case.

    Red-proof: on the pre-fix 2-way CLI (``0 if ok else 1``) an inconclusive plane has ok=True →
    exit 0 and the render has no INCONCLUSIVE branch → "Overall: OK". Both assertions go red. It
    ALSO catches the FLAW-B trap: if the ok-line were left unchanged, ok=False → this exits 1.
    """
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify",
        lambda plane, *_a, **_k: PlaneCheck(
            plane=plane,
            configured=True,
            reachable=True,
            ca_ok=True,
            status="identity_probe_failed",
            identified=False,
            detail="transport reachable, but the identity probe FAILED: 401",
        ),
    )
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 3
    assert "INCONCLUSIVE" in out
    assert "Overall: OK" not in out
    assert "! channelfinder" in out  # the inconclusive glyph


def test_failing_dominates_inconclusive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S12 precedence: a HARD failure dominates an inconclusive identity probe → exit 1 / PROBLEM.

    Red-proof: a mutant that checks inconclusive before failing (``3 if inconclusive else 1``)
    returns 3 here.
    """
    _set_config(
        monkeypatch,
        channelfinder_url="http://cf.example/ChannelFinder",
        alarm_url="http://alarm:8081",
    )
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_mcp.services.doctor.AlarmClient",
        _cause_client(requests.exceptions.ConnectionError("refused")),  # alarm HARD-fails
    )
    monkeypatch.setattr(
        "epics_mcp.services.doctor._identify",
        lambda plane, *_a, **_k: PlaneCheck(
            plane=plane,
            configured=True,
            reachable=True,
            ca_ok=True,
            status="identity_probe_failed",
            identified=False,
            detail="probe failed",
        ),
    )
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "PROBLEM" in out
    assert "INCONCLUSIVE" not in out  # the hard failure wins the headline


def test_render_and_exit_agree() -> None:
    """S12: the verdict WORD (_render) and the exit CODE (main) both come from one _exit_category,
    so they cannot drift. A reorder of the _render elif-chain that said "OK" while main exits 3
    would break here.
    """
    privacy = PrivacyReport(cf_safe_owner_accounts=[], cf_safe_property_names=[])
    # Both gates OFF, so this fixture exercises the verdict WITHOUT the armed-gate tail. The tail
    # itself is pinned separately (test_an_armed_gate_is_named_on_the_verdict_line), because a
    # constant here would silently decide which branch every case below takes.
    write_safety = _disarmed_write_safety()

    def _mk(
        *,
        ok: bool,
        inconclusive: list[str],
        complete: bool,
        identified: list[str],
        degraded: list[str] | None = None,
    ) -> DoctorReport:
        return DoctorReport(
            planes=[],
            privacy=privacy,
            write_safety=write_safety,
            ok=ok,
            verification_complete=complete,
            degraded_planes=degraded or [],
            unverified_planes=[],
            inconclusive_identity_planes=inconclusive,
            identified_planes=identified,
        )

    cases = {
        "failed": (_mk(ok=False, inconclusive=[], complete=False, identified=[]), "PROBLEM", 1),
        "inconclusive": (
            _mk(ok=True, inconclusive=["cf"], complete=False, identified=[]),
            "INCONCLUSIVE",
            3,
        ),
        "clean": (_mk(ok=True, inconclusive=[], complete=True, identified=["cf"]), "OK", 0),
    }
    for category, (report, word, code) in cases.items():
        assert cli_doctor._exit_category(report) == category
        assert word in cli_doctor._render(report)
        assert cli_doctor._EXIT_CODE[cli_doctor._exit_category(report)] == code

    # A degraded plane is "clean" for the exit code AND must not wear the strongest confirmation
    # sentence. Both halves matter: the verdict line changes, the category (and so the exit code)
    # does not. Red-proof (mutation): delete the degraded branch in _render and "AS ITSELF" is
    # printed for a plane the report itself lists as not doing its job.
    degraded = _mk(
        ok=True, inconclusive=[], complete=True, identified=["archiver"], degraded=["archiver"]
    )
    assert cli_doctor._exit_category(degraded) == "clean"
    assert cli_doctor._EXIT_CODE["clean"] == 0
    rendered = cli_doctor._render(degraded)
    assert "AS ITSELF" not in rendered
    assert "NOT doing their job" in rendered and "archiver" in rendered


#: The honest-but-not-healthy report lists, paired with the status set each is built from in
#: ``run_doctor``. DERIVED rather than declared: the guard below asserts that this mapping covers
#: every such list on ``DoctorReport``, so a FOURTH category added to the report reddens it instead
#: of slipping past three hardcoded field names. That is the whole difference between this guard and
#: a set of literal assertions, and it is why the status sets are named here at all.
_HONEST_BUT_NOT_HEALTHY: dict[str, frozenset[str]] = {
    "inconclusive_identity_planes": _INCONCLUSIVE_STATUSES,
    "degraded_planes": _DEGRADED_STATUSES,
    "unverified_planes": frozenset({"unverified"}),
}


@pytest.mark.parametrize(
    ("ok", "degraded", "unverified", "inconclusive"),
    [
        # The three states the ticket measured, plus the pair that is hidden ONLY as a name.
        pytest.param(True, ["archiver"], ["olog", "naming"], [], id="degraded-hides-unverified"),
        pytest.param(True, ["archiver"], [], ["channelfinder"], id="inconclusive-hides-degraded"),
        pytest.param(True, [], ["olog"], ["channelfinder"], id="inconclusive-counts-unverified"),
        pytest.param(True, ["archiver"], ["olog"], ["channelfinder"], id="inconclusive-hides-both"),
        # Controls: one category alone is named by its own branch, and a clean report gets no tail.
        pytest.param(True, ["archiver"], [], [], id="control-degraded-alone"),
        pytest.param(True, [], ["olog"], [], id="control-unverified-alone"),
        pytest.param(True, [], [], ["channelfinder"], id="control-inconclusive-alone"),
        pytest.param(True, [], [], [], id="control-nothing-to-say"),
    ],
)
def test_the_verdict_names_every_honest_but_not_healthy_state(
    ok: bool, degraded: list[str], unverified: list[str], inconclusive: list[str]
) -> None:
    """BG-DFIX(a): the verdict named only the HIGHEST-ranking of the three honest-but-not-healthy
    categories and stayed silent about the rest, while ``verification_complete`` said False.

    Measured on the pre-fix code over all 16 states ``run_doctor`` can build: ELEVEN of them hid at
    least one plane NAME. The ticket described one of them (degraded hiding unverified); the
    inconclusive branch hid degraded entirely and disclosed unverified as a COUNT only, never as a
    name, which is why ``inconclusive-counts-unverified`` is a row here rather than a control.

    The claim is deliberately about NAMES, not about a mention: an operator who reads "1 other
    plane(s) also unverified" learns that something is wrong and not WHERE, and the two branches
    that do disclose (degraded, unverified) print names, so a count is the weaker standard of the
    same report.

    What this does NOT cover: the ``failed`` branch, which hides all three in seven states and needs
    the failing planes named FIRST to stay honest. That is its own guard
    (``test_the_problem_verdict_names_what_failed_before_what_did_not``), because a tail appended to
    a headline that names no plane at all would make the harmless planes the only named ones.

    Red-proof (per row, on the pre-fix code): ``degraded-hides-unverified`` and
    ``inconclusive-hides-degraded`` fail on the missing name, ``inconclusive-counts-unverified``
    fails because the count clause never names the plane.
    """
    report = DoctorReport(
        planes=[],
        privacy=PrivacyReport(cf_safe_owner_accounts=[], cf_safe_property_names=[]),
        write_safety=_disarmed_write_safety(),
        ok=ok,
        # The invariant run_doctor holds (services/doctor.py): a state built any other way is not
        # one the tool can produce, and pinning it here keeps the rows honest.
        verification_complete=not unverified and not inconclusive,
        degraded_planes=degraded,
        unverified_planes=unverified,
        inconclusive_identity_planes=inconclusive,
        identified_planes=degraded,
    )
    assert set(_HONEST_BUT_NOT_HEALTHY) <= set(DoctorReport.model_fields), (
        "a list in _HONEST_BUT_NOT_HEALTHY is not a field of DoctorReport any more"
    )
    # The categories come from the STATUS SETS, so a fourth honest-but-not-healthy status added to
    # the doctor reddens this instead of being quietly outside the three names below.
    covered = set().union(*_HONEST_BUT_NOT_HEALTHY.values())
    assert covered == _NON_FAILING_STATUSES - {"ok", "disabled", "info"} | _INCONCLUSIVE_STATUSES, (
        "the honest-but-not-healthy statuses drifted from the sets this guard reads: "
        f"{sorted(covered)}"
    )

    verdict = next(
        line for line in cli_doctor._render(report).splitlines() if line.startswith("Overall:")
    )

    for field in _HONEST_BUT_NOT_HEALTHY:
        for plane in getattr(report, field):
            assert plane in verdict, (
                f"{plane} is in {field} and the verdict does not name it: {verdict!r}"
            )


def test_the_problem_verdict_names_what_failed_before_what_did_not() -> None:
    """BG-DFIX(a), the failed branch: it is the state that hides the most, and the only one where
    disclosing the OTHER categories without also naming the failures would make things worse.

    The headline was "PROBLEM, a configured plane failed (see above)": it names no plane at all,
    because ``DoctorReport`` carries a list for every honest-but-not-healthy category and none for
    the failures. Appending the tail alone would therefore have made the harmless planes the ONLY
    ones named in the last line an operator reads, which inverts the urgency of the sentence. So the
    failures are named first, derived from ``report.planes`` through ``_FAILING_STATUSES`` rather
    than from a new report field: ``ok`` is computed from those same statuses, so the two cannot
    disagree, and no wire contract changes.

    ORDER is asserted, not just presence. "alarm is somewhere in the sentence" would also hold for
    the sentence that reads worst, the one that opens with the two planes nobody has to fix today.

    The COUNTS are asserted too, as the exact rendered fragments. They are the one part of this
    sentence nothing else pins: a post-build audit measured that adding a constant to both of them
    left the whole suite green, in a repository that has an open ticket about a prose number nobody
    compared (BG-DBYTE).

    Red-proof on the pre-fix code: no plane name appears at all, so both the naming and the ordering
    assertion fail.
    """
    planes = [
        PlaneCheck(plane="channelfinder", configured=True, status="unreachable", detail="refused"),
        PlaneCheck(plane="archiver", configured=True, status="no_ingest", identified=True),
        PlaneCheck(plane="olog", configured=True, status="unverified"),
    ]
    report = DoctorReport(
        planes=planes,
        privacy=PrivacyReport(cf_safe_owner_accounts=[], cf_safe_property_names=[]),
        write_safety=_disarmed_write_safety(),
        ok=False,
        verification_complete=False,
        degraded_planes=["archiver"],
        unverified_planes=["olog"],
        inconclusive_identity_planes=[],
        identified_planes=["archiver"],
    )

    verdict = next(
        line for line in cli_doctor._render(report).splitlines() if line.startswith("Overall:")
    )

    assert "channelfinder" in verdict, f"the failed plane is not named: {verdict!r}"
    assert "archiver" in verdict and "olog" in verdict, (
        f"a degraded and an unverified plane are in the report and not in the verdict: {verdict!r}"
    )
    assert verdict.index("channelfinder") < min(verdict.index("archiver"), verdict.index("olog")), (
        f"the verdict names a plane nobody has to fix before the one that failed: {verdict!r}"
    )
    assert "1 configured plane(s) FAILED: channelfinder" in verdict, (
        f"the failure count is not the number of failing planes: {verdict!r}"
    )
    assert "1 degraded (archiver); 1 unverified (olog)" in verdict, (
        f"a tail count does not match the list it summarises: {verdict!r}"
    )
    assert "(see above). Also" in verdict, (
        f"the headline and the tail run together as one sentence: {verdict!r}"
    )


def test_cli_verdict_with_nothing_configured_claims_no_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S18(c): with not a single REST plane configured, the verdict used to read "every configured
    plane answered AS ITSELF", vacuously true over the empty set, and it READS as a confirmation
    of probes that never ran. Nothing failed, so exit stays 0; but the sentence must say that
    nothing was verified either.

    Red-proof: on the pre-fix code this test FAILS (the strong sentence is printed).
    """
    _set_config(monkeypatch)  # all URLs empty → every REST plane disabled, live=info
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "AS ITSELF" not in out
    assert "nothing was identity-verified" in out


def test_cli_verdict_counts_the_identity_verified_planes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The strong sentence is earned by actual identifications and says how many there were."""
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "AS ITSELF (1 verified)" in out


async def test_identified_planes_is_the_positive_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """S18(c), machine side: ``verification_complete`` alone cannot tell "all confirmed" from
    "nothing ran", it is vacuously True on an empty config, and three docs tell scripts to read
    it. ``identified_planes`` is the positive counterpart to ``unverified_planes``: a script that
    wants POSITIVE confirmation asserts it is non-empty (found by the adversarial review of the
    first S18 fix, which had closed the vacuous truth only on the human-rendered verdict line).
    """
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_mcp.services.doctor.ChannelFinderClient", _OkClient)
    report = await run_doctor()
    assert report.identified_planes == ["channelfinder"]
    assert report.verification_complete is True


def test_every_plane_status_has_a_render_mark() -> None:
    """Every PlaneStatus value must carry its own glyph in the CLI render.

    ``_render`` falls back to "?" for an unknown status, which is the ``unverified`` mark, so a
    status missing from ``_STATUS_MARK`` would silently wear the honest-doubt glyph. This guard
    makes adding a status without a mark a red test instead (it caught exactly that while
    ``config_error`` was being added).
    """
    assert set(get_args(PlaneStatus)) == set(cli_doctor._STATUS_MARK)


def test_every_problem_status_names_a_remedy() -> None:
    """C1: a status that reports a PROBLEM must say what to change, not only what happened.

    Sibling of the render-mark guard above, and it borrows the same fail-closed property from
    ``test_status_partition_is_total_and_disjoint``: the two sets compared here tile
    ``PlaneStatus``, so a NEW failing status that nobody gave a remedy goes red here rather than
    shipping a finding the reader cannot act on.

    The second half is not decoration. Set equality alone is satisfied by ``{s: "" for s in ...}``,
    and so is every ``_REMEDY[...] in detail`` assertion in this file, because the empty string is
    in everything: the table could be gutted and the whole suite would stay green while the output
    lost every remedy. Requiring an opening imperative also rejects the softer failure, an entry
    that restates the finding ("The host is unreachable.") instead of directing the reader.

    Red-proof: delete an entry, set one to "", or reword one to open with a noun.
    """
    assert set(_REMEDY) == _FAILING_STATUSES | _INCONCLUSIVE_STATUSES

    for status, remedy in sorted(_REMEDY.items()):
        assert remedy.strip(), f"{status} carries an empty remedy, which asserts nothing anywhere"
        assert remedy.split()[0] in _REMEDY_IMPERATIVES, (
            f"the remedy for {status} opens with {remedy.split()[0]!r}, which is not one of "
            f"{sorted(_REMEDY_IMPERATIVES)}: a remedy tells the reader what to DO"
        )
        # No positional reference. Measured on the rendered output: an unreachable detail ends in a
        # urllib3 exception several hundred characters long and the remedy follows on the SAME line,
        # so "named above" pointed back across all of it at something the reader had lost. Red on
        # the first wording of four of these seven entries.
        for positional in (" above", " below"):
            assert positional not in remedy, (
                f"the remedy for {status} refers to a position ({positional.strip()!r}); it is "
                "appended to a detail that can be hundreds of characters long, so it has to name "
                "or describe what it means"
            )


def test_config_error_has_exactly_one_construction_site() -> None:
    """The ``config_error`` remedy says "the variable named at the start of this finding", and a
    positional reference is only affordable while ONE site produces this status.

    Unlike the other six, this remedy points INTO its observation instead of standing alone, because
    the variable to set is site knowledge a status-keyed table cannot hold. That is affordable at
    one site and a wrong instruction at a second one shaped differently (two conflicting values,
    say), so the count is pinned rather than left to be noticed.

    What this does NOT check is that the observation actually LEADS with the variable to set. That
    is the claim the remedy makes, and it is pinned on the rendered detail by
    ``test_the_first_variable_a_finding_names_is_the_one_to_edit``. The two are halves of one
    contract, and saying so here is the point: an earlier version of this docstring rested the
    remedy on "the empty variable this finding names", implying that emptiness semantics were
    covered by the count below. They never were, and a one-word edit to the observation proved it
    while every assertion in the suite stayed green.

    ⚠️ Honest limit: this counts CONSTRUCTIONS, not call sites. Move the construction into a helper
    the way ``_backend_down`` is one, call it twice, and this stays at 1 while two sites exist. It
    is kept anyway because it catches the realistic route (a second inline ``PlaneCheck``), and the
    blind spot is named here rather than discovered later.

    Red-proof: add a second inline ``PlaneCheck(status="config_error")`` anywhere in the module.
    """
    tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))

    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "status"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == "config_error"
    ]

    assert len(sites) == 1, (
        f"{len(sites)} sites construct a config_error PlaneCheck (lines "
        f"{[node.lineno for node in sites]}); the status-wide remedy points at 'the variable "
        "named at the start of this finding', which a differently shaped second site would not "
        "provide"
    )


#: The wording both positional remedies share. Pinned as MEMBERSHIP, not as prose: the guard below
#: has to cover every status that makes this promise, and a set comparison is what notices a third
#: one joining. What the sentence says beyond that stays free (docs/known-limits.md section 13).
_POSITIONAL_PROMISE = "named at the start of this finding"


@pytest.mark.parametrize(
    ("status", "archiver_url", "retrieval_url", "expected_var", "expected_var_is_empty"),
    [
        pytest.param(
            "config_error",
            "",
            "http://arch.example:17668",
            "EPICS_MCP_ARCHIVER_URL",
            True,
            id="config_error",
        ),
        pytest.param(
            "unreachable",
            "http://arch.example:17665",
            "http://arch.example:17668",
            "EPICS_MCP_ARCHIVER_RETRIEVAL_URL",
            False,
            id="unreachable",
        ),
    ],
)
async def test_the_first_variable_a_finding_names_is_the_one_to_edit(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    archiver_url: str,
    retrieval_url: str,
    expected_var: str,
    expected_var_is_empty: bool,
) -> None:
    """Two remedies tell the reader that the variable to edit is "named at the start of this
    finding". That is a claim about the ORDER of the rendered detail, so it is checked there.

    Why this guard exists as well as the site count above: the count pins how MANY sites produce
    ``config_error``, which is not the claim the remedy makes. Measured on the predecessor wording
    ("the empty variable this finding names"), a one-word edit to the observation made it report two
    variables as empty and no assertion anywhere noticed. A positional claim can be checked without
    pinning prose, which is why the wording moved onto one.

    Both rows run through ``_check_retrieval_plane`` on purpose: it is the one plane that reaches
    BOTH statuses, and the only one where the variable genuinely varies (mgmt vs. split-port), so a
    row that passes for the wrong reason has nowhere to hide. Faked at the TRANSPORT seam
    (``rest_get_json``) rather than by doubling a client class, for the reason recorded at
    ``test_unreachable_retrieval_names_the_variable_the_url_came_from``.

    The name is not compared against a literal alone: it is cross-checked against the config that
    produced the finding, so the test cannot drift into asserting a fixture string. For
    ``config_error`` the variable it names must really be the EMPTY one; for ``unreachable`` it must
    really be the one that carried the URL that failed.

    ⚠️ What this does NOT catch: swapping the observation's predicate ("is empty" for "is set")
    leaves it green. Deliberate. The variable to edit is still the first one named, so what the
    operator DOES stays right and only the descriptive clause would be wrong, and a guard over that
    clause would pin prose that section 13 of docs/known-limits.md keeps free.

    Red-proof, per row: restoring the old observation order fails ``config_error``; passing a
    different name as ``url_var`` fails ``unreachable``; copying the promise into a third
    ``_REMEDY`` entry fails the membership assertion.
    """
    promise_makers = {name for name, text in _REMEDY.items() if _POSITIONAL_PROMISE in text}
    assert promise_makers == {"config_error", "unreachable"}, (
        f"these statuses promise the reader a POSITION: {sorted(promise_makers)}, but this guard "
        "checks config_error and unreachable. A status that makes the promise without being "
        "checked here is exactly the gap this pair of guards exists to close"
    )

    cfg = _set_config(monkeypatch, archiver_url=archiver_url, archiver_retrieval_url=retrieval_url)
    monkeypatch.setattr(
        "epics_mcp.services.doctor.rest_get_json",
        Mock(side_effect=RestConnectionError("refused")),
    )

    check = await _check_retrieval_plane(cfg, 5.0)
    detail = check.detail or ""
    named = re.findall(r"EPICS_MCP_[A-Z_]+", detail)

    assert check.status == status, f"expected {status}, got {check.status}: {detail!r}"
    assert named, f"a {status} finding names no EPICS_MCP_* variable at all: {detail!r}"
    assert named[0] == expected_var, (
        f"the remedy for {status} points at the variable 'named at the start of this finding', but "
        f"the first one named is {named[0]}, not {expected_var}: {detail!r}"
    )

    # The cross-check: the name is only right if the CONFIG agrees about what it is.
    values = {
        "EPICS_MCP_ARCHIVER_URL": cfg.archiver_url,
        "EPICS_MCP_ARCHIVER_RETRIEVAL_URL": cfg.archiver_retrieval_url,
    }
    assert (values[expected_var] == "") is expected_var_is_empty, (
        f"{expected_var} is {values[expected_var]!r} in the config that produced this {status}, "
        "which is not the role this row asserts it plays"
    )


def test_a_healthy_status_gets_no_remedy_appended() -> None:
    """C1, the other direction: ``_with_remedy`` returns the observation UNCHANGED where there is no
    remedy, byte for byte.

    Stated as an identity rather than as "no remedy is present", which would be a tautology over the
    table membership the guard above already pins. This form catches what that one cannot: a
    ``_REMEDY.get(status, "")`` reached through a formatting path that appends a separator anyway,
    or a well-meant catch-all default added later. Both would decorate an honest state
    (``no_ingest``, ``unverified``) with advice it must not carry.
    """
    observation = "appliance identity: appliance0; it holds 5 PV(s), all connected"
    # Spelled out so the statuses are LITERALS (``_with_remedy`` takes a ``PlaneStatus``, and a
    # ``str`` off the frozenset is an arg-type error under --strict), then pinned RELATIONALLY
    # against the set, so this list cannot quietly fall behind it.
    healthy: tuple[PlaneStatus, ...] = ("ok", "disabled", "info", "unverified", "no_ingest")
    assert set(healthy) == _NON_FAILING_STATUSES, "this list drifted from the honest-state set"

    for status in healthy:
        assert _with_remedy(status, observation) == observation, (
            f"{status} is an honest state, not a problem, and must not be given advice"
        )


def test_cli_bad_arg_exits_two() -> None:
    """argparse rejects an unknown flag with SystemExit(2), the usage-error convention."""
    with pytest.raises(SystemExit) as excinfo:
        cli_doctor.main(["--nonsense"])
    assert excinfo.value.code == 2


def test_cli_epicserror_exits_one_not_the_usage_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine internal EpicsError is a COMMAND failure (exit 1), never a usage error (QA-15).

    It used to return 2, the code ``cli_common._USAGE_ERROR`` and argparse both reserve for "the
    caller passed something wrong". A wrapper acting on that told the operator to check their
    arguments while the command itself had broken. The test above (``--nonsense``) is the real
    usage error and still exits 2, so the two codes now mean two different things.

    Red on the pre-fix code: measured 2 against an asserted 1.
    """

    async def _boom(**kwargs: object) -> DoctorReport:
        raise EpicsError("internal", error_code="INTERNAL")

    monkeypatch.setattr("epics_mcp.cli_doctor.run_doctor", _boom)

    assert cli_doctor.main([]) == 1


def test_cli_epicserror_is_distinguishable_from_a_failed_plane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The named cost of QA-15, made executable rather than left as a comment.

    Exit 1 is now shared by an internal error and a hard-failed plane, so the exit code alone no
    longer separates them. What DOES separate them is asserted here: the internal path writes a
    ``doctor:`` line to stderr and produces no report on stdout, the plane path produces the report
    and stays silent on stderr. A reader of the trade-off comment can check it instead of trusting
    it.
    """

    async def _boom(**kwargs: object) -> DoctorReport:
        raise EpicsError("internal", error_code="INTERNAL")

    # The plane path first, while run_doctor is still the real one: S18(b)'s dead
    # retrieval-without-archiver pair is a hard-failed plane, exit 1.
    _set_config(monkeypatch, archiver_retrieval_url="http://arch.example:17668")
    assert cli_doctor.main([]) == 1
    plane = capsys.readouterr()

    monkeypatch.setattr("epics_mcp.cli_doctor.run_doctor", _boom)
    assert cli_doctor.main([]) == 1
    internal = capsys.readouterr()

    assert plane.out != "" and plane.err == ""  # a report, nothing on stderr
    assert internal.out == "" and internal.err.startswith("doctor: ")  # the inverse, exactly


# --- write-safety posture (BG-DSAFE) ------------------------------------------------------------
#
# These deliberately do NOT stop at "the report echoes the config". That assertion cannot fail for
# any implementation that reads the config, so it measures nothing. What decides whether a write
# happens is the GATE, so the cross-checks build the real SafetyLayer / OlogWriteGate from the SAME
# config and show that what the block reports is what those objects do, each with a positive and a
# negative control.


def test_the_reported_pv_pattern_is_the_one_the_real_gate_enforces() -> None:
    """Cross-check rather than echo: the reported pattern is bound to observed gate behaviour.

    Red-proof: report ``pv_write_pattern`` from any other source (a constant, another field) and
    the two probes below stop agreeing with the string this asserts on.
    """
    pattern = r"^SIM:PS-01:.*-SP$"
    cfg = _write_config(allow_pv_write=True, pv_write_pattern=pattern)
    report = _write_safety_report(cfg)
    with _isolated_audit_loggers():
        gate = SafetyLayer(cfg, environ=_LOOPBACK_ENV)

    assert report.pv.armed is True
    assert report.pv.name_pattern == pattern
    gate.check_write_allowed("SIM:PS-01:Cur-SP")  # positive control: the gate admits it
    with pytest.raises(PVWriteDeniedError):  # negative control: and refuses the neighbour
        gate.check_write_allowed("SIM:PS-02:Cur-SP")


def test_the_reported_pattern_is_flagged_when_it_allows_every_pv() -> None:
    """``.*`` is the sanctioned way to say "everything", so it has to be LOUD, not merely shown.

    The flag is a string comparison against a closed set, never an attempt to decide regex
    universality. Both directions, because a flag that is always True is the same as no flag.
    """
    wide = _write_safety_report(_write_config(allow_pv_write=True, pv_write_pattern=".*"))
    narrow = _write_safety_report(
        _write_config(allow_pv_write=True, pv_write_pattern=r"^SIM:PS-01:.*-SP$")
    )

    assert wide.pv.pattern_allows_every_name is True
    assert narrow.pv.pattern_allows_every_name is False
    # And it is not a stand-in for "the gate is off": on a default config the pattern is EMPTY,
    # which is a refuse-to-start rather than an allow-all.
    assert _write_safety_report(_write_config()).pv.pattern_allows_every_name is False


def test_the_reported_logbooks_are_the_set_the_real_olog_gate_enforces() -> None:
    """Cross-check for the Olog half: reported set versus an admitted and a refused write.

    Red-proof: report the raw ``olog_write_logbooks`` string unsplit, or drop the sort, and the
    equality below fails while both gate probes keep passing, which is exactly the drift it guards.
    """
    cfg = _write_config(
        allow_olog_write=True,
        olog_write_logbooks=" Commissioning , Operations ",
        olog_url="http://127.0.0.1:8080/Olog",
    )
    report = _write_safety_report(cfg)
    with _isolated_audit_loggers():
        gate = OlogWriteGate(cfg)

    assert report.olog.logbooks == ["Commissioning", "Operations"]
    gate.check_write_preconditions(["Commissioning"])  # positive control
    with pytest.raises(OlogWriteDeniedError):  # negative control
        gate.check_write_preconditions(["SomewhereElse"])


@pytest.mark.parametrize(
    ("url", "allow_remote", "allowlist", "allowed", "loopback"),
    [
        ("http://127.0.0.1:8080/Olog", False, "", True, True),
        ("https://olog.example.org/Olog", False, "https://olog.example.org/Olog", False, False),
        ("https://olog.example.org/Olog", True, "https://olog.example.org/Olog", True, False),
        ("http://olog.example.org/Olog", True, "http://olog.example.org/Olog", False, False),
        ("garbage", True, "garbage", False, False),
    ],
)
def test_the_reported_target_verdict_matches_what_the_gate_does_to_a_write(
    url: str, allow_remote: bool, allowlist: str, allowed: bool, loopback: bool
) -> None:
    """The WHERE half, cross-checked per case: the report versus a real write attempt.

    ``target_allowed`` and ``target_is_loopback`` are deliberately two fields. The third row is
    allowed AND remote, i.e. a write that reaches a real logbook, and a reader who took the first
    field for the second would call that configuration a sandbox.
    """
    cfg = _write_config(
        allow_olog_write=True,
        olog_write_logbooks="Commissioning",
        olog_url=url,
        olog_write_allow_remote=allow_remote,
        olog_write_url_allowlist=allowlist,
    )
    report = _write_safety_report(cfg)
    assert (report.olog.target_allowed, report.olog.target_is_loopback) == (allowed, loopback)

    with _isolated_audit_loggers():
        gate = OlogWriteGate(cfg)
    if allowed:
        gate.check_write_preconditions(["Commissioning"])
    else:
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_preconditions(["Commissioning"])


def test_an_olog_url_with_credentials_is_redacted_in_the_report() -> None:
    """Doctor output is what an operator pastes into a ticket, and this is the one field of the
    block whose value can carry ``user:password@``."""
    cfg = _write_config(olog_url="https://svc:hunter2@olog.example.org/Olog")

    assert "hunter2" not in _write_safety_report(cfg).olog.target_url


def test_the_reported_reach_violations_decide_whether_the_real_gate_can_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-check for the reach: no findings exactly when a write-enabled SafetyLayer constructs.

    This is the field with the highest chance of being read off the WRONG source, because the
    doctor already prints a search posture for the live plane and that one answers a different
    question (the ACTIVE provider only). Binding it to whether the gate can be BUILT is what keeps
    the block from inheriting that other answer.
    """
    cfg = _write_config(allow_pv_write=True, pv_write_pattern=r"^SIM:PS-01:.*-SP$")

    for name, value in _LOOPBACK_ENV.items():
        monkeypatch.setenv(name, value)
    assert _write_safety_report(cfg).pv.search_reach_violations == []
    with _isolated_audit_loggers():
        SafetyLayer(cfg)  # constructs: a write-enabled server would start here

    monkeypatch.delenv("EPICS_CA_AUTO_ADDR_LIST")  # one provider's broadcast back ON
    assert _write_safety_report(cfg).pv.search_reach_violations != []
    with pytest.raises(SafetyConfigError):
        SafetyLayer(cfg)


def test_the_block_is_built_without_constructing_either_gate() -> None:
    """The configuration that makes SafetyLayer REFUSE must still produce a report.

    Armed with an empty allowlist is a refuse-to-start, and it is exactly a state an operator needs
    the doctor to describe. It is also reachable through ``epics-init``, which puts the block it
    has just composed into ``os.environ`` and runs this command against it.

    Red-proof: construct a SafetyLayer inside ``_write_safety_report`` and this raises
    SafetyConfigError instead of returning a report.
    """
    cfg = _write_config(allow_pv_write=True, pv_write_pattern="")

    report = _write_safety_report(cfg)

    assert (report.pv.armed, report.pv.name_pattern) == (True, "")
    with pytest.raises(SafetyConfigError):  # the control: the gate really does refuse this config
        SafetyLayer(cfg, environ=_LOOPBACK_ENV)


# --- the audit sink probe -----------------------------------------------------------------------


def test_audit_sink_unset_is_undecided_and_says_stderr() -> None:
    """Not False: stderr IS writable, it merely does not survive a restart. Answering "no" would
    be a claimed finding about a state that is only un-durable."""
    writable, note, _resolved = _probe_audit_sink("")

    assert writable is None
    assert "stderr" in note


def test_audit_sink_accepts_an_append_on_a_real_file(tmp_path: Path) -> None:
    file = tmp_path / "audit.log"
    file.write_text("first line\n", encoding="utf-8")

    writable, note, _resolved = _probe_audit_sink(str(file))

    assert writable is True
    assert "append" in note


def test_probing_an_existing_sink_changes_nothing_about_it(tmp_path: Path) -> None:
    """The read-only contract at its sharpest. The probe opens a WRITE handle, so prove that it
    writes nothing and moves no timestamp; ``run_doctor`` documents "it probes, never writes"."""
    file = tmp_path / "audit.log"
    file.write_text("first line\n", encoding="utf-8")
    before = file.stat()

    assert _probe_audit_sink(str(file))[0] is True

    after = file.stat()
    assert file.read_text(encoding="utf-8") == "first line\n"
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_audit_sink_with_a_missing_parent_is_a_hard_no(tmp_path: Path) -> None:
    """The one half of the missing-file case that IS decidable: the handler creates no directory,
    so this configuration cannot work and the report may say so."""
    writable, note, _resolved = _probe_audit_sink(str(tmp_path / "nodir" / "audit.log"))

    assert writable is False
    assert "parent directory" in note


def test_audit_sink_that_does_not_exist_yet_is_undecided(tmp_path: Path) -> None:
    """Undecidable, not "no". Nothing read-only on Windows predicts whether the create succeeds
    (``os.access`` on a directory is a measured false positive), so the honest answer is None."""
    writable, note, _resolved = _probe_audit_sink(str(tmp_path / "audit.log"))

    assert writable is None
    assert "does not exist yet" in note


def test_audit_sink_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    """``os.access`` answers True here on Windows, measured. The append probe answers correctly,
    which is the whole reason it is the probe."""
    assert _probe_audit_sink(str(tmp_path))[0] is False


def test_the_audit_probe_raises_nothing_on_an_unusable_path() -> None:
    """It runs OUTSIDE the total gather and cli_doctor catches only EpicsError, so anything that
    escaped here would leave the command as a bare traceback. A NUL byte is the reachable case."""
    writable, note, _resolved = _probe_audit_sink("audit\x00.log")

    assert writable is False
    assert note  # never empty: an empty note satisfies every containment assertion and says nothing


def test_the_audit_probe_never_creates_the_file(tmp_path: Path) -> None:
    """Read-only, through the builder that the report uses.

    Red-proof: add ``os.O_CREAT`` to the probe's flags, or switch it to ``open(path, "a")``, and
    the file exists afterwards.
    """
    target = tmp_path / "audit.log"

    _write_safety_report(_write_config(audit_log_file=str(target)))

    assert not target.exists()


# --- the block in the report and in the render --------------------------------------------------


async def test_run_doctor_carries_the_write_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, **_WRITE_GATE_DEFAULTS)
    report = await run_doctor()

    assert report.write_safety.pv.armed is False
    assert report.write_safety.olog.armed is False


def test_cli_json_carries_the_write_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field that only exists in the model is invisible to CI, which is where this is read."""
    _set_config(monkeypatch, **_WRITE_GATE_DEFAULTS)

    assert cli_doctor.main(["--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload["write_safety"]) == {"pv", "olog", "audit"}
    assert payload["write_safety"]["pv"]["armed"] is False


def test_the_render_shows_both_gates_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red-proof: drop the block from ``_render`` and every assertion here goes."""
    _set_config(monkeypatch, **_WRITE_GATE_DEFAULTS)

    assert cli_doctor.main([]) == 0

    out = capsys.readouterr().out
    assert "Write gates" in out
    assert "PV write:   OFF" in out
    assert "Olog write: OFF" in out
    # The heading has to name WHOSE environment was read: this command runs in its own process and
    # a running server was started from a different env block.
    assert "THIS command's environment" in out


def test_the_render_shows_where_an_armed_gate_can_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The armed state, and the WHERE with it. An approver asks "can this write, and where", and
    the word ARMED answers only the first half."""
    audit = tmp_path / "audit.log"
    audit.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "epics_mcp.services.doctor.get_config",
        lambda: _write_config(
            allow_pv_write=True,
            pv_write_pattern=r"^SIM:PS-01:.*-SP$",
            allow_olog_write=True,
            olog_write_logbooks="Commissioning",
            olog_url="http://127.0.0.1:8080/Olog",
            audit_log_file=str(audit),
        ),
    )
    for name, value in _LOOPBACK_ENV.items():
        monkeypatch.setenv(name, value)

    cli_doctor.main([])

    out = capsys.readouterr().out
    assert "PV write:   ARMED" in out
    assert r"^SIM:PS-01:.*-SP$" in out
    assert "search reach: loopback-only" in out
    assert "logbooks: Commissioning" in out
    assert "loopback, a local test server" in out
    assert "writable" in out


def test_the_render_says_deny_all_rather_than_leaving_an_empty_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty Olog allowlist on an armed gate is DENY-ALL, and the naive reading is its opposite.

    Red-proof: print the joined list and nothing else, and the line reads ``logbooks:`` with
    nothing after it, which no reader takes to mean "every write is denied".
    """
    monkeypatch.setattr(
        "epics_mcp.services.doctor.get_config",
        lambda: _write_config(allow_olog_write=True, olog_url="http://127.0.0.1:8080/Olog"),
    )

    cli_doctor.main([])

    assert "every Olog write is denied" in capsys.readouterr().out


def test_an_armed_gate_is_named_on_the_verdict_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verdict is the LAST line and is labelled "Overall", so a reader takes everything above
    it to be in its scope. "Overall: OK" above an ARMED gate therefore reads as an all-clear on the
    write posture, which nothing here measured. Same remedy the degraded branch already uses.

    Red-proof: delete the tail and ARMED appears in the block while the last line says only OK.
    """
    monkeypatch.setattr(
        "epics_mcp.services.doctor.get_config", lambda: _write_config(allow_pv_write=True)
    )

    exit_code = cli_doctor.main([])

    out = capsys.readouterr().out
    assert "Nothing here CHECKED a write gate" in out
    assert "the PV write gate is ARMED" in out
    assert exit_code == 0  # informative: the block is not a verdict and moves no exit code


def test_an_armed_gate_moves_neither_ok_nor_the_verdict_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The informative contract, asserted rather than promised."""
    for armed in (False, True):
        monkeypatch.setattr(
            "epics_mcp.services.doctor.get_config",
            lambda armed=armed: _write_config(allow_pv_write=armed),
        )
        report = asyncio.run(run_doctor())
        assert report.ok is True
        assert cli_doctor._exit_category(report) == "clean"


# --- the RENDER, state by state ------------------------------------------------------------------
#
# The first version of this section cross-checked the MODEL thoroughly and then asserted the render
# only along the all-clear path. Measured with mutants: a render that always printed
# "search reach: loopback-only", and one that turned the empty-pattern line into "so every PV is
# writable", both survived the entire file. Those are the two sentences an approver acts on, and the
# second is the exact misreading the line exists to prevent. Every output shape gets a case here.


def _render_with(**overrides: object) -> str:
    """Render a full report for a config, with every write-gate field pinned. No network: the
    default config configures no REST plane, so ``run_doctor`` probes nothing."""
    cfg = _write_config(**overrides)
    report = asyncio.run(_report_for(cfg))
    return cli_doctor._render(report)


async def _report_for(cfg: EpicsConfig) -> DoctorReport:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("epics_mcp.services.doctor.get_config", lambda: cfg)
        return await run_doctor()


def _write_block_of(rendered: str) -> list[str]:
    """The write-gate block, sliced out of a full report by its heading and the blank line after.

    Sliced rather than obtained from ``_write_safety_lines`` directly, because the block's PLACE in
    the report is part of what these tests hold: a block rendered correctly into a variable and
    never appended would satisfy every substring assertion in this file.
    """
    lines = rendered.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Write gates"))
    end = next(i for i in range(start, len(lines)) if not lines[i].strip())
    return lines[start:end]


# The two states below are pinned LINE BY LINE rather than by substring, and that is a deliberate
# change of instrument. An exhaustive census of this block counted 30 outcomes, of which 15 were
# held by an assertion and 11 by none, and each of the 11 was a separate one-line test away from
# being held. Substring assertions also let a mutant satisfy a sibling branch's phrasing: measured,
# an ARMED Olog gate printing the disarmed sentence survived the entire suite, because the only
# test naming that sentence renders the DISARMED state. A full-text pin cannot be satisfied by a
# neighbouring branch's wording, and it holds the reassuring parentheticals, the two rate limits,
# the value slots and the heading's disclaimer in one place. The cost is that any deliberate
# rewording touches this list, which is the intended cost: this block is read by someone deciding
# whether a server may write.
_DEFAULT_WRITE_BLOCK = [
    "Write gates (as THIS command's environment has them; a running server may have been",
    "started with a different one):",
    "  PV write:   OFF (no PV write can leave this server)",
    "  Olog write: OFF (no logbook entry can leave this server)",
    "  audit log:  not set, so the trail goes to stderr and is lost on restart (a write-enabled",
    "              server refuses to start without a durable path)",
]


def test_the_write_block_of_a_shipped_default_renders_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state every fresh install is in, and the one the surrounding prose kept describing as if
    it were the armed one.

    Red-proof: three separate mutants survived the whole suite before this test: dropping the two
    audit lines entirely, rewording either OFF parenthetical, and inverting the heading's second
    line, which is where the block says whose environment it read.
    """
    for name, value in _LOOPBACK_ENV.items():
        monkeypatch.setenv(name, value)

    assert _write_block_of(_render_with()) == _DEFAULT_WRITE_BLOCK


def test_the_write_block_of_a_fully_armed_install_renders_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both gates armed, every value slot filled, so every ARMED line is held at once.

    Red-proof: an ARMED Olog gate printing ``Olog write: OFF (no logbook entry can leave this
    server)`` survived the entire suite, and so did a PV rate limit pinned to a constant. The two
    gates carry DIFFERENT defaults (10 and 5), so swapping the two model fields is caught here too.
    """
    audit = tmp_path / "audit.log"
    audit.write_text("", encoding="utf-8")
    for name, value in _LOOPBACK_ENV.items():
        monkeypatch.setenv(name, value)

    rendered = _render_with(
        allow_pv_write=True,
        pv_write_pattern=r"^SIM:PS-01:.*-SP$",
        allow_olog_write=True,
        olog_write_logbooks="Commissioning,Operations",
        olog_url="http://127.0.0.1:8080/Olog",
        audit_log_file=str(audit),
    )

    assert _write_block_of(rendered) == [
        "Write gates (as THIS command's environment has them; a running server may have been",
        "started with a different one):",
        "  PV write:   ARMED",
        "              PV names allowed: ^SIM:PS-01:.*-SP$",
        "              (a regular expression; the WHOLE name has to match. Whether it is wide was",
        "              checked against a fixed list of spellings, not by reading the expression)",
        "              at most 10 writes per minute",
        "              search reach: loopback-only",
        "  Olog write: ARMED",
        "              logbooks: Commissioning, Operations",
        "              at most 5 writes per minute",
        "              target: http://127.0.0.1:8080/Olog (loopback, a local test server)",
        f"  audit log:  {audit}, writable",
    ]
    assert rendered.splitlines()[-1] == (
        "         Nothing here CHECKED a write gate, and the PV and Olog write gates are ARMED "
        "(see the block above)."
    )


def test_the_verdict_tail_is_absent_when_no_gate_is_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counter-direction of the tail, which nothing held: both tests that assert the tail arm a
    gate first, so a tail printed unconditionally was green.

    Red-proof: ``armed = _armed_gate_names(report) or ["PV"]``. That mutant survived the whole
    suite, and it makes the LAST line of every default report claim an armed write gate while the
    block four lines above it says OFF.
    """
    for name, value in _LOOPBACK_ENV.items():
        monkeypatch.setenv(name, value)

    rendered = _render_with()

    assert "Nothing here CHECKED a write gate" not in rendered
    assert "ARMED" not in rendered
    assert rendered.splitlines()[-1].startswith("Overall:")


def test_the_render_names_the_reach_violations_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WHERE half of an armed PV gate, in the state that matters.

    Red-proof: make the reach branch unconditional (``if False:``) so the line always reads
    ``loopback-only``. That mutant survived the whole file before this test existed, which means the
    report could claim a loopback-only reach for a configuration that is not one.
    """
    monkeypatch.delenv("EPICS_CA_AUTO_ADDR_LIST", raising=False)
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")

    out = _render_with(allow_pv_write=True, pv_write_pattern=r"^SIM:PS-01:.*-SP$")

    assert "search reach: NOT loopback-only" in out
    assert "refuses to start" in out
    assert "EPICS_CA_AUTO_ADDR_LIST" in out  # the finding names the variable, not just the verdict
    assert "search reach: loopback-only" not in out  # and never both


def test_the_render_says_an_empty_pattern_refuses_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty allowlist on an armed gate is a refuse-to-start, and the naive reading is its
    opposite ("nothing listed, so everything goes").

    Red-proof: replace the line with "(none set, so every PV is writable)". That mutant survived
    the whole file, and it states the inversion the code exists to prevent.
    """
    out = _render_with(allow_pv_write=True, pv_write_pattern="")

    assert "PV names allowed: (none set, so a write-enabled server refuses to start)" in out
    assert "writable" not in out.split("Olog write")[0]


#: The spellings this project promises to recognise as "allow every PV name", DECLARED here rather
#: than read from the code. Deriving them from ``_ALLOW_EVERY_PV_NAME`` is what the first version
#: did, and it was a tautology of the exact kind this file warns about two hundred lines up:
#: deleting a member deleted its own test case, so the guard could not fail. Measured, that mutant
#: survived the whole file. A declared list fails on both a removal and a silent addition.
_EXPECTED_ALLOW_ALL_SPELLINGS = frozenset(
    {
        ".*",
        ".*$",
        "^.*",
        "^.*$",
        ".*?",
        ".*?$",
        "^.*?",
        "^.*?$",
        "(.*)",
        "(.*)$",
        "^(.*)",
        "^(.*)$",
        "(?:.*)",
        "(?:.*)$",
        "^(?:.*)",
        "^(?:.*)$",
        ".{0,}",
        ".{0,}$",
        "^.{0,}",
        "^.{0,}$",
        "[\\s\\S]*",
        "[\\s\\S]*$",
        "^[\\s\\S]*",
        "^[\\s\\S]*$",
        "\\A.*",
        ".*\\Z",
        "\\A.*\\Z",
    }
)


def test_every_family_of_the_declared_spellings_carries_all_of_its_anchors() -> None:
    """The anchor asymmetry, guarded rather than narrated. The set's own docstring tells the story
    of ``^.*`` being known while ``.*$`` was not; the SECOND version of it repeated that mistake in
    four more families, which is what this asserts against.

    Derived from the declared list, so it also fails if a family is added with a gap.

    Red-proof: drop ``(.*)$`` from the declared list and the ``(.*)`` family reports 3 of 4.
    """
    incomplete = {
        core: sorted(
            anchored
            for anchored in (core, f"{core}$", f"^{core}", f"^{core}$")
            if anchored not in _EXPECTED_ALLOW_ALL_SPELLINGS
        )
        for core in (".*", ".*?", "(.*)", "(?:.*)", ".{0,}", "[\\s\\S]*")
    }

    assert not {core: missing for core, missing in incomplete.items() if missing}


def test_the_recognised_allow_all_spellings_are_the_declared_ones() -> None:
    """Set equality, both directions. A removal loses a warning an operator relies on; an addition
    that nobody declared is a claim about a spelling nothing checked."""
    assert _ALLOW_EVERY_PV_NAME == _EXPECTED_ALLOW_ALL_SPELLINGS


@pytest.mark.parametrize("pattern", sorted(_EXPECTED_ALLOW_ALL_SPELLINGS))
def test_every_declared_allow_all_spelling_is_called_out(pattern: str) -> None:
    """All of them, not one. An operator writes the anchored form because ``safety.py``'s own error
    message teaches it, and the first version of the set knew ``^.*`` and not ``.*$``, which
    ``re.fullmatch`` treats identically.

    Red-proof: drop any member from ``_ALLOW_EVERY_PV_NAME`` and its row here fails, because the
    rows come from the declared list rather than from the set under test.
    """
    out = _render_with(allow_pv_write=True, pv_write_pattern=pattern)

    assert "allows EVERY PV" in out


def test_a_narrow_pattern_is_not_called_out() -> None:
    """The counter-direction: a hint that always fires is the same as no hint."""
    out = _render_with(allow_pv_write=True, pv_write_pattern=r"^SIM:PS-01:.*-SP$")

    assert "allows EVERY PV" not in out
    assert "the WHOLE name has to match" in out
    # And silence about the width is not a claim of narrowness: the line says what was checked.
    assert "checked against a fixed list of spellings, not by reading the expression" in out


@pytest.mark.parametrize("wide", [".*|", "|.*", "^$|.*", "(?s).*"])
def test_a_pattern_outside_the_declared_spellings_is_not_called_narrow(wide: str) -> None:
    """The honest half of a hint that cannot be complete.

    Each pattern here admits EVERY PV name and none is in the declared list, which is a deliberate
    limit rather than an oversight: the flag compares strings and refuses to interpret or execute
    the expression. What the render must therefore not do is read as reassurance. It used to end
    at "the WHOLE name has to match", which is true of an allow-everything pattern too.

    ``.*|`` is not a hypothetical spelling: the anti-forgery docstring in ``cli_doctor`` uses it as
    its own worked example of a pattern ``SafetyLayer`` accepts for every name.

    Red-proof: delete the second half of the else-branch and every row fails.
    """
    assert all(re.fullmatch(wide, name) for name in ("A", "SIM:PS-01:Cur-SP", "x y"))  # the premise

    out = _render_with(allow_pv_write=True, pv_write_pattern=wide)

    assert "allows EVERY PV" not in out  # it is outside the list, so no positive claim either way
    assert "checked against a fixed list of spellings, not by reading the expression" in out


def test_a_pattern_that_does_not_compile_is_named_rather_than_shown_as_an_allowlist() -> None:
    """A FOURTH start condition of the PV gate, and the one the block used to hide.

    Measured: with a broken pattern the block printed it exactly like a working narrow one, under a
    line saying the whole name has to match it, while ``SafetyLayer`` refuses to construct at all.
    An operator reading the report would take a server that cannot start for one with a tight
    allowlist.

    Red-proof: report ``pattern_is_valid_regex=True`` unconditionally and the first two assertions
    fail while the control below keeps passing.
    """
    broken = "^SIM:[.*$"

    out = _render_with(allow_pv_write=True, pv_write_pattern=broken)

    assert "NOT a valid regular expression, so a write-enabled server refuses to start" in out
    assert "the WHOLE name has to match" not in out
    with pytest.raises(SafetyConfigError):  # the control: the gate really does refuse this config
        SafetyLayer(
            _write_config(allow_pv_write=True, pv_write_pattern=broken), environ=_LOOPBACK_ENV
        )


@pytest.mark.parametrize(
    ("url", "allow_remote", "allowlist", "expected"),
    [
        ("", False, "", "no Olog URL set"),
        ("http://127.0.0.1:8080/Olog", False, "", "loopback, a local test server"),
        (
            "https://olog.example.org/Olog",
            True,
            "https://olog.example.org/Olog",
            "REMOTE and allowlisted",
        ),
        ("https://olog.example.org/Olog", False, "", "NOT a permitted write target"),
    ],
)
def test_the_render_names_the_olog_target_in_every_shape(
    url: str, allow_remote: bool, allowlist: str, expected: str
) -> None:
    """Four target shapes, four sentences. Three of them were never executed by any test, including
    the allowlisted-remote one, which is the shape the docstring calls "a different approval from a
    loopback sandbox"."""
    out = _render_with(
        allow_olog_write=True,
        olog_write_logbooks="Commissioning",
        olog_url=url,
        olog_write_allow_remote=allow_remote,
        olog_write_url_allowlist=allowlist,
    )

    assert expected in out


#: The two lines the report adds under a target verdict that can rest on the allowlist, spelled out
#: here rather than imported from ``cli_doctor``: importing the constant under test would make
#: every assertion below true by construction, and the rewording this text has already had once is
#: exactly what a reader of the block should be made to look at.
_ADDRESS_NOTE = [
    "              (shown for reading. The string the gate works from is EPICS_MCP_OLOG_URL",
    "              exactly as configured, and this line need not match it character for character)",
]


def test_a_denied_target_does_not_read_as_the_string_the_gate_compared() -> None:
    """The address on this line is REDACTED, the gate reads the configured value exactly, and the
    two used to stand next to each other with nothing saying so (BG-DCMP).

    The configuration here is the sharp case rather than a generic one. The report prints exactly
    the string that is in the allowlist, character for character, and the gate denies anyway,
    because the configured value carries a userinfo that the printed line drops and the comparison
    reads the configured value. An operator who copies the address off this line into
    EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST is then looking at two values that read identically and a
    deny that will not go away. The collision is asserted as a PRECONDITION rather than assumed: a
    fixture that stopped producing it would leave the rest of this test green for a reason that
    has nothing to do with what it is named after.

    ⚠️ The MECHANISM here changed once, which is why the precondition is asserted at all. Until
    the line moved off the rebuilding redaction it was a case difference: the rebuild lower-cased
    the host, so ``https://Olog.Example.org/Olog`` printed as its allowlisted lower-case twin.
    ``shown_url`` deletes rather than rebuilds and therefore preserves case, so that spelling no
    longer collides (measured). What still does is anything the printed line legitimately DROPS,
    the userinfo below and a query string. The class is narrower than it was, and not empty, which
    is exactly why the note this test guards is still owed.

    ⚠️ The note claims NOTHING about a comparison having run, and the first draft did, which an
    adversarial pass showed to be false in seven states of this same branch. Six reach the refusal
    without any allowlist being read (five SEC-2 vetoes, plus an exactly-allowlisted target with
    EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE unset, where the and-chain short-circuits), and in the
    seventh, a plain-http allowlisted target, the comparison ran and SUCCEEDED while the https rule
    denied. Whoever rewords these two lines owes that enumeration again.

    ⚠️ This one test contacts the network, and it is the only reason it takes seconds: setting
    olog_url configures the Olog REST plane, so ``run_doctor`` probes it and the synthetic host
    fails to resolve three times. It renders through the full report rather than calling
    ``_olog_write_lines`` because the block's PLACE in the report is part of what is held here; the
    branch-by-branch checks below need no report and take none.

    Red-proof: remove the two added lines and the block comparison fails.
    """
    configured = "https://svc:pw@olog.example.org/Olog"
    allowlisted = "https://olog.example.org/Olog"
    overrides: dict[str, object] = {
        "allow_olog_write": True,
        "olog_write_logbooks": "Commissioning",
        "olog_url": configured,
        "olog_write_allow_remote": True,
        "olog_write_url_allowlist": allowlisted,
    }

    olog = _write_safety_report(_write_config(**overrides)).olog
    block = _write_block_of(_render_with(**overrides))
    start = block.index("  Olog write: ARMED")
    end = next(i for i in range(start, len(block)) if block[i].startswith("  audit log:"))

    # The precondition: what is printed IS the allowlist entry, and the gate refuses all the same.
    assert olog.target_url == allowlisted
    assert olog.target_allowed is False
    assert block[start:end] == [
        "  Olog write: ARMED",
        "              logbooks: Commissioning",
        "              at most 5 writes per minute",
        f"              target: {allowlisted} is NOT a permitted write target, "
        "so every write is denied",
        *_ADDRESS_NOTE,
    ]


@pytest.mark.parametrize(
    ("url", "allow_remote", "allowlist", "note_expected"),
    [
        ("", False, "", False),
        ("http://127.0.0.1:8080/Olog", False, "", False),
        ("https://olog.example.org/Olog", False, "", True),
        ("https://Olog.Example.org/Olog", True, "https://Olog.Example.org/Olog", True),
    ],
    ids=["no-url", "loopback", "refused", "allowlisted-remote"],
)
def test_only_the_allowlist_verdicts_warn_that_the_address_is_not_the_configured_string(
    url: str, allow_remote: bool, allowlist: str, note_expected: bool
) -> None:
    """Presence AND absence, over all four target shapes, because presence alone is half a guard.

    The ALLOWLISTED-REMOTE row is the one this test exists for. Its verdict says "allowlisted",
    which IS the result of the membership test, and it prints the same rebuilt address: measured,
    a target configured and allowlisted as ``https://Olog.Example.org/Olog`` prints
    ``https://olog.example.org/Olog``, so an operator tidying the allowlist to match what the
    report shows turns a working gate into a deny-all. That branch had no note, and a mutant
    copying the note onto it survived the entire suite, so nothing held the question either way.

    The two absent rows are not decoration. Loopback is a property of the ADDRESS rather than of a
    configured string, and the no-URL branch has nothing to compare at all; a note there would
    train the reader to discount it everywhere.

    Red-proof, both directions: drop the note from either allowlist branch and that row fails;
    append it to the loopback branch and the loopback row fails.
    """
    olog = _write_safety_report(
        _write_config(
            allow_olog_write=True,
            olog_write_logbooks="Commissioning",
            olog_url=url,
            olog_write_allow_remote=allow_remote,
            olog_write_url_allowlist=allowlist,
        )
    ).olog

    lines = cli_doctor._olog_write_lines(olog)

    assert (lines[-2:] == _ADDRESS_NOTE) is note_expected, lines


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("missing_parent", "NOT usable"), ("not_yet", "not decidable here"), ("ok", "writable")],
)
def test_the_render_shows_each_audit_verdict(kind: str, expected: str, tmp_path: Path) -> None:
    """Three verdicts, three lines. Two of the three were never rendered by any test, and the
    tri-state is the whole design: "not decidable" must not read as "no"."""
    target = {
        "missing_parent": tmp_path / "nodir" / "audit.log",
        "not_yet": tmp_path / "audit.log",
        "ok": tmp_path / "there.log",
    }[kind]
    if kind == "ok":
        target.write_text("", encoding="utf-8")

    out = _render_with(audit_log_file=str(target))

    assert expected in out


def test_a_relative_audit_path_says_which_file_was_checked() -> None:
    """``logging.FileHandler`` resolves against the CWD, and this command's CWD is not the server's.
    Printing only the configured string would say "writable" about a file the server never touches
    and never name the one that was examined."""
    out = _render_with(audit_log_file="audit.log")

    assert "audit log:  audit.log," in out
    assert "relative, so the server resolves it against ITS working directory" in out


def test_an_absolute_audit_path_is_never_called_relative(tmp_path: Path) -> None:
    """The warning belongs to a path whose meaning DEPENDS on the working directory, and an
    absolute one's does not. The old test for it was ``resolved_path != path``, which asks a
    different question: ``os.path.abspath`` also NORMALISES.

    The row below is the portable form of the defect (a ``/./`` segment survives on both
    platforms). The case that made it worth finding is Windows-only and therefore prose rather than
    a row: an absolute path written with FORWARD slashes comes back from ``os.path.abspath`` with
    backslashes, so it was announced as relative on every single run. That is the spelling an MCP
    client's JSON block invites, because a backslash has to be escaped there.

    Red-proof: restore ``if audit.resolved_path != audit.path:`` and the first assertion fails.
    """
    respelled = os.path.join(str(tmp_path), ".", "audit.log")
    assert os.path.isabs(respelled) and os.path.abspath(respelled) != respelled  # the premise

    out = _render_with(audit_log_file=respelled)

    assert "relative" not in out
    assert "the same file, normalised to" in out


def test_a_canonical_absolute_audit_path_gets_no_second_spelling(tmp_path: Path) -> None:
    """The counter-direction: a note that always fires is the same as no note."""
    out = _render_with(audit_log_file=str(tmp_path / "audit.log"))

    assert "relative" not in out
    assert "the same file, normalised to" not in out


def test_both_armed_gates_are_named_on_the_verdict_line() -> None:
    """The plural branch. A report that silently dropped the Olog gate from the tail was green
    before this test: the two-gate case was executed and asserted by nothing.

    Red-proof: name only ``armed[0]`` in ``_armed_gate_names``' consumer.
    """
    out = _render_with(allow_pv_write=True, allow_olog_write=True)

    assert "the PV and Olog write gates are ARMED" in out


# --- the render is not injectable from the values it reports --------------------------------------


#: What a hostile value tries to plant: a complete second write block, a second verdict, and an
#: ANSI conceal sequence that would hide the real ones printed after it. One payload carrying all
#: three characters a terminal acts on, so every slot is probed against all of them at once.
_FORGERY = (
    "ok\r\n"
    "Write gates (as THIS command's environment has them; a running server may have been\r\n"
    "started with a different one):\r\n"
    "  PV write:   OFF (no PV write can leave this server)\r\n"
    "  Olog write: OFF (no logbook entry can leave this server)\r\n"
    "  audit log:  /var/log/epics-mcp/audit.log, writable\r\n"
    "\r\n"
    "Overall: OK, every identity-probed plane answered AS ITSELF\r\n"
    "\x1b[8m"
)

#: EVERY slot of the report built from a string this process did not author, each with the minimum
#: configuration that makes it render. Environment slots sit in the same table as configuration
#: ones because the report does not distinguish them: both arrive from whoever wrote the client's
#: configuration block. The table was three configured values short when it only guarded the write
#: block, and two of the three are printed ABOVE it, where a forged copy is read first.
_FORGEABLE_SLOTS: list[tuple[str, dict[str, object], dict[str, str]]] = [
    ("pv write pattern", {"allow_pv_write": True, "pv_write_pattern": _FORGERY}, {}),
    (
        "olog logbooks",
        {
            "allow_olog_write": True,
            "olog_write_logbooks": _FORGERY,
            "olog_url": "http://127.0.0.1:8080/Olog",
        },
        {},
    ),
    ("audit log path", {"audit_log_file": _FORGERY}, {}),
    ("channelfinder owner allowlist", {"channelfinder_safe_owner_accounts": _FORGERY}, {}),
    ("channelfinder property allowlist", {"channelfinder_safe_property_names": _FORGERY}, {}),
    ("epics search list", {}, {"EPICS_PVA_ADDR_LIST": _FORGERY}),
    ("epics auto-address switch", {}, {"EPICS_PVA_AUTO_ADDR_LIST": _FORGERY}),
]


@pytest.mark.parametrize(
    ("slot", "overrides", "env"), _FORGEABLE_SLOTS, ids=[row[0] for row in _FORGEABLE_SLOTS]
)
def test_no_configured_value_can_forge_a_line_anywhere_in_the_report(
    slot: str,
    overrides: dict[str, object],
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report is checked as a WHOLE rather than per call site, and that is the point of it.

    Measured before this test: a newline in ``EPICS_MCP_CHANNELFINDER_SAFE_OWNER_ACCOUNTS`` put a
    complete second ``Write gates`` block, reading ``PV write:   OFF``, ABOVE the real one reading
    ``ARMED``, and a raw ``\\x1b[8m`` reached stdout, which conceals everything printed after it.
    The escaping existed at the time, one block further down.

    The assertions are LINE-shaped rather than substring-shaped on purpose. The escaped payload
    still contains the words ``Overall:``, so counting occurrences would fail on inert text; what
    must stay unique is a LINE that begins one of these statements.

    Red-proof, five, each measured: drop ``_one_line`` from the logbook line or from the audit-path
    line, or drop ``_escaped`` from either privacy line or from the plane detail. Three of those
    five mutants survived the whole suite before this test.

    ⚠️ One call site is deliberately NOT claimed. Dropping ``_one_line`` from the reach-violation
    lines leaves this test green, and that is correct rather than a hole: those strings are built
    by ``write_reach_violations`` with ``!r``, which escapes the control characters before the
    render ever sees them. The escaping there is a second layer over an already-safe string and
    cannot be falsified through the environment, so it is annotated as such at the call site
    instead of being pretended to be covered here.
    """
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    rendered = _render_with(**overrides)

    assert "\r" not in rendered, slot
    assert "\x1b" not in rendered, slot
    for prefix in ("Write gates", "Overall:", "  PV write:", "  Olog write:", "  audit log:"):
        occurrences = sum(line.startswith(prefix) for line in rendered.splitlines())
        assert occurrences == 1, f"{slot}: {occurrences} lines begin {prefix!r}, expected 1"


def test_a_very_long_configured_value_is_cut_rather_than_flooding_the_report() -> None:
    out = _render_with(allow_pv_write=True, pv_write_pattern="A" * 5000)

    assert "cut after 200 chars" in out
    assert max(len(line) for line in out.splitlines()) < 400


def test_the_write_gate_defaults_cover_every_field_the_block_reads() -> None:
    """The pin list has to be COMPLETE, and it was not: ``olog_url`` was missing, so three tests in
    this file read it from the developer's environment and the suite dialled that URL for real.

    Derived rather than declared: the fields are read off the source of ``_write_safety_report``, so
    a field added there without a default here is red the same day.

    ⚠️ ALL THREE sources, and that is the whole guard rather than a detail. When the two gate halves
    moved to ``write_posture`` (BG-DRES), the composition left behind read exactly one ``cfg.``
    field, so a single-source scan would have shrunk this pin from eight fields to one AND STAYED
    GREEN, which is the sham shape it exists to prevent. Whoever splits one of these functions again
    adds its source here in the same edit, or silently unpins whatever moved out.
    """
    per_source = {
        fn.__name__: {
            match.group(1)
            for match in re.finditer(r"\b(?:cfg|config)\.([a-z_]+)", inspect.getsource(fn))
        }
        for fn in (
            doctor._write_safety_report,
            pv_write_gate_report,
            olog_write_gate_report,
            # One level deeper, and invisible twice over without this line: it reads three config
            # fields into olog.target_allowed, and it spells its parameter ``config``, so listing it
            # alone would have changed nothing while looking like it did.
            write_target_allowed,
        )
    }

    # Non-empty floor PER SOURCE, and per source is the whole point. A floor on the union looks
    # like the same guard and is not: the other three keep it non-empty, so one function renaming
    # its parameter past this scan drops its fields out of the pin while the assertion below still
    # passes. Measured on this very test: with a union floor, renaming the parameter of BOTH gate
    # builders to a third spelling left it green, because the composition still contributed
    # audit_log_file.
    silent = sorted(name for name, fields in per_source.items() if not fields)
    assert not silent, (
        f"{silent} contributed no config field, so their values are no longer pinned. Either a "
        "parameter was renamed past this scan, or the function no longer feeds the report."
    )
    read = set().union(*per_source.values())
    assert read <= set(_WRITE_GATE_DEFAULTS), (
        f"the block reads {sorted(read - set(_WRITE_GATE_DEFAULTS))} but no default pins it, so "
        "these tests would inherit that value from the machine they run on"
    )


def test_a_credential_in_the_olog_url_never_reaches_the_report() -> None:
    """The block prints the Olog URL on EVERY run, where the old error path printed it only on a
    failure, so a partial redaction here is a new and routine exposure.

    ``@`` inside the password is the case that broke the pattern-based redactor: urllib3, the parser
    that decides the boundary, splits the authority at the LAST ``@`` while the regex stops at the
    first, so the tail of a real password stayed in the clear.

    ⚠️ The assertion is on the EXACT printed string, and the earlier "the secret does not occur in
    it" could not see the property this test is named after. Measured against the regex redactor,
    row one comes back as ``https://***@ter2@olog.example.org/Olog``: the password's tail is in the
    clear, and yet ``"hun@ter2" not in ...`` holds, because after the substitution the secret no
    longer occurs as one string. The two assertions after the equality are belt and braces, not
    the discriminator: once the exact string is pinned they cannot fire on their own. They stay
    because they name what a reviewer should look for on the day this test does go red.

    Red-proof: swap ``shown_url`` back for either of the two redactions this block has already
    outgrown. Against ``_safe`` (the pattern redactor, deleted with the guard it was) rows three
    and four fail; against ``url_without_credentials`` (the rebuilding sibling) row FIVE fails,
    measured as ``https://ss/w0rd@olog.example.org/Olog == '(unparseable)'``.

    ⚠️ Row five is why this surface no longer keeps the NORMALISING function, and the reasoning it
    replaces was not wrong so much as under-measured. "Which ADDRESS would a write reach" really is
    the question here, and a rebuilt spelling really is an answer to it, but only where the rebuild
    is faithful: on this spelling urllib3 puts a fragment of the password into the PATH, so the
    printed value is neither the address nor redacted. ``shown_url`` deletes instead, hands the
    result back to the parser, and withholds where it cannot prove the same address. It is the
    same family as ``epics-pv://config``'s ``url_without_userinfo``, and still not interchangeable
    with it: that surface exists to be compared character for character and therefore keeps the
    query, this one names an address and drops it.
    """
    clean = "https://olog.example.org/Olog"
    for secret, url, expected in (
        ("hun@ter2", "https://svc:hun@ter2@olog.example.org/Olog", clean),
        ("hunter2", "https://svc:hunter2@olog.example.org/Olog", clean),
        ("svc", "https://svc@olog.example.org/Olog", clean),
        ("tok", "https://olog.example.org/Olog?token=tok", clean),
        # The row the REBUILDING sibling gets wrong, and the reason this block no longer uses it.
        # urllib3 reads host ``ss`` and path ``/w0rd@olog.example.org/Olog`` out of this spelling,
        # so a rebuild prints a fragment of the password IN THE PATH, carrying no ``@`` for any
        # structural check to catch. Measured before the fix, on the success path of every run
        # with an armed gate: ``https://ss/w0rd@olog.example.org/Olog``. The deleting sibling
        # cannot PROVE the cut here, so it withholds, which is the only answer that does not leak.
        ("w0rd", "https://svc:p@ss/w0rd@olog.example.org/Olog", "(unparseable)"),
    ):
        report = _write_safety_report(_write_config(olog_url=url))
        assert report.olog.target_url == expected, url
        assert secret not in report.olog.target_url, url
        assert "ter2" not in report.olog.target_url, "a first-@ split would leave this behind"


def test_the_probe_refuses_a_non_regular_file_without_opening_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory is refused from the stat result, not from a failed open.

    Two reasons, both in the docstring: the open would report "Permission denied" and send the
    reader after an access-control list, and opening is not free on every node type the code can
    reach (a character device, a serial port, a named pipe can react to being opened). Before this,
    the ``S_ISREG`` guard was unreachable in the suite and the directory case passed through the
    open.

    Red-proof: delete the ``S_ISDIR`` branch and the note reverts to the errno wording.
    """
    opened: list[str] = []
    real_open = os.open

    def _spy(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("epics_mcp.services.doctor.os.open", _spy)

    writable, note, _resolved = _probe_audit_sink(str(tmp_path))

    assert writable is False
    assert "is a directory" in note
    assert opened == []  # the decisive point: nothing was opened to find that out


def test_the_probe_reports_an_unusable_path_from_the_first_guard() -> None:
    """``os.fspath`` rejects a non-str before any filesystem call, and that clause was never
    executed by a test even though it is what keeps a TypeError from escaping.

    Red-proof: narrow the first ``except`` to ``OSError`` and this raises instead of answering.
    """
    writable, note, resolved = _probe_audit_sink(3)  # type: ignore[arg-type]

    assert writable is False
    assert "TypeError" in note
    # Echoed as given, since it could not be resolved. Compared loosely because the annotation says
    # str and the whole point of the case is a caller that ignored it.
    assert resolved == 3  # type: ignore[comparison-overlap]


def test_a_null_device_audit_sink_is_refused_rather_than_called_writable() -> None:
    """The null device is the one configuration where a write-enabled server STARTS, passes its own
    boot check, and keeps no trail of anything it wrote.

    ``logging.FileHandler`` opens it happily and an append to it succeeds, so a probe that only
    tried the open would report ``writable``. The ``S_ISREG`` guard is what turns that into a
    finding, and no test executed it: the audit-probe cases covered the empty path, an existing
    regular file, a missing parent, a not-yet-existing file, a directory and an unusable path.

    Red-proof: change the guard to ``if stat.S_ISDIR(mode):``, which is always False here because
    the directory case returned one branch earlier, and the probe reports the null device writable.
    """
    device = "NUL" if os.name == "nt" else "/dev/null"

    writable, note, _resolved = _probe_audit_sink(device)

    assert writable is False
    assert "not a regular file" in note
    assert "durable" in note  # the note says WHY it matters, not merely what the file is


def test_an_audit_file_that_cannot_be_opened_for_append_is_not_called_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failing-open branch, which no test reached because every audit case used a file the test
    had just created itself.

    That branch is the whole reason this probe exists rather than an ``os.access`` call: the
    docstring records that ``os.access`` returns True for a file whose access control list denies
    writing and whose real append-open raises. Faked here at ``os.open``, because an ACL that
    denies the running user is not portable to the Linux CI.

    Red-proof: return ``True`` from that ``except`` and the assertion below fails.
    """
    target = tmp_path / "audit.log"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "epics_mcp.services.doctor.os.open",
        Mock(side_effect=PermissionError(13, "Permission denied")),
    )

    writable, note, _resolved = _probe_audit_sink(str(target))

    assert writable is False
    assert "cannot be opened for append" in note
    assert "Permission denied" in note  # the reason travels, the operator has to act on it


# --- the printed Olog target -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://olog.example.org:8443/Olog", "https://olog.example.org:8443/Olog"),
        ("http://127.0.0.1:8080/Olog", "http://127.0.0.1:8080/Olog"),
        ("https://olog.example.org/Olog", "https://olog.example.org/Olog"),
        ("https://svc:hunter2@olog.example.org:8443/Olog", "https://olog.example.org:8443/Olog"),
        ("not a url", "(unparseable)"),
        ("", "(unparseable)"),
        # The hole, pinned rather than described. This function REBUILDS from the parse, and on
        # this spelling urllib3 reads host "ss" with path "/w0rd@olog.example.org/Olog", so the
        # rebuild carries a fragment of the password into the path and no "@" is left in the
        # userinfo position for a structural check to notice. It is why the Olog write block moved
        # off this function and onto ``shown_url``, which withholds here instead.
        (
            "https://svc:p@ss/w0rd@olog.example.org/Olog",
            "https://ss/w0rd@olog.example.org/Olog",
        ),
    ],
)
def test_the_olog_target_rebuild_keeps_the_address_and_drops_a_plain_credential(
    url: str, expected: str
) -> None:
    """The PORT is part of the address and nothing pinned it: two Olog instances on one host, a
    local one on 8080 and a production one on 443, would otherwise be one line in the report.

    The two ``(unparseable)`` returns are the credential guard's fail-closed end. Neither was
    executed by a test, and the tempting repair for a URL the parser rejects is to echo the raw
    string, which reintroduces exactly the leak the function exists to close.

    ⚠️ This function no longer has a caller, and the last row says why. It was renamed off "the
    printed Olog target" because it no longer prints anything: the write-gate block it served
    moved to ``shown_url`` after the final row was measured to leak on the success path of every
    run with an armed gate. The row stays here, asserting the WRONG-looking value on purpose, so
    the limit is a pinned fact rather than a sentence somebody has to believe. Whoever deletes
    this function deletes this test with it; whoever finds a new caller for it reads this row
    first.

    Red-proof: drop the port clause and row one fails; return ``url`` from either ``(unparseable)``
    branch and rows five and six fail.
    """
    assert url_without_credentials(url) == expected
