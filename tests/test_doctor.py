"""Offline tests for the read-only config self-check (services/doctor + cli_doctor), no network.

Every test is hermetic: the config is patched to a fresh EpicsConfig and each client class is
replaced by a fake, so the 'not live' suite makes no network call. Covers the 3-bucket classifier
(Plan-QA #1: a served non-2xx is api_error/reachable, not unreachable), the disabled/ok/failing
planes, the single-source privacy report, the live plane's no-default-egress posture (Plan-QA #4),
and the CLI exit-code convention (0 clean / 1 a plane hard-failed / 2 usage / 3 inconclusive: an
identity probe that FAILED, S12).
"""

from __future__ import annotations

import json
from typing import get_args
from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp import cli_doctor
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.doctor import (
    _FAILING_STATUSES,
    _INCONCLUSIVE_STATUSES,
    _NON_FAILING_STATUSES,
    DoctorReport,
    PlaneCheck,
    PlaneStatus,
    PrivacyReport,
    _classify_failure,
    _identify,
    _identify_alarm,
    _identify_archiver,
    _identify_naming,
    _identify_retrieval_plane,
    _privacy_report,
    _safe,
    run_doctor,
)
from epics_pv_mcp.services.olog_client import OlogClient


def _set_config(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> EpicsConfig:
    """Point doctor's config read at a fresh EpicsConfig with the given fields."""
    cfg = EpicsConfig(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr("epics_pv_mcp.services.doctor.get_config", lambda: cfg)
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


# --- _classify_failure (the 3-bucket core) ---


def test_classify_ssl_error_is_ca_error() -> None:
    exc = RuntimeError("x")
    exc.__cause__ = requests.exceptions.SSLError("bad cert")
    reachable, ca_ok, status, detail = _classify_failure(exc)
    assert (reachable, ca_ok, status) == (False, False, "ca_error")
    assert "CA_BUNDLE" in detail


def test_classify_served_non2xx_is_api_error_reachable() -> None:
    """Plan-QA #1: a served non-2xx is 'api_error' (reachable), NOT 'unreachable'."""
    http_err = requests.exceptions.HTTPError("404")
    http_err.response = Mock(status_code=404)
    exc = RuntimeError("x")
    exc.__cause__ = http_err
    reachable, ca_ok, status, detail = _classify_failure(exc)
    assert (reachable, ca_ok, status) == (True, True, "api_error")
    assert "404" in detail
    # the actionable payload: the mgmt/retrieval hint distinguishes api_error from unreachable.
    assert "mgmt" in detail
    assert "not retrieval" in detail


def test_classify_retry_error_is_api_error() -> None:
    """A retry-exhausted 502/503/504 (chained RetryError, no .response) is api_error (reachable),
    NOT unreachable — the host answered repeatedly with a 5xx."""
    exc = RuntimeError("x")
    exc.__cause__ = requests.exceptions.RetryError("too many 503 error responses")
    reachable, ca_ok, status, detail = _classify_failure(exc)
    assert (reachable, ca_ok, status) == (True, True, "api_error")
    assert "5xx" in detail


def test_classify_transport_failure_is_unreachable() -> None:
    exc = RuntimeError("x")
    exc.__cause__ = requests.exceptions.ConnectionError("refused")
    reachable, ca_ok, status, detail = _classify_failure(exc)
    assert (reachable, ca_ok, status) == (False, None, "unreachable")
    assert "could not reach" in detail


# --- run_doctor: disabled / reachable / failing planes ---


@pytest.fixture(autouse=True)
def _identity_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the identity probes for every run_doctor test — AUTOUSE, deliberately.

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
    # 17s resolving a fake hostname — passing, silently, over the network. A hermetic test that is
    # merely slow is how "no network" rots.
    monkeypatch.setattr("epics_pv_mcp.services.doctor.rest_get_json", lambda *_a, **_k: {})
    monkeypatch.setattr("epics_pv_mcp.services.doctor._identify", _identified)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify_alarm",
        lambda *_a, **_k: _identified("alarm"),
    )
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify_archiver",
        lambda *_a, **_k: _identified("archiver"),
    )
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify_naming",
        lambda *_a, **_k: _identified("naming"),
    )
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify_retrieval_plane",
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
        monkeypatch.setattr(f"epics_pv_mcp.services.doctor.{name}", boom)
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
    # Nothing was left unproven because nothing was probed at all — verification_complete is
    # VACUOUSLY true here, and identified_planes carries the machine-readable difference between
    # "all confirmed" and "nothing ran": it must be empty.
    assert report.verification_complete is True
    assert report.unverified_planes == []
    assert report.identified_planes == []


