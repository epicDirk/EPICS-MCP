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


# --- client: strict response schema (S11) — unreadable 2xx is NEVER a definitive answer ---
#
# Measured (ESS Naming, live 2026-07-16): GET /rest/deviceNames/{name} with Accept:
# application/json answers a dict that ALWAYS carries a string `status` (plus name/uuid/…);
# WITHOUT the Accept header the service answers XML (content-type application/xml) — so the
# client must ask for JSON explicitly. A nonexistent name answers HTTP 204 (No Content), NOT
# the 404 the old contract assumed (S16a).


def test_session_asks_for_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Premise pin (S11, measured live 2026-07-16): the real ESS Naming service serves **XML**
    to a plain ``Accept: */*`` GET — only ``Accept: application/json`` yields the JSON record
    this client parses. The shared ``build_retrying_session`` sets that header today; this pins
    it, because swapping the session builder would silently collapse EVERY live lookup into
    withheld (``resp.json()`` fails on XML). Mutant-red: without the header the requests default
    is ``*/*``."""
    client = NamingServiceClient(base_url="http://naming.example/")
    assert client.session.headers.get("Accept") == "application/json"


def test_validate_name_payload_without_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: a 2xx dict WITHOUT the measured anchor ``status`` must RAISE (→ callers withhold) —
    it used to become the definitive ``registered=False`` with status ''."""
    client = _client_with(monkeypatch, _resp({"unexpected": "shape"}))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


@pytest.mark.parametrize("status", [123, ""], ids=["non-str", "empty-str"])
def test_validate_name_unreadable_status_raises(
    status: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11 (plan-review finding A2 + diff-review): a non-string ``status`` used to be
    str()-minted ('123'), and an EMPTY string read as an unknown status — both became the
    definitive ``registered=False``. Neither is a measured server value; junk raises instead."""
    client = _client_with(monkeypatch, _resp({"status": status}))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


@pytest.mark.parametrize("payload", [["x"], "nope", 123], ids=["list", "string", "number"])
def test_validate_name_non_dict_payload_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: a non-dict 2xx payload used to escape as an uncaught AttributeError (crashing
    crossplane_check); it must be the plane's own ResponseError so every caller withholds."""
    client = _client_with(monkeypatch, _resp(payload))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


def test_validate_name_204_is_definitively_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S16(a), measured live (ESS Naming 2026-07-16): a nonexistent name answers HTTP 204
    (No Content) — NOT 404. Before this, the empty body failed ``resp.json()`` and the lookup
    withheld (honest but needlessly vague); the measured 204 is the service's definitive
    "not registered" and maps to it. The 404 branch stays (a second definitive signal)."""
    resp = _resp(None, status=204)
    resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
    client = _client_with(monkeypatch, resp)
    result = client.validate_name("ZZZ-FAKE99:Ctrl-X-99")
    assert result["registered"] is False
    assert "not registered" in result["message"]


def test_get_parts_non_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: a non-list 2xx parts payload was returned as-is (annotation-only typing) and crashed
    later — the shape guard makes it the plane's own ResponseError. (The lenient
    ``_approved_part`` semantics on top are S13's business, untouched here.)"""
    client = _client_with(monkeypatch, _resp({"unexpected": "shape"}))
    with pytest.raises(NamingServiceResponseError):
        client._get_parts("SYSX")


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
