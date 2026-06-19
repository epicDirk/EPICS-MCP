"""Offline tests for the vendored ESS Naming-Service client (mocked HTTP)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.services.naming_client import NamingServiceClient


def _resp(payload: object, *, ok: bool = True) -> Mock:
    """Build a fake ``requests`` response with the given JSON payload."""
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")
    return resp


def _client_with(monkeypatch: pytest.MonkeyPatch, response: Mock) -> NamingServiceClient:
    """A NamingServiceClient whose every GET returns *response* (no network)."""
    client = NamingServiceClient()
    monkeypatch.setattr(client.session, "get", Mock(return_value=response))
    return client


def test_validate_name_active(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(monkeypatch, _resp({"status": "ACTIVE"}))
    result = client.validate_name("DEV-TEST01:Ctrl-EVR-01")
    assert result["registered"] is True
    assert result["status"] == "ACTIVE"


def test_validate_name_obsolete_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(monkeypatch, _resp({"status": "OBSOLETE"}))
    result = client.validate_name("DEV-TEST01:Ctrl-EVR-99")
    assert result["registered"] is False
    assert result["status"] == "OBSOLETE"


def test_validate_name_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(monkeypatch, _resp({}, ok=False))
    result = client.validate_name("NOPE:nope")
    assert result["registered"] is False
    assert result["status"] == ""
    assert "not registered" in result["message"]


def test_validate_system_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(
        monkeypatch,
        _resp([{"status": "Approved", "type": "System Structure", "level": "1"}]),
    )
    assert client.validate_system("SYSX") is True


def test_validate_system_unapproved(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(
        monkeypatch,
        _resp([{"status": "Pending", "type": "System Structure", "level": "1"}]),
    )
    assert client.validate_system("SYSX") is False


def test_validate_discipline_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(
        monkeypatch,
        _resp([{"status": "Approved", "type": "Device Structure", "level": "1"}]),
    )
    assert client.validate_discipline("Ctrl") is True
