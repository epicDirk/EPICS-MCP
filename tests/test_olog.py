"""Offline tests for the Phoebus Olog client + tools (no network), DS-PRIVACY focus."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError
from epics_pv_mcp.olog_safety import OlogWriteGate
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.olog_exceptions import (
    OlogConnectionError,
    OlogError,
    OlogResponseError,
)
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


def _err_resp(status: int) -> Mock:
    """A response double that fails with *status*, carrying it where http_status() reads it."""
    http_error = requests.exceptions.HTTPError(str(status))
    http_error.response = Mock(status_code=status)
    resp = Mock(is_redirect=False)  # explicit: a bare Mock's attributes are all truthy
    resp.raise_for_status.side_effect = http_error
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


def test_full_mode_search_still_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full mode must not disturb ``[:size]`` truncation.

    ``capped``/``total_matches`` are computed on the RAW list before projection. (This test used
    to also pin a silent non-dict FILTER inside the comprehension — S11 removed that filter: a
    junk element in the page now raises instead of silently shrinking the result, see
    ``test_search_entry_without_identity_raises``.)
    """
    client = _sandbox()
    payload = [dict(_RAW_ENTRY, id=i) for i in range(3)]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    entries, capped, _ = client.search_logbook(text="vacuum", size=2)
    assert len(entries) == 2  # slice keeps size
    assert capped is True  # capped is computed on the RAW list, before projection
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


@pytest.mark.parametrize(
    "payload",
    [{}, ["x"], "nope", 123, {"unexpected": "shape"}, {"id": None}],
    ids=["empty-dict", "list", "string", "number", "dict-without-id", "null-id"],
)
def test_get_log_entry_unreadable_2xx_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: a 200 whose body is not a log entry must RAISE — never a definitive answer.

    Replaces the former ``…empty_body_is_none`` pin, which cemented the defect: ``{}`` collapsed
    to ``None`` (indistinguishable from the definitive 404 "not found"), and an unrelated
    non-empty dict was PROJECTED as a fabricated entry (auditor probe ``{"unexpected": "shape"}``
    → a plausible log entry that never existed). The measured entry record always carries ``id``.
    """
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(OlogResponseError):
        client.get_log_entry("999")


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
    # S11 (Zusatzfläche 3): a DISABLED plane was the lone `found: False` among four
    # None-on-disabled siblings (archived/configured/registered/get_archive_info's found) —
    # a definitive "this entry does not exist" from a plane that was never asked. None = not
    # checked; False stays reserved for the definitive 404.
    assert result["found"] is None
    assert "note" in result


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


# --- search_logbook: the ERROR CLASS at the tool boundary ---
#
# Three outcomes, three classes. Reporting a bad argument or a served rejection as
# EPICS_CONNECTION_FAILED ("cannot reach Olog") sends the reader after the wrong problem.


def _search_client_raising(exc: BaseException) -> type:
    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search_logbook(self, **kwargs: object) -> tuple[list[dict[str, object]], bool, int]:
            raise exc

    return _Fake


@pytest.mark.asyncio
async def test_search_bad_time_is_not_a_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable time window is a bad ARGUMENT — nothing was ever sent, so 'cannot reach Olog'
    would be a lie. Pins that TimeWindowFormatError is not swept up by the OlogError branch."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.OlogClient",
        _search_client_raising(TimeWindowFormatError("start='1 year': use days or weeks")),
    )
    with pytest.raises(EpicsError) as excinfo:
        await _search_logbook(start="1 year")
    assert excinfo.value.error_code == "INVALID_TIME_WINDOW"
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.asyncio
async def test_search_served_error_is_not_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server ANSWERED and said no — that is not an outage."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    http_error = requests.exceptions.HTTPError("401")
    http_error.response = Mock(status_code=401)
    served = OlogResponseError("Olog rejected the search (HTTP 401)")
    served.__cause__ = http_error
    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _search_client_raising(served))
    with pytest.raises(EpicsError) as excinfo:
        await _search_logbook(text="x")
    assert excinfo.value.error_code == "OLOG_HTTP_401"
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.asyncio
async def test_search_connection_failure_still_maps_to_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-test: the split must not over-reach — a real outage stays a connection error."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.OlogClient",
        _search_client_raising(OlogConnectionError("no route to host")),
    )
    with pytest.raises(EpicsConnectionError) as excinfo:
        await _search_logbook(text="x")
    assert excinfo.value.error_code == "EPICS_CONNECTION_FAILED"


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


# --- search_logbook: the time window on the wire (live-established contract) ---
#
# Olog cannot parse ISO-8601 (its vendored TimestampFormats is a stripped copy with no ISO support)
# and does NOT say so: an unparseable value degrades to a zero offset from *now*, collapsing the
# window to [now, now+us] -> HTTP 200 with an empty result that reads as "nothing matched".
# Measured live, 2026-07-15. These tests pin the normalization that prevents it; they are wire-level
# because that silent drop happens on the SERVER, where no mock can see it.


def test_search_logbook_iso_window_sent_as_wall_clock_with_tz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline regression: an ISO window must reach Olog as its space-separated wall clock.

    Sent verbatim (the pre-fix behaviour), this exact window returned 0 of 9 entries live.
    """
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp({"logs": [], "hitCount": 0})

    monkeypatch.setattr(client.session, "get", _get)
    client.search_logbook(start="2026-01-01T00:00:00Z", end="2027-01-01T00:00:00Z")
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["start"] == "2026-01-01 00:00:00.000"
    assert params["end"] == "2027-01-01 00:00:00.000"
    assert "T" not in str(params["start"])
    # Without an explicit tz Olog reads the wall clock in the SERVER's zone — silently offset
    # against any Olog not running UTC, which no UTC-sandbox test would ever reveal.
    assert params["tz"] == "UTC"