async def test_reachable_plane_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, channelfinder_url="http://cf:8080/ChannelFinder")
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    report = await run_doctor()
    cf = _plane(report, "channelfinder")
    assert (cf.status, cf.reachable, cf.ca_ok) == ("ok", True, True)
    assert report.ok is True


# --- the identity probe: the full result matrix, offline (S4) ---
#
# WHY THIS EXISTS: "ok" used to mean only "check_connectivity did not raise". check_connectivity is
# a HEAD and counts ANY HTTP response as reachable — so a ChannelFinder URL pointing at a DEAD
# container reported "✓ channelfinder ok", because a different service on that port answered 401
# (its blanket auth answers 401 for every path, so the status said nothing about CF at all).
# These drive the REAL _identify against a patched rest_get_json: no network, full matrix.


def _payload(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    """Make the identity probe's GET return *value* (no network)."""
    monkeypatch.setattr("epics_pv_mcp.services.doctor.rest_get_json", lambda *a, **k: value)


def _raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make the identity probe's GET fail with *exc* (no network)."""

    def _boom(*_a: object, **_k: object) -> object:
        raise exc

    monkeypatch.setattr("epics_pv_mcp.services.doctor.rest_get_json", _boom)


def test_identity_exact_name_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"name": "Olog Service", "version": "6.0.4"})
    check = _identify("olog", "http://olog.example/Olog", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)


def test_identity_of_a_different_known_service_is_unverified_with_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S14: a foreign service name is "cannot confirm", never a hard failure.

    The earlier ``wrong_service``+exit-1 verdict rested on "a misconfiguration that is
    unambiguous at any site" — refuted by measurement (2026-07-16): a path-based reverse
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
    assert check.status in _NON_FAILING_STATUSES  # honest doubt, exit 0
    # And the vocabulary itself is gone — a re-added dead Literal value (paired with its glyph)
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
    """No usable name → unverified. NEVER ok, and never a failure either — it is a "don't know"."""
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
    ``identity_probe_failed`` — NOT the honest ``unverified`` (that is for a 2xx answered-but-not-
    nameable). This is exactly where the 401 of the dead-container case lands: rest_get_json raises
    on a non-2xx BEFORE parsing, so an auth wall can never reach the name check — and it must no
    longer collapse to a silent exit 0. A TLS/transport failure DURING the identity GET is re-homed
    here too (the transport HEAD already proved reachability+CA to the same host).

    Red-proof: on the pre-fix code every case here was ``unverified`` (exit 0).
    """
    _raises(monkeypatch, exc)
    check = _identify("channelfinder", "http://cf.example/ChannelFinder", None, 5.0)
    assert (check.status, check.identified) == ("identity_probe_failed", False)
    assert check.status in _INCONCLUSIVE_STATUSES
    assert check.status not in _NON_FAILING_STATUSES  # NOT a silent all-clear


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
    assert check.status in _NON_FAILING_STATUSES  # honest, exit 0 — never a failed probe


def test_identity_unreadable_2xx_raw_valueerror_stays_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S12 min-version robustness (diff-review R1): on the ``requests>=2.25`` floor a bad-JSON 2xx
    raises the STDLIB ``json.JSONDecodeError`` — a ``ValueError`` but NOT a ``RequestException``, so
    ``rest_get_json`` does not wrap it and it arrives RAW (``__cause__`` is None). It must still be
    ``unverified`` (the service answered 2xx), so the discriminator checks the exception ITSELF.

    Red-proof: a discriminator that only inspects ``__cause__`` makes this identity_probe_failed.
    """
    raw = json.JSONDecodeError("Expecting value", "<html>login</html>", 0)  # __cause__ is None
    _raises(monkeypatch, raw)
    check = _identify("channelfinder", "http://cf.example/ChannelFinder", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)
    assert check.status in _NON_FAILING_STATUSES


