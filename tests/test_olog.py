"""Offline tests for the Phoebus Olog client + tools (no network), DS-PRIVACY focus."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.olog_safety import OlogWriteGate
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError, OlogError
from epics_pv_mcp.services.redact import FREETEXT_WITHHELD
from epics_pv_mcp.tools.olog import (
    _get_log_entry,
    _list_logbooks,
    _list_tags,
    _search_logbook,
)

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
    # is_redirect is set explicitly: on a bare Mock every attribute is truthy, so a normal response
    # double would trip the client's redirect guard.
    resp = Mock(is_redirect=False)
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- client: the redaction switch (ESS-spec pending, decisions 2026-07-15) ---
#
# Against a real server the projection below is unchanged. Entries leave WHOLE only when BOTH hold:
# a loopback URL AND the operator's explicit `assume_test_data` declaration. Neither suffices alone
# — a port-forward serves production on localhost without the URL changing (so the URL cannot prove
# the data is synthetic), and a flag alone would not catch "pointed at the facility and forgot".


def _sandbox(url: str = "http://localhost:8080/Olog") -> OlogClient:
    """A client for a DECLARED local sandbox — the only configuration that sees whole entries."""
    return OlogClient(url, assume_test_data=True)


def test_declared_loopback_client_returns_the_whole_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared loopback sandbox surfaces free text, owner, source and properties."""
    client = _sandbox()
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["title"] == "Vacuum trip found by c.person"
    assert entry["description"] == "d.person restarted the IOC; ask e.person"
    assert entry["owner"] == "a.person"
    assert entry["source"] == "written by b.person"
    assert entry["properties"] == _RAW_ENTRY["properties"]


def test_loopback_without_the_declaration_still_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE tunnel guard: a loopback URL alone must NOT un-redact.

    `ssh -L 8080:olog-prod:8080` makes a production logbook answer on localhost with the URL
    unchanged — so the address can never be the sufficient condition. Only a person can declare the
    data synthetic. Default (no declaration) = redact.
    """
    client = OlogClient("http://localhost:8080/Olog", assume_test_data=False)
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["title"] == FREETEXT_WITHHELD
    assert "owner" not in entry


def test_sandbox_refuses_to_follow_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared sandbox must not follow a redirect — it could land on a real server un-redacted.

    Demonstrated live (QA 2026-07-15): a loopback server answering 302 -> a non-loopback address
    made the client return that server's entries WHOLE, because the mode was decided from the
    configured URL while requests silently followed the hop. Olog's REST API has no legitimate
    redirect, so the client refuses to follow one at all — a loud error beats a silent leak.
    """
    client = _sandbox()
    captured: dict[str, object] = {}

    def _get(url: str, **kwargs: object) -> Mock:
        captured.update(kwargs)
        resp = Mock()
        resp.is_redirect = True
        resp.status_code = 302
        resp.headers = {"location": "http://10.0.0.5/Olog/logs/42"}
        resp.raise_for_status.return_value = None  # a 302 is NOT an HTTP error
        resp.json.return_value = _RAW_ENTRY
        return resp

    monkeypatch.setattr(client.session, "get", _get)
    with pytest.raises(OlogError) as excinfo:
        client.get_log_entry("42")
    assert captured["allow_redirects"] is False  # never followed in the first place
    assert "redirect" in str(excinfo.value).lower()