def test_search_logbook_relative_window_not_rewritten_and_sends_no_tz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative amounts are the server's to resolve: passed verbatim, and tz would be a no-op."""
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp({"logs": [], "hitCount": 0})

    monkeypatch.setattr(client.session, "get", _get)
    client.search_logbook(start="7 days")
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["start"] == "7 days"
    assert "tz" not in params


def test_search_logbook_mixed_window_still_sends_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    """One absolute value is enough to need a zone for it."""
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp({"logs": [], "hitCount": 0})

    monkeypatch.setattr(client.session, "get", _get)
    client.search_logbook(start="7 days", end="2027-01-01T00:00:00Z")
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["start"] == "7 days"
    assert params["end"] == "2027-01-01 00:00:00.000"
    assert params["tz"] == "UTC"


def test_search_logbook_without_window_sends_no_time_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards against always-sending tz: no window means no time params at all."""
    client = OlogClient("http://olog")
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp({"logs": [], "hitCount": 0})

    monkeypatch.setattr(client.session, "get", _get)
    client.search_logbook(text="x")
    params = captured["params"]
    assert isinstance(params, dict)
    assert "start" not in params
    assert "end" not in params
    assert "tz" not in params


def test_search_logbook_bad_time_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value Olog cannot read is refused BEFORE any I/O — never sent and silently misread."""
    client = OlogClient("http://olog")

    def _fail(*_a: object, **_k: object) -> Mock:
        raise AssertionError("a request was made with an unusable time value")

    monkeypatch.setattr(client.session, "get", _fail)
    with pytest.raises(TimeWindowFormatError):
        client.search_logbook(start="1 year")


def test_search_logbook_start_after_end_rejected_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both values absolute -> we can compare them ourselves, deterministically.

    Left to the server this is a 400, which our ANONYMOUS read path only ever sees as a 401
    ('unauthorized') — actively misleading for what is a swapped window.
    """
    client = OlogClient("http://olog")

    def _fail(*_a: object, **_k: object) -> Mock:
        raise AssertionError("a request was made with an inverted window")

    monkeypatch.setattr(client.session, "get", _fail)
    with pytest.raises(TimeWindowFormatError, match="after"):
        client.search_logbook(start="2027-01-01T00:00:00Z", end="2026-01-01T00:00:00Z")


def test_search_logbook_401_message_explains_anonymous_error_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured: Olog turns EVERY server-side 400 into a 401 for an anonymous caller (its error
    dispatch requires auth). Our read path IS anonymous, so 401 — not 400 — is the reachable
    branch, and blaming credentials would send the user hunting the wrong problem."""
    client = OlogClient("http://olog")

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        return _err_resp(401)

    monkeypatch.setattr(client.session, "get", _get)
    with pytest.raises(OlogResponseError, match="rejected"):
        client.search_logbook(text="x")