def test_archiver_identity_requires_the_identity_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_connectivity accepts ANY parseable 2xx JSON — an empty {} passes it. The appliance's
    own 'identity' field is what makes it an Archiver rather than "something served JSON here"."""
    _payload(monkeypatch, {})
    assert _identify_archiver("http://arch.example:17665", None, 5.0).status == "unverified"

    _payload(monkeypatch, {"identity": "appliance0", "engineURL": "http://arch.example:17666"})
    check = _identify_archiver("http://arch.example:17665", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)
    assert "appliance0" in (check.detail or "")


def test_naming_identifies_via_its_swagger_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Naming Service DOES have an identity beacon — an earlier pass claimed it had none.

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
    confirm" — honest doubt, exit 0 (since S14 that is the ONLY verdict any unconfirmed
    identity can earn; the harder wrong_service verdict was refuted by measurement)."""
    _payload(monkeypatch, {"info": {"title": "Some other API"}})
    assert _identify_naming("http://naming.example", 5.0).status == "unverified"


def test_retrieval_identifies_via_getversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieval serves /retrieval/bpl — probing /mgmt/bpl there 404s and proves nothing, which is
    exactly how an earlier pass concluded (wrongly) that retrieval had no identity endpoint."""
    probe = _identify_retrieval_plane
    _payload(monkeypatch, {"version": "Archiver Appliance Version 2.2.1"})
    check = probe("http://arch.example:17668", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)

    # The release number must NOT be pinned — an upgrade is not a misconfiguration.
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

    ``_identify`` matches the service name EXACTLY and its docstring says why — a substring would
    let a service calling itself "Not Olog Service" pass. Three functions later the retrieval probe
    shipped a containment check anyway (`_ARCHIVER_PRODUCT in version`), fail-open: the reasoning
    was written down and then ignored within the same file. The match is anchored at the START
    *and at a word boundary* — a bare ``startswith`` closed only the left side ("Archiver
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
# a blind HEAD (check_connectivity), so it reports "reachable" even when ES is dead — and the search
# history tools would then fail while the doctor said "✓ ok". _identify_alarm reads elastic.status
# from the SAME body the name check already parses (no second request). The healthy sentinel is
# EXACTLY "Connected"; a dead ES yields a string starting "Failed to connect to elastic " (measured
# from the Phoebus source SearchController.info(); GET / returns HTTP 200 either way, so the failure
# is body-only and a HEAD can never see it).


def test_alarm_elastic_down_is_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable + identified, but ES is down → a hard failure, not a silent ok.

    Red-proof: the pre-change alarm path used the shared name-only ``_identify``, which returns
    ``ok`` for this exact body — the blind-HEAD lie this change closes.
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
    know"), never ``backend_down`` — even when ``elastic.status`` says the backend is down. We do
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
    unverified (exit 0), reintroducing the S12 silent-exit-0 regression on the alarm plane — this
    test then goes red.
    """
    _raises(monkeypatch, exc)
    check = _identify_alarm("http://alarm.example", None, 5.0)
    assert (check.status, check.identified) == ("identity_probe_failed", False)
    assert check.status in _INCONCLUSIVE_STATUSES
    assert check.status not in _NON_FAILING_STATUSES


