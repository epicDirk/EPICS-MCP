"""Tests for the shared REST substrate (services/_http) and the REST exception roots (C3).

Covers the M3/M12/L-Logger deduplication: one retry policy, one GET-and-translate, one root
exception hierarchy, and the single debug line that wakes the previously-dead REST logger.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services._http import build_retrying_session, rest_get_json
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverError,
    ArchiverResponseError,
)
from epics_pv_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


def _resp(payload: object, *, ok: bool = True) -> Mock:
    """A fake requests response returning *payload*; ok=False makes raise_for_status raise HTTP."""
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- build_retrying_session ---


def test_session_mounts_the_single_3_retry_policy() -> None:
    """The shared session carries the 3-retry/502-503-504 policy on both http and https (M3)."""
    session = build_retrying_session()
    for scheme in ("http://x", "https://x"):
        adapter = session.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        retries = adapter.max_retries
        assert isinstance(retries, Retry)
        assert retries.total == 3
        assert set(retries.status_forcelist or ()) == {502, 503, 504}
    assert session.headers["accept"] == "application/json"
    assert "authorization" not in session.headers


def test_session_forwards_optional_auth_header() -> None:
    session = build_retrying_session(auth_header="Bearer tok")
    assert session.headers["authorization"] == "Bearer tok"


# --- TLS verify resolution at the single chokepoint (DS-1) ---


def test_verify_kwarg_true_keeps_default_and_trust_env() -> None:
    """An explicit ``verify=True`` mirrors the plain default: verify on, trust_env untouched so the
    zero-code REQUESTS_CA_BUNDLE env path still works."""
    session = build_retrying_session(verify=True)
    assert session.verify is True
    assert session.trust_env is True


def test_verify_kwarg_ca_path_sets_verify_and_pins_trust_env() -> None:
    """A CA-bundle path is applied AND trust_env is pinned off so a REQUESTS_CA_BUNDLE env var
    cannot override the explicit bundle via requests' per-request environment merge."""
    session = build_retrying_session(verify="ess-root-ca.pem")
    assert session.verify == "ess-root-ca.pem"
    assert session.trust_env is False


def test_verify_kwarg_false_disables_and_pins_trust_env() -> None:
    """tls_verify=False must actually disable verification — pinning trust_env off is what makes the
    escape hatch real even when REQUESTS_CA_BUNDLE is set in the environment."""
    session = build_retrying_session(verify=False)
    assert session.verify is False
    assert session.trust_env is False


def test_verify_resolves_ca_bundle_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no kwarg the chokepoint reads ITS OWN get_config
    (epics_pv_mcp.services._http.get_config, NOT checkers.get_config) → ca_bundle path wins and
    trust_env is pinned off."""
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    session = build_retrying_session()
    assert session.verify == "ca.pem"
    assert session.trust_env is False


def test_verify_resolves_tls_verify_false_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="", tls_verify=False),
    )
    session = build_retrying_session()
    assert session.verify is False
    assert session.trust_env is False


def test_verify_default_config_verifies_and_keeps_trust_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plain default (ca_bundle empty, tls_verify True) verifies against certifi and leaves
    trust_env on — the zero-code REQUESTS_CA_BUNDLE path stays available."""
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="", tls_verify=True),
    )
    session = build_retrying_session()
    assert session.verify is True
    assert session.trust_env is True


# --- rest_exceptions root ---


def test_per_service_errors_derive_from_the_shared_roots() -> None:
    """Each per-service error is a RestClientError; conn/resp map to the matching root (C3)."""
    assert isinstance(ArchiverConnectionError(""), RestClientError)
    assert isinstance(ArchiverConnectionError(""), RestConnectionError)
    assert isinstance(ArchiverResponseError(""), RestResponseError)
    assert isinstance(ArchiverResponseError(""), RestClientError)
    # The per-service base still catches only its own subclasses (no behaviour regression).
    assert isinstance(ArchiverConnectionError(""), ArchiverError)


# --- rest_get_json ---


def test_rest_get_json_returns_parsed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_retrying_session()
    monkeypatch.setattr(session, "get", Mock(return_value=_resp([{"ok": 1}])))
    data = rest_get_json(
        session,
        "http://x",
        None,
        1.0,
        conn_exc=ArchiverConnectionError,
        resp_exc=ArchiverResponseError,
    )
    assert data == [{"ok": 1}]


def test_rest_get_json_connection_error_logs_once_and_raises_conn_exc(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ConnectionError → conn_exc, and EXACTLY ONE debug line is emitted (dead logger woke)."""
    session = build_retrying_session()
    monkeypatch.setattr(
        session, "get", Mock(side_effect=requests.exceptions.ConnectionError("down"))
    )
    with (
        caplog.at_level(logging.DEBUG, logger="epics_pv_mcp.services._http"),
        pytest.raises(ArchiverConnectionError),
    ):
        rest_get_json(
            session,
            "http://x",
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )
    debug_lines = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug_lines) == 1


def test_rest_get_json_http_error_raises_resp_exc(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_retrying_session()
    monkeypatch.setattr(session, "get", Mock(return_value=_resp(None, ok=False)))
    with pytest.raises(ArchiverResponseError):
        rest_get_json(
            session,
            "http://x",
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )
