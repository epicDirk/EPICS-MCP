"""Offline tests for the vendored ESS Naming-Service client (mocked HTTP)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.naming_exceptions import (
    NamingServiceConnectionError,
    NamingServiceResponseError,
)


def _resp(payload: object, *, status: int = 200) -> Mock:
    """Build a fake ``requests`` response with the given JSON payload and HTTP status.

    A status >= 400 makes ``raise_for_status`` raise an ``HTTPError`` carrying this response (as
    real requests does), so the client can read ``exc.response.status_code`` to split 404 apart.
    """
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = payload
    if status >= 400:
        err = requests.exceptions.HTTPError(str(status))
        err.response = resp
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _client_with(monkeypatch: pytest.MonkeyPatch, response: Mock) -> NamingServiceClient:
    """A NamingServiceClient whose every GET returns *response* (no network)."""
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(client.session, "get", Mock(return_value=response))
    return client


@pytest.mark.parametrize("base", ["http://h/enotify-web", "http://h/enotify-web/"])
def test_base_url_normalised_so_urls_match_with_or_without_trailing_slash(base: str) -> None:
    """M10: base_url is rstripped like the other REST clients, so a URL configured
    with OR without a trailing slash yields identical, correct endpoints (no 404)."""
    client = NamingServiceClient(base_url=base)
    assert client.base_url == "http://h/enotify-web"
    assert client.parts_url == "http://h/enotify-web/rest/parts/mnemonic/"
    assert client.names_url == "http://h/enotify-web/rest/deviceNames/"


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
    """A genuine 404 on the deviceNames endpoint = the DEFINITIVE 'not registered'."""
    client = _client_with(monkeypatch, _resp({}, status=404))
    result = client.validate_name("NOPE:nope")
    assert result["registered"] is False
    assert result["status"] == ""
    assert "not registered" in result["message"]


def test_validate_name_service_error_propagates_not_false_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-2 / audit S5: a NON-404 failure (here HTTP 500) must NOT collapse into a definitive
    ``registered=False`` (a false name_typo). It PROPAGATES as NamingServiceResponseError so the
    caller withholds. Distinguishes 'name not registered' from 'service/URL error'."""
    client = _client_with(monkeypatch, _resp({}, status=500))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


def test_validate_name_transport_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-HTTP GET failure (here a read Timeout during the deviceNames GET, after a successful
    reachability probe) also propagates as a service error, never a false 'not registered'."""
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.Timeout("read timeout"))
    )
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


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


def test_check_connectivity_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(
        client.session, "head", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(NamingServiceConnectionError):
        client.check_connectivity()


def test_check_connectivity_wraps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """S8-5 contract guard: a requests.exceptions.Timeout must surface as
    NamingServiceConnectionError (a withheld transport signal). This is a forward guard — it holds
    today because RequestException ⊂ OSError, and it FAILS if the except arm is ever narrowed to
    only ConnectionError, re-opening the raw-escape/false-'not registered' hole."""
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(client.session, "head", Mock(side_effect=requests.exceptions.Timeout()))
    with pytest.raises(NamingServiceConnectionError):
        client.check_connectivity()


def test_check_connectivity_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # G4: the reachability probe must honor the configured timeout (default 5 s),
    # not a hardcoded 1 s that falsely reports a slow-but-reachable service down.
    client = NamingServiceClient(base_url="http://naming.example/", timeout=7.5)
    head = Mock(return_value=Mock())
    monkeypatch.setattr(client.session, "head", head)
    assert client.check_connectivity() is True
    head.assert_called_once_with(client.base_url, timeout=7.5)