def test_alarm_unreadable_2xx_body_stays_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx alarm beacon whose body is not JSON is honest ``unverified`` (answered, not nameable),
    NOT ``identity_probe_failed`` — the same ValueError carve-out as the shared _identify path, now
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
    stubs BOTH _identify and _identify_alarm to the same identified=True value — so reverting the
    wiring line (_check_alarm._id → _identify("alarm", ...)) is otherwise invisible to every test.
    Here the REAL _identify_alarm and a real rest_get_json body are restored over the autouse stubs;
    with the wiring reverted, _check_alarm would call the STILL-STUBBED _identify → status 'ok' →
    this fails.

    Red-proof (mutation): point _check_alarm._id back at _identify and this test goes red (status
    'ok', report.ok True) — the exact pre-MA-2b(e) blind-HEAD behaviour the change removes.
    """
    _set_config(monkeypatch, alarm_url="http://alarm.example")
    monkeypatch.setattr("epics_pv_mcp.services.doctor.AlarmClient", _OkClient)
    # Restore the REAL probe + a GET / body over the autouse stubs so the real chain runs.
    monkeypatch.setattr("epics_pv_mcp.services.doctor._identify_alarm", _identify_alarm)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor.rest_get_json",
        lambda *_a, **_k: {
            "name": "Alarm logging Service",
            "elastic": {"status": "Failed to connect to elastic boom"},
        },
    )
    report = await run_doctor()
    alarm = _plane(report, "alarm")
    assert (alarm.status, alarm.identified) == ("backend_down", True)
    assert report.ok is False  # backend_down ∈ _FAILING_STATUSES → exit 1


def test_unknown_status_fails_closed() -> None:
    """The allowlist is the point: a new or mistyped status must FAIL, not slip through as exit 0.

    With the previous failure DENYLIST, a typo like "wrong-service" was simply absent from it and
    therefore counted as healthy — fail-open, in the one tool whose job is to catch bad config.
    """
    assert "wrong-service" not in _NON_FAILING_STATUSES  # the typo'd twin of a former status
    assert {"ok", "disabled", "info", "unverified"} == _NON_FAILING_STATUSES


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
    """The WIRING guard — the identity logic being correct is worthless if nobody calls it.

    Measured with a mutant: deleting the identity argument from the plane gatherers (i.e. exactly
    the pre-S4 state) left the whole gate chain green — 47/48 tests, ruff, mypy — while the doctor
    went back to reporting "✓ channelfinder ok" for a dead container, now under the even bolder
    "every configured plane answered AS ITSELF". Only this assertion notices.
    """
    _set_config(monkeypatch, **{url_field: "http://service.example/x"})
    monkeypatch.setattr(f"epics_pv_mcp.services.doctor.{client_name}", _OkClient)
    report = await run_doctor()
    checked = _plane(report, plane)
    assert checked.identified is True, (
        f"{plane}: reachable but never identity-probed — a transport probe alone is what let a "
        "dead container report ok"
    )


async def test_retrieval_falls_back_to_the_archiver_url_like_the_client_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-JVM appliance leaves EPICS_MCP_ARCHIVER_RETRIEVAL_URL empty and serves retrieval on
    the archiver port — ArchiverClient resolves `retrieval_url or base_url` (archiver_client.py) and
    get_pv_history queries it. Reporting that plane as "disabled" would be the same false all-clear
    this check exists to remove, only wearing a more reassuring word.
    """
    _set_config(monkeypatch, archiver_url="http://arch.example:17665")  # retrieval URL empty
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ArchiverClient", _OkClient)
    monkeypatch.setattr("epics_pv_mcp.services.doctor.rest_get_json", lambda *a, **k: {})
    report = await run_doctor()
    retrieval = _plane(report, "archiver_retrieval")
    assert retrieval.status != "disabled", "retrieval is live via fallback, not reported as off"
    assert retrieval.configured is True


