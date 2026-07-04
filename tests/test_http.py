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