def test_search_logbook_400_message_names_the_time_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment with read credentials sees Olog's real 400 — it must name the likely cause."""
    client = OlogClient("http://olog")

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        return _err_resp(400)

    monkeypatch.setattr(client.session, "get", _get)
    with pytest.raises(OlogResponseError, match="time window"):
        client.search_logbook(text="x")


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


# --- client: strict response schema (S11) — unreadable 2xx is NEVER a definitive answer ---
#
# Auditor probes (QA 2026-07-16 §8.2/B1): syntactically valid 2xx JSON of the wrong shape used to
# collapse into plausible definitive answers — search -> ([], False, None) ("no hits"), list
# endpoints -> [] ("there are none"). The measured payload shapes (local Olog 6.0.4, live): search
# is {hitCount:int, logs:[entry…]} (bare list = older-version variant, stays valid), every entry
# carries `id`, every /logbooks//tags item carries `name`.


@pytest.mark.parametrize(
    "payload",
    [{}, "nope", 123, {"unexpected": "shape"}, {"logs": "not-a-list"}],
    ids=["empty-dict", "string", "number", "unrelated-dict", "logs-not-a-list"],
)
def test_search_unreadable_2xx_payload_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: an unreadable 2xx search payload must RAISE — it used to read as ``([], False, None)``,
    indistinguishable from a genuinely empty search (auditor probe OLOG_SEARCH_BAD_2XX)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(OlogResponseError):
        client.search_logbook(text="x")


@pytest.mark.parametrize(
    "payload",
    [[123], [{"title": "no id"}], {"logs": ["junk"], "hitCount": 1}],
    ids=["non-dict-entry", "entry-without-id", "junk-inside-wrapper"],
)
def test_search_entry_without_identity_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: every entry of the page must be a dict carrying the measured anchor ``id`` — junk
    entries were silently DROPPED before (a fabricated, smaller result)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(OlogResponseError):
        client.search_logbook(text="x")


def test_search_unreadable_hitcount_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: a PRESENT-but-unreadable ``hitCount`` raises; only an ABSENT count is the honest
    ``total_matches=None`` (the no-count server variant)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp({"logs": [], "hitCount": "many"}))
    )
    with pytest.raises(OlogResponseError):
        client.search_logbook(text="x")


def test_search_empty_results_stay_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: both MEASURED empty shapes stay a real empty result, not an error —
    strictness must not flag a genuinely empty search (the S14 false-red lesson)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    assert client.search_logbook(text="x") == ([], False, None)
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp({"logs": [], "hitCount": 0}))
    )
    assert client.search_logbook(text="x") == ([], False, 0)


@pytest.mark.parametrize(
    "payload",
    [{}, "nope", [{"owner": "x"}], ["Operations"], [{"name": 7}]],
    ids=["dict", "string", "item-without-name", "string-item", "non-str-name"],
)
def test_list_logbooks_unreadable_2xx_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: the top-level /logbooks listing IS the answer — an unreadable payload or item must
    RAISE. It used to collapse to ``[]`` ("there are no logbooks") or silently drop items (a
    fabricated "this logbook does not exist" for anyone validating a name against the list)."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(OlogResponseError):
        client.list_logbooks()


@pytest.mark.parametrize(
    "payload",
    [{}, "nope", [{"state": "Active"}], ["vacuum"]],
    ids=["dict", "string", "item-without-name", "string-item"],
)
def test_list_tags_unreadable_2xx_raises(payload: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: same strictness for /tags (auditor probe OLOG_LIST_TAGS_BAD_2XX -> [])."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(OlogResponseError):
        client.list_tags()


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