async def test_retrieval_url_without_archiver_url_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S18(b): EPICS_MCP_ARCHIVER_RETRIEVAL_URL set while EPICS_MCP_ARCHIVER_URL is empty.

    Every archiver tool gates on EPICS_MCP_ARCHIVER_URL (tools/archiver.py, checkers.py), so that
    retrieval URL is never used by anything — yet the fallback fix reported the STRONGEST all-clear
    the tool knows for it (measured against a live retrieval endpoint: ``ok=True,
    verification_complete=True`` while every archiver tool was disabled). A fix against false-green
    that produced false-green. The pair is dead config and must FAIL, loudly, without probing:
    an ``ok`` next to a config error would only muddy what the operator has to change.

    Red-proof: on the pre-fix code this test FAILS — the plane is probed instead of refused
    (live it reported ``ok``; under this test's boom-mock the probe errors, either way the
    status is not ``config_error``).
    """
    _set_config(monkeypatch, archiver_retrieval_url="http://arch.example:17668")
    boom = Mock(side_effect=AssertionError("dead config must not be probed"))
    monkeypatch.setattr("epics_pv_mcp.services.doctor.rest_get_json", boom)
    report = await run_doctor()
    retrieval = _plane(report, "archiver_retrieval")
    assert retrieval.status == "config_error"
    assert retrieval.configured is True
    assert "EPICS_MCP_ARCHIVER_URL" in (retrieval.detail or "")
    assert retrieval.status not in _NON_FAILING_STATUSES  # it must drive exit 1
    assert report.ok is False
    # The archiver plane itself stays honestly disabled — the ERROR is the inconsistent pair.
    assert _plane(report, "archiver").status == "disabled"


async def test_retrieval_plane_is_actually_identity_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring guard for the SIXTH plane — the matrix above covers five and left retrieval out.

    Not a matrix row, because retrieval has no client class (its transport probe calls
    rest_get_json directly) and a lone retrieval URL is a ``config_error`` since S18(b) — so the
    wired path to guard is the fallback one (archiver URL set). Mutant-proof: removing the
    identity argument from the ``_run_probe`` call in ``_check_retrieval_plane`` leaves every
    other test green; only this assertion notices (``identified`` stays None).
    """
    _set_config(monkeypatch, archiver_url="http://arch.example:17665")
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ArchiverClient", _OkClient)
    report = await run_doctor()
    retrieval = _plane(report, "archiver_retrieval")
    assert retrieval.identified is True, (
        "archiver_retrieval: reachable but never identity-probed — the same gap the matrix "
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
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify",
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
    True (it is NOT a hard failure), so the exit code cannot be derived from ``ok`` alone — it lands
    in ``inconclusive_identity_planes`` (the field a machine reader must check ALONGSIDE
    ``unverified_planes``), and ``verification_complete`` is False.

    Red-proof (the FLAW-B trap): a naive ``ok = all(status in _NON_FAILING_STATUSES)`` (leaving the
    old line) flips ``ok`` to False here — which would collapse exit 3 into exit 1. Pins the union.
    """
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify",
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
    assert report.ok is True  # NOT a hard failure — pins the ok-union
    assert report.verification_complete is False
    assert report.inconclusive_identity_planes == ["channelfinder"]
    assert report.unverified_planes == []


async def test_ca_error_plane_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, channelfinder_url="http://cf")
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor.ChannelFinderClient",
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
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ArchiverClient", _cause_client(http_err))
    report = await run_doctor()
    arch = _plane(report, "archiver")
    assert arch.status == "api_error"
    assert arch.reachable is True  # NOT falsely unreachable
    assert report.ok is False


async def test_unreachable_plane_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, alarm_url="http://alarm:8081")
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor.AlarmClient",
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
    assert report.privacy.olog_freetext_withheld is True


async def test_privacy_report_reflects_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, channelfinder_safe_owner_accounts="svc_a,svc_b")
    report = await run_doctor()
    assert report.privacy.cf_safe_owner_accounts == ["svc_a", "svc_b"]


@pytest.mark.parametrize(
    ("olog_url", "declared", "withheld"),
    [
        # Both conditions must hold before free text is surfaced — mirror of OlogClient._redact.
        ("http://localhost:8080/Olog", True, False),  # declared local sandbox → full
        ("http://127.0.0.1:8080/Olog", True, False),
        ("http://localhost:8080/Olog", False, True),  # loopback but NOT declared → withheld
        ("https://olog.example.org/Olog", True, True),  # declared but remote → withheld
        ("https://olog.example.org/Olog", False, True),
        ("http://olog:8080/Olog", True, True),
        ("http://127.0.0.1@evil.example.org/Olog", True, True),  # userinfo spoof → not loopback
        ("", True, True),  # plane disabled: no client, no read → "withheld" is honest
    ],
)
def test_privacy_report_olog_freetext_matches_the_client(
    olog_url: str, declared: bool, withheld: bool
) -> None:
    """The doctor must REPORT the effective Olog posture, never assert a static guarantee.

    This is the tool an operator runs to CHECK the privacy posture — a hardcoded "always withheld"
    would make it lie in exactly the configuration where the answer differs, and its tests would
    stay green. Tested against ``_privacy_report`` directly: it is the unit that carries the
    decision, and ``run_doctor`` would probe the URL over the network.
    """
    cfg = EpicsConfig(olog_url=olog_url, olog_assume_test_data=declared)
    assert _privacy_report(cfg).olog_freetext_withheld is withheld
    # The report must not diverge from what the client actually does.
    if olog_url:
        client = OlogClient(olog_url, assume_test_data=declared)
        assert client._redact is withheld


# --- live plane (Plan-QA #4: no default egress) ---


async def test_live_plane_info_only_makes_no_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --probe-pv the live plane is INFO-only and pv_get is NEVER called."""
    _set_config(monkeypatch)
    pv_get = Mock(side_effect=AssertionError("pv_get must not be called without --probe-pv"))
    monkeypatch.setattr("epics_pv_mcp.services.doctor.pv_get", pv_get)
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

    monkeypatch.setattr("epics_pv_mcp.services.doctor.pv_get", _ok)
    report = await run_doctor(probe_pv="SIM:PS-01:Cur-RB")
    live = _plane(report, "live")
    assert live.status == "ok"
    assert live.reachable is True
    assert report.ok is True


async def test_live_plane_probe_disconnected_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch)

    async def _down(pv_name: str, timeout: float) -> dict[str, object]:
        raise EpicsError("timeout", error_code="PV_TIMEOUT")

    monkeypatch.setattr("epics_pv_mcp.services.doctor.pv_get", _down)
    report = await run_doctor(probe_pv="SIM:PS-01:Cur-RB")
    live = _plane(report, "live")
    assert live.status == "disconnected"
    assert live.reachable is False
    assert report.ok is False


