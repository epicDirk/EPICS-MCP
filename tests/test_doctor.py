"""Offline tests for the read-only config self-check (services/doctor + cli_doctor), no network.

Every test is hermetic: the config is patched to a fresh EpicsConfig and each client class is
replaced by a fake, so the 'not live' suite makes no network call. Covers the 3-bucket classifier
(Plan-QA #1: a served non-2xx is api_error/reachable, not unreachable), the disabled/ok/failing
planes, the single-source privacy report, the live plane's no-default-egress posture (Plan-QA #4),
and the CLI exit-code convention (0 healthy / 1 a plane failed / 2 usage).
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp import cli_doctor
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.doctor import (
    _NON_FAILING_STATUSES,
    DoctorReport,
    PlaneCheck,
    _classify_failure,
    _identify,
    _identify_archiver,
    _privacy_report,
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

    monkeypatch.setattr("epics_pv_mcp.services.doctor._identify", _identified)
    monkeypatch.setattr(
        "epics_pv_mcp.services.doctor._identify_archiver",
        lambda *_a, **_k: _identified("archiver"),
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
    # Nothing was left unproven because nothing was probed at all.
    assert report.verification_complete is True
    assert report.unverified_planes == []


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


def test_identity_of_a_different_known_service_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE case that motivated all of this — but sharpened: here the wrong service ANSWERS.

    A ChannelFinder URL served by Olog is a misconfiguration that is unambiguous at any site, so
    unlike `unverified` it is a hard failure.
    """
    _payload(monkeypatch, {"name": "Olog Service"})
    check = _identify("channelfinder", "http://olog.example/Olog", None, 5.0)
    assert (check.status, check.identified) == ("wrong_service", False)
    assert "points at the olog service" in (check.detail or "")
    assert check.status not in _NON_FAILING_STATUSES  # it must drive exit 1


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
def test_identity_failed_probe_is_unverified(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Anything the probe cannot read → unverified, never ok.

    This is also where the 401 of the dead-container case lands: rest_get_json raises on a non-2xx
    BEFORE parsing, so an auth wall can never reach the name check.
    """
    _raises(monkeypatch, exc)
    check = _identify("channelfinder", "http://cf.example/ChannelFinder", None, 5.0)
    assert (check.status, check.identified) == ("unverified", False)


def test_archiver_identity_requires_the_identity_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_connectivity accepts ANY parseable 2xx JSON — an empty {} passes it. The appliance's
    own 'identity' field is what makes it an Archiver rather than "something served JSON here"."""
    _payload(monkeypatch, {})
    assert _identify_archiver("http://arch.example:17665", None, 5.0).status == "unverified"

    _payload(monkeypatch, {"identity": "appliance0", "engineURL": "http://arch.example:17666"})
    check = _identify_archiver("http://arch.example:17665", None, 5.0)
    assert (check.status, check.identified) == ("ok", True)
    assert "appliance0" in (check.detail or "")


def test_unknown_status_fails_closed() -> None:
    """The allowlist is the point: a new or mistyped status must FAIL, not slip through as exit 0.

    With the previous failure DENYLIST, a typo like "wrong-service" was simply absent from it and
    therefore counted as healthy — fail-open, in the one tool whose job is to catch bad config.
    """
    assert "wrong-service" not in _NON_FAILING_STATUSES  # the typo'd twin of a real status
    assert {"ok", "disabled", "info", "unverified"} == _NON_FAILING_STATUSES


async def test_unverified_plane_does_not_fail_but_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest middle: reachable, identity unproven → exit 0, but NOT "healthy".

    That a healthy service answers its info endpoint anonymously is measured at ONE site (n=1), so
    turning "cannot prove it" into a hard failure would be the very overclaim we keep finding.
    """
    _set_config(monkeypatch, naming_url="http://naming.example")
    monkeypatch.setattr("epics_pv_mcp.services.doctor.NamingServiceClient", _OkClient)
    report = await run_doctor()
    naming = _plane(report, "naming")
    assert (naming.status, naming.identified, naming.reachable) == ("unverified", False, True)
    assert "no info endpoint" in (naming.detail or "")
    assert report.ok is True  # honest, not a failure
    assert report.verification_complete is False  # ...but NOT confirmed
    assert report.unverified_planes == ["naming"]


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