def test_declaration_without_loopback_still_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: declaring test data does NOT un-redact a remote server.

    Catches "pointed at the facility and forgot the flag was on" — loopback stays necessary.
    """
    client = OlogClient("https://olog.example.org/Olog", assume_test_data=True)
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["title"] == FREETEXT_WITHHELD


def test_client_reads_the_declaration_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit argument the client takes the declaration from the config."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.olog_client.get_config",
        lambda: EpicsConfig(olog_url="http://localhost:8080/Olog", olog_assume_test_data=True),
    )
    assert OlogClient("http://localhost:8080/Olog")._redact is False
    monkeypatch.setattr(
        "epics_pv_mcp.services.olog_client.get_config",
        lambda: EpicsConfig(olog_url="http://localhost:8080/Olog"),
    )
    assert OlogClient("http://localhost:8080/Olog")._redact is True  # default false = redact


def test_declared_sandbox_keeps_the_derived_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full mode only ADDS fields — it never changes the shape the caller already relies on.

    Regression guard: returning the server dict verbatim would drop ``attachment_count`` (it is
    SYNTHESISED, not an Olog field) and flip ``logbooks``/``tags`` from list[str] to list[dict].
    ``dict[str, object]`` is wide enough that mypy would not catch either.
    """
    client = _sandbox("http://127.0.0.1:8080/Olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["logbooks"] == ["Operations"]  # name-only list[str], as in redacted mode
    assert entry["tags"] == ["vacuum"]
    assert entry["attachment_count"] == 1
    # Every key the redacted mode promises is still present.
    redacted_keys = {
        "id",
        "createdDate",
        "modifyDate",
        "level",
        "state",
        "title",
        "description",
        "logbooks",
        "tags",
        "attachment_count",
    }
    assert redacted_keys <= entry.keys()


def test_full_mode_search_still_truncates_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full mode must not disturb ``[:size]`` truncation or the non-dict filter.

    ``capped``/``total_matches`` are computed on the RAW list before projection; the truncation and
    the ``isinstance(e, dict)`` filter live INSIDE the comprehension and decide ``total``. The junk
    sits INSIDE the slice on purpose — placed after it, truncation would remove it before the filter
    ever ran and this test would pin nothing. It matters because the full mode's ``dict(entry)``
    raises on a non-dict, so the filter is what keeps a junk element from crashing the search.
    """
    client = _sandbox()
    payload = [_RAW_ENTRY, "not-a-dict", _RAW_ENTRY, _RAW_ENTRY]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    entries, capped, _ = client.search_logbook(text="vacuum", size=2)
    assert len(entries) == 1  # slice keeps 2, the filter drops the junk one
    assert capped is True  # capped is computed on the RAW list, before either
    assert all(isinstance(e, dict) for e in entries)


def test_allowlisted_remote_write_target_is_still_read_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE core regression: a URL the WRITE gate would allow remotely must still READ redacted.

    ``OlogWriteGate._url_write_allowed`` returns True for an allowlisted remote host with
    ``allow_remote`` — reusing it as the read predicate would surface a PRODUCTION logbook in the
    clear. Only ``is_loopback_url`` may drive the redaction. This pins that for good.
    """
    remote = "https://olog.example.org/Olog"
    gate = OlogWriteGate(
        EpicsConfig(
            olog_url=remote,
            allow_olog_write=True,
            olog_write_logbooks="Operations",
            olog_write_url_allowlist=remote,
            olog_write_allow_remote=True,
        )
    )
    gate.check_write_allowed(["Operations"])  # the gate says: writing here is permitted…

    # assume_test_data=True isolates the URL as the deciding condition — without it the client would
    # redact regardless and this would not test the write-gate/read-predicate separation at all.
    client = OlogClient(remote, assume_test_data=True)
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["title"] == FREETEXT_WITHHELD  # …and reading it is STILL redacted.
    assert "owner" not in entry
    for name in _PERSON_NAMES:
        assert name not in str(entry)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1@evil.example.org/Olog",  # userinfo spoof: host is evil.example.org
        "http://[::1]./Olog",  # malformed → unparseable
        "garbage",
    ],
)
def test_spoofed_or_unparseable_url_redacts(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-safe: anything not provably loopback is redacted — even WITH the declaration set.

    ``assume_test_data=True`` on purpose: it isolates the loopback check as the deciding condition,
    so this stays a real test of the URL logic. Without it the client would redact anyway and the
    case would pass even if ``is_loopback_url`` were broken.
    """
    client = OlogClient(url, assume_test_data=True)
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(_RAW_ENTRY)))
    entry = client.get_log_entry("42")
    assert entry is not None
    assert entry["title"] == FREETEXT_WITHHELD


# --- client: DS-PRIVACY projection (the core guarantee) ---


def test_project_log_entry_withholds_all_person_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redacted entry keeps only technical fields + logbook/tag NAMES; every person name (owner,
    source, free-text title/description, logbook owner, attachment filename, property) is gone."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([_RAW_ENTRY])))
    entries, capped, total_matches = client.search_logbook(text="vacuum")
    assert capped is False
    assert total_matches is None  # bare-list has no hitCount → honest None (not fabricated)
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

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["url"] = url
        captured["params"] = params
        # return size+1 entries so capped is True
        return _resp([dict(_RAW_ENTRY, id=i) for i in range(3)])

    monkeypatch.setattr(client.session, "get", _get)
    entries, capped, _total = client.search_logbook(text="trip", logbooks="Operations", size=2)
    assert capped is True
    assert len(entries) == 2  # truncated to size
    assert captured["url"] == "http://olog/logs/search"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["size"] == "3"  # size + 1
    assert params["desc"] == "trip"
    assert params["logbooks"] == "Operations"
    assert params["sort"] == "down"  # default newest-first ordering is always sent


def test_search_logbook_wrapped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Olog {logs, hitCount} wrapper is handled; hitCount → total_matches (the true total)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp({"logs": [_RAW_ENTRY], "hitCount": 7}))
    )
    entries, _capped, total_matches = client.search_logbook()
    assert len(entries) == 1
    assert entries[0]["id"] == 42
    assert total_matches == 7  # authoritative total across all pages, NOT len(entries)


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
    resp = Mock(is_redirect=False)  # explicit: a bare Mock's attributes are all truthy
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

        def search_logbook(
            self, **kwargs: object
        ) -> tuple[list[dict[str, object]], bool, int | None]:
            # a REAL client would redact; this fake returns an already-redacted entry to assert the
            # tool surfaces it faithfully (redaction is pinned by the client tests above). The
            # 3-tuple mirrors the real client (entries, capped, total_matches).
            return [{"id": 7, "title": FREETEXT_WITHHELD, "logbooks": ["Ops"]}], False, 1

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
    result = await _search_logbook(text="c.person")
    assert result["enabled"] is True
    assert result["total"] == 1
    assert result["total_matches"] == 1
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


# --- check_connectivity (E2 doctor probe) ---


def test_check_connectivity_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP response to a HEAD on the root = reachable (transport + CA proven)."""
    client = OlogClient("http://olog:8080/Olog")
    monkeypatch.setattr(client.session, "head", Mock(return_value=Mock()))
    assert client.check_connectivity() is True


def test_check_connectivity_raises_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OlogClient("http://olog:8080/Olog")
    monkeypatch.setattr(
        client.session, "head", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(OlogConnectionError):
        client.check_connectivity()


# --- search_logbook pagination + sort (offset -> Olog wire 'from', sort) ---


def test_search_logbook_offset_and_sort_wire_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """offset maps to the Olog wire key 'from' (only when >0); sort passes through unchanged."""
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp({"logs": [], "hitCount": 0})

    monkeypatch.setattr(client.session, "get", _get)
    client.search_logbook(text="x", offset=25, sort="up")
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["from"] == "25"
    assert params["sort"] == "up"


def test_search_logbook_offset_zero_omits_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """offset=0 (default) sends NO 'from' key; sort defaults to 'down' (newest first)."""
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp({"logs": [], "hitCount": 0})

    monkeypatch.setattr(client.session, "get", _get)
    client.search_logbook(text="x")
    params = captured["params"]
    assert isinstance(params, dict)
    assert "from" not in params
    assert params["sort"] == "down"


# --- list_logbooks / list_tags: client name-only projection (owner dropped) ---


def test_list_logbooks_names_only_drops_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /logbooks items are {name,owner,state}; only the NAME survives (owner=PII gone)."""
    client = OlogClient("http://olog")
    raw = [
        {"name": "Operations", "owner": "a.person", "state": "Active"},
        {"name": "Controls", "owner": "b.person", "state": "Active"},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    names = client.list_logbooks()
    assert names == ["Operations", "Controls"]
    blob = str(names)
    assert "a.person" not in blob and "b.person" not in blob  # no logbook owner leaks


def test_list_tags_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tags returns {name,state} per item (no owner field); the names survive."""
    client = OlogClient("http://olog")
    raw = [{"name": "vacuum", "state": "Active"}, {"name": "rf", "state": "Active"}]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    assert client.list_tags() == ["vacuum", "rf"]


# --- list_logbooks / list_tags: config gate + enabled path ---


@pytest.mark.asyncio
async def test_list_logbooks_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(olog_url="")
    )

    def _boom(*args: object, **kwargs: object) -> OlogClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
    result = await _list_logbooks()
    assert result["enabled"] is False
    assert result["logbooks"] == []


@pytest.mark.asyncio
async def test_list_tags_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(olog_url="")
    )

    def _boom(*args: object, **kwargs: object) -> OlogClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
    result = await _list_tags()
    assert result["enabled"] is False
    assert result["tags"] == []


@pytest.mark.asyncio
async def test_list_logbooks_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enabled tool surfaces the client's name-only list (owner-drop pinned separately)."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_logbooks(self) -> list[str]:
            return ["Operations", "Controls"]

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
    result = await _list_logbooks()
    assert result["enabled"] is True
    assert result["logbooks"] == ["Operations", "Controls"]


@pytest.mark.asyncio
async def test_list_tags_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_tags(self) -> list[str]:
            return ["vacuum", "rf"]

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
    result = await _list_tags()
    assert result["enabled"] is True
    assert result["tags"] == ["vacuum", "rf"]