async def test_live_plane_probe_generic_exception_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-EpicsError from the probe (internal failure) is still caught → disconnected, keeping
    doctor total; the exception's type name flows into the detail."""
    _set_config(monkeypatch)

    async def _boom(pv_name: str, timeout: float) -> dict[str, object]:
        raise ValueError("boom")

    monkeypatch.setattr("epics_pv_mcp.services.doctor.pv_get", _boom)
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
    """BG14 red proof 1: `EPICS_PVA_NAME_SERVERS` alone is a search path — TCP unicast to the
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
    searches into the local subnets — autoAddrList defaults to true (pvxs pvxs/client.h).
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
    true — every search list unset AND the auto-addr search explicitly disabled."""
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
    """All set search vars appear in the posture — none is masked by another (pre-fix the
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
    """BG14-QA: pvxs' parse_bool accepts ONLY case-insensitive "NO" or exactly "0" —
    untrimmed (PickOne passes the raw getenv value); anything else is a parse error that
    keeps the DEFAULT, and the default is broadcast (pvxs src/config.cpp, pvxs/client.h).
    Claiming isolation for a spelling the real parser rejects would be exactly the false
    claim BG14 removed — "false" is the likeliest real-world case, since this repo's own
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
        ("nope", True),  # strstr: any substring "no" disables — pinned so the semantics stay honest
        ("No", False),  # mixed case matches neither strstr("no") nor strstr("NO")
        ("false", False),
        ("0", False),
    ],
)
async def test_live_posture_ca_off_is_substring_case_sensitive(
    monkeypatch: pytest.MonkeyPatch, value: str, isolated: bool
) -> None:
    """libca disables the auto search only when the value CONTAINS "no" or "NO" as a
    case-sensitive substring (epics-base modules/ca/src/client/iocinf.cpp) — "false",
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


def test_cli_failing_plane_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_config(monkeypatch, alarm_url="http://alarm:8081")
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor.AlarmClient",
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
        "epics_pv_mcp.services.doctor.AlarmClient",
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
    assert "(empty — all owners redacted)" in out  # the empty-owner fallback line
    assert "Olog free-text:     withheld" in out  # the VALUE, not just the label (see below)


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
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify",
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
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor.AlarmClient",
        _cause_client(requests.exceptions.ConnectionError("refused")),  # alarm HARD-fails
    )
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify",
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
    privacy = PrivacyReport(
        cf_safe_owner_accounts=[], cf_safe_property_names=[], olog_freetext_withheld=True
    )

    def _mk(
        *, ok: bool, inconclusive: list[str], complete: bool, identified: list[str]
    ) -> DoctorReport:
        return DoctorReport(
            planes=[],
            privacy=privacy,
            ok=ok,
            verification_complete=complete,
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


def test_cli_verdict_with_nothing_configured_claims_no_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S18(c): with not a single REST plane configured, the verdict used to read "every configured
    plane answered AS ITSELF" — vacuously true over the empty set, and it READS as a confirmation
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
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    code = cli_doctor.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "AS ITSELF (1 verified)" in out


async def test_identified_planes_is_the_positive_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """S18(c), machine side: ``verification_complete`` alone cannot tell "all confirmed" from
    "nothing ran" — it is vacuously True on an empty config, and three docs tell scripts to read
    it. ``identified_planes`` is the positive counterpart to ``unverified_planes``: a script that
    wants POSITIVE confirmation asserts it is non-empty (found by the adversarial review of the
    first S18 fix, which had closed the vacuous truth only on the human-rendered verdict line).
    """
    _set_config(monkeypatch, channelfinder_url="http://cf.example/ChannelFinder")
    monkeypatch.setattr("epics_pv_mcp.services.doctor.ChannelFinderClient", _OkClient)
    report = await run_doctor()
    assert report.identified_planes == ["channelfinder"]
    assert report.verification_complete is True


def test_every_plane_status_has_a_render_mark() -> None:
    """Every PlaneStatus value must carry its own glyph in the CLI render.

    ``_render`` falls back to "?" for an unknown status — which is the ``unverified`` mark, so a
    status missing from ``_STATUS_MARK`` would silently wear the honest-doubt glyph. This guard
    makes adding a status without a mark a red test instead (it caught exactly that while
    ``config_error`` was being added).
    """
    assert set(get_args(PlaneStatus)) == set(cli_doctor._STATUS_MARK)


def test_cli_reports_full_olog_freetext_for_a_declared_sandbox(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human render must say FULL for a declared local sandbox — the doctor cannot lie.

    Asserting the VALUE, not just the label: the other CLI test only checked that the line existed,
    which is why a hardcoded "always withheld" could have survived both doctor tests untouched.
    """
    _set_config(monkeypatch, olog_url="http://localhost:8080/Olog", olog_assume_test_data=True)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor.OlogClient",
        _cause_client(requests.exceptions.ConnectionError("refused")),
    )
    cli_doctor.main([])
    out = capsys.readouterr().out
    assert "Olog free-text:     FULL (declared local test data — ESS-spec pending)" in out


def test_cli_bad_arg_exits_two() -> None:
    """argparse rejects an unknown flag with SystemExit(2) — the usage-error convention."""
    with pytest.raises(SystemExit) as excinfo:
        cli_doctor.main(["--nonsense"])
    assert excinfo.value.code == 2


def test_cli_epicserror_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine internal EpicsError during the run maps to exit 2 (not a crash)."""

    async def _boom(**kwargs: object) -> DoctorReport:
        raise EpicsError("internal", error_code="INTERNAL")

    monkeypatch.setattr("epics_pv_mcp.cli_doctor.run_doctor", _boom)
    assert cli_doctor.main([]) == 2
