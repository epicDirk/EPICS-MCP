"""Offline tests for the Phoebus Olog client + tools (no network), DS-PRIVACY focus."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError, OlogError
from epics_pv_mcp.services.redact import FREETEXT_WITHHELD
from epics_pv_mcp.tools.olog import _get_log_entry, _search_logbook

# A raw Olog entry that names people in EVERY person-bearing place: the owner key, the source, the
# title AND description free text, a logbook owner, an attachment filename, and a property value.
_RAW_ENTRY = {
    "id": 42,
    "createdDate": 1717200000000,
    "modifyDate": 1717200500000,
    "owner": "a.person",
    "source": "written by b.person",
    "level": "Info",
    "state": "Active",
    "title": "Vacuum trip found by c.person",
    "description": "d.person restarted the IOC; ask e.person",
    "logbooks": [{"name": "Operations", "owner": "f.person"}],
    "tags": [{"name": "vacuum", "state": "Active"}],
    "attachments": [{"id": 1, "filename": "photo-by-g.person.png"}],
    "properties": [{"name": "x", "attributes": [{"name": "author", "value": "h.person"}]}],
}
_PERSON_NAMES = [f"{c}.person" for c in "abcdefgh"]


def _resp(payload: object, *, ok: bool = True) -> Mock:
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- client: DS-PRIVACY projection (the core guarantee) ---


def test_project_log_entry_withholds_all_person_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redacted entry keeps only technical fields + logbook/tag NAMES; every person name (owner,
    source, free-text title/description, logbook owner, attachment filename, property) is gone."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([_RAW_ENTRY])))
    entries, capped = client.search_logbook(text="vacuum")
    assert capped is False
    assert entries == [
        {
            "id": 42,
            "createdDate": 1717200000000,
            "modifyDate": 1717200500000,
            "level": "Info",
            "state": "Active",
            "title": FREETEXT_WITHHELD,
            "description": FREETEXT_WITHHELD,
            "logbooks": ["Operations"],
            "tags": ["vacuum"],
            "attachment_count": 1,
        }
    ]
    blob = str(entries)
    for name in _PERSON_NAMES:
        assert name not in blob  # NO person name leaks, from any field


def test_search_logbook_capped_and_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """size+1 is requested to detect capping honestly; the search params are forwarded."""
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured["url"] = url
        captured["params"] = params
        # return size+1 entries so capped is True
        return _resp([dict(_RAW_ENTRY, id=i) for i in range(3)])

    monkeypatch.setattr(client.session, "get", _get)
    entries, capped = client.search_logbook(text="trip", logbooks="Operations", size=2)
    assert capped is True
    assert len(entries) == 2  # truncated to size
    assert captured["url"] == "http://olog/logs/search"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["size"] == "3"  # size + 1
    assert params["desc"] == "trip"
    assert params["logbooks"] == "Operations"


def test_search_logbook_wrapped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Olog version that wraps the hits in {logs: [...]} is handled like a bare list."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp({"logs": [_RAW_ENTRY], "hitCount": 1}))
    )
    entries, _capped = client.search_logbook()
    assert len(entries) == 1
    assert entries[0]["id"] == 42


def test_get_log_entry_found_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["title"] == FREETEXT_WITHHELD
    assert entry["logbooks"] == ["Operations"]
    assert "a.person" not in str(entry)


def test_get_log_entry_404_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/deleted id makes the Olog answer HTTP 404 -> found:false (None), NOT a raised
    error (the archiver getPVTypeInfo 404 lesson) — else 'does this exist?' == a real outage."""
    client = OlogClient("http://olog")
    http_error = requests.exceptions.HTTPError("404")
    http_error.response = Mock(status_code=404)
    resp = Mock()
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    assert client.get_log_entry("999") is None


def test_get_log_entry_non_404_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-404 failure (5xx / unreachable) must PROPAGATE — a could-not-read is never reported
    as 'not found' (the inverse of the 404 case)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({}, ok=False)))
    with pytest.raises(OlogError):
        client.get_log_entry("999")


def test_get_log_entry_empty_body_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with an empty/non-dict body also collapses to None (defensive, never a crash)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({})))
    assert client.get_log_entry("999") is None


def test_search_logbook_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OlogClient("http://olog")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(OlogConnectionError):
        client.search_logbook()


# --- tools: config gate + enabled path ---


@pytest.mark.asyncio
async def test_search_logbook_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(olog_url="")
    )

    def _boom(*args: object, **kwargs: object) -> OlogClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
    result = await _search_logbook(text="x")
    assert result["enabled"] is False
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_get_log_entry_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(olog_url="")
    )

    def _boom(*args: object, **kwargs: object) -> OlogClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
    result = await _get_log_entry("1")
    assert result["enabled"] is False
    assert result["found"] is False


@pytest.mark.asyncio
async def test_search_logbook_tool_enabled_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LAYERING contract: the tool routes through the redacting client — a person named in the
    free-text title/description of a raw entry never reaches the tool result."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search_logbook(self, **kwargs: object) -> tuple[list[dict[str, object]], bool]:
            # a REAL client would redact; this fake returns an already-redacted entry to assert the
            # tool surfaces it faithfully (the redaction itself is pinned by the client tests above)
            return [{"id": 7, "title": FREETEXT_WITHHELD, "logbooks": ["Ops"]}], False

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
    result = await _search_logbook(text="c.person")
    assert result["enabled"] is True
    assert result["total"] == 1
    entries = result["entries"]
    assert isinstance(entries, list)
    assert entries[0]["title"] == FREETEXT_WITHHELD


@pytest.mark.asyncio
async def test_get_log_entry_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_log_entry(self, log_id: str) -> dict[str, object] | None:
            return {"id": 7, "title": FREETEXT_WITHHELD}

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
    result = await _get_log_entry("7")
    assert result["enabled"] is True
    assert result["found"] is True
    entry = result["entry"]
    assert isinstance(entry, dict)
    assert entry["id"] == 7
