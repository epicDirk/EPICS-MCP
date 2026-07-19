"""Offline tests for the Phoebus Olog client + tools (no network), DS-PRIVACY focus."""

from typing import Any, ClassVar
from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError
from epics_pv_mcp.olog_safety import OlogWriteGate
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.olog_client import OlogClient, split_level_values
from epics_pv_mcp.services.olog_exceptions import (
    OlogConnectionError,
    OlogError,
    OlogFilterValueError,
    OlogResponseError,
)
from epics_pv_mcp.services.redact import FREETEXT_WITHHELD
from epics_pv_mcp.tools.olog import (
    _get_log_entry,
    _list_log_levels,
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


# --- list_log_levels (OA2): strict listing + an UNAMBIGUOUS default, never a guessed one ---
#
# Levels are the logbook's triage axis and are site-configurable, so the server is the only source
# of the valid values. The names reuse the S11-strict `_named_list`; the default is reported only
# when the server states it unambiguously, because a guessed default would misdescribe what a
# create without an explicit level actually writes.


def test_list_log_levels_names_and_unambiguous_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured payload shape: a list of {name, defaultLevel} with exactly one flagged."""
    client = OlogClient("http://olog")
    payload = [
        {"name": "Urgent", "defaultLevel": False},
        {"name": "Info", "defaultLevel": True},
        {"name": "Problem", "defaultLevel": False},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    names, default, note = client.list_log_levels()
    assert names == ["Urgent", "Info", "Problem"]
    assert default == "Info"
    assert note is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ([{"name": "A", "defaultLevel": True}, {"name": "B", "defaultLevel": True}], "2 levels"),
        ([{"name": "A", "defaultLevel": False}], "no level"),
        ([{"name": "A"}], "did not report"),
        ([{"name": "A", "defaultLevel": "yes"}], "did not report"),
    ],
    ids=["two-defaults", "no-default", "flag-missing", "flag-not-a-bool"],
)
def test_list_log_levels_withholds_an_ambiguous_default(
    payload: list[dict[str, object]], reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withheld is not guessed. Two defaults is NOT hypothetical: the server's own seed file ships
    two, even though its createLevel endpoint enforces one, so "take the first" would invent an
    answer. A missing or non-bool flag must not take the whole listing down either: the NAMES are
    the tool's primary answer and stay readable."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    names, default, note = client.list_log_levels()
    assert names == [item["name"] for item in payload]
    assert default is None
    assert note is not None and reason in note


@pytest.mark.parametrize(
    "payload",
    [{}, "nope", [{"defaultLevel": True}], ["Info"], [{"name": 7, "defaultLevel": True}]],
    ids=["dict", "string", "item-without-name", "string-item", "non-str-name"],
)
def test_list_log_levels_unreadable_2xx_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11 strictness inherited from `_named_list`: an unreadable listing must RAISE. Collapsing to
    [] would tell a caller validating a level value that NONE of them exist."""
    client = OlogClient("http://olog")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(OlogResponseError):
        client.list_log_levels()


@pytest.mark.asyncio
async def test_list_log_levels_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(olog_url="")
    )

    def _boom(*args: object, **kwargs: object) -> OlogClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
    result = await _list_log_levels()
    assert result["enabled"] is False
    assert result["levels"] == []
    assert result["default_level"] is None


@pytest.mark.asyncio
async def test_list_log_levels_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
            return ["Info", "Problem"], "Info", None

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
    result = await _list_log_levels()
    assert result["enabled"] is True
    assert result["levels"] == ["Info", "Problem"]
    assert result["default_level"] == "Info"
    assert "note" not in result  # nothing to explain -> no noise


@pytest.mark.asyncio
async def test_list_log_levels_propagates_the_withholding_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note is the whole point of withholding an ambiguous default — a service that computed it
    and then dropped it would surface a bare null the caller cannot interpret."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Ambiguous:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
            return ["A", "B"], None, "marks 2 levels as default"

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Ambiguous)
    result = await _list_log_levels()
    assert result["default_level"] is None
    assert result["note"] == "marks 2 levels as default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (OlogConnectionError("down"), EpicsConnectionError),
        (OlogResponseError("garbage"), EpicsError),
    ],
    ids=["unreachable", "answered-but-unreadable"],
)
async def test_list_log_levels_splits_outage_from_bad_answer(
    raised: Exception, expected: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different facts, two different next actions: "the service is down" vs "the service
    ANSWERED and we could not read it". Collapsing them sends the reader after the wrong problem
    (S11 section 8 — the same split search already lives)."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
            raise raised

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Broken)
    with pytest.raises(expected):
        await _list_log_levels()


# --- search: the level/title facets (OA2/OA5) ---
#
# What a mock CAN prove: that we SEND what we claim to send, and that the blank guard fires before
# any request. What it CANNOT prove is whether the server HONOURS the filter — Olog silently drops
# parameters it does not know, so that half lives in tests/test_olog_live.py as a differential
# probe carrying both controls.


def test_search_sends_level_and_title_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wire names are `level` and `title`. A typo in either is silently IGNORED by Olog rather
    than rejected, so the names themselves are worth pinning."""
    client = OlogClient("http://olog")
    get = Mock(return_value=_resp({"logs": [], "hitCount": 0}))
    monkeypatch.setattr(client.session, "get", get)
    client.search_logbook(level="Problem", title="vacuum")
    params = get.call_args.kwargs["params"]
    assert params["level"] == "Problem"
    assert params["title"] == "vacuum"


def test_search_omits_level_and_title_when_not_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    """None means "not filtering" and must not reach the wire at all."""
    client = OlogClient("http://olog")
    get = Mock(return_value=_resp({"logs": [], "hitCount": 0}))
    monkeypatch.setattr(client.session, "get", get)
    client.search_logbook()
    params = get.call_args.kwargs["params"]
    assert "level" not in params
    assert "title" not in params


def test_level_split_matches_java_trim_not_python_strip() -> None:
    """Python's ``strip()`` is Unicode-aware; Java's ``trim()`` is not. Stripping more than the
    server does would normalise an UNMATCHABLE level into a configured name — the cross-check would
    then see a known level, stay silent, and let a fabricated emptiness through.

    Measured 2026-07-19: level='\\xa0Info' returns 0 hits where level='Info' returns 19, i.e. the
    server really does keep the NBSP."""
    assert split_level_values(" Info ") == ["Info"]  # ASCII padding: trimmed by both
    assert split_level_values("\t\nInfo\r") == ["Info"]  # all <= U+0020
    assert split_level_values("\xa0Info") == ["\xa0Info"]  # NBSP: kept, as the server keeps it
    assert split_level_values("Info,Problem") == ["Info", "Problem"]
    assert split_level_values("  ") == []  # still blank
    assert split_level_values("|;,") == []


def test_title_blank_guard_uses_the_titles_own_separator_class() -> None:
    """`title` and `level` do NOT share a separator class, and assuming they do is a silent bug.

    The server's title class is the Java literal ``[\\|,;\\s+]`` — inside a character class that
    trailing ``+`` is a LITERAL member, not a quantifier. So a ``+``-only title yields no search
    terms and Olog returns the UNFILTERED set (measured: title='+' -> every entry), while the same
    value is a perfectly ordinary level (measured: level='+' -> 0 hits, genuinely filtered)."""
    client = OlogClient("http://olog")
    with pytest.raises(OlogFilterValueError):
        client.search_logbook(title="+")
    with pytest.raises(OlogFilterValueError):
        client.search_logbook(title="  +  ")
    # ... and it must stay a usable value on the OTHER field, which does not split on '+'
    assert split_level_values("+") == ["+"]


@pytest.mark.parametrize("blank", ["", " ", ",", ",,", " ; ", "|"], ids=repr)
@pytest.mark.parametrize("field", ["level", "title"])
def test_search_refuses_a_blank_filter_before_any_request(
    field: str, blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank filter is refused BEFORE the request, because Olog's two answers to it are both
    misleading and they DISAGREE with each other: a blank level matches nothing (0 hits, reading as
    "no such entries") while a blank title is dropped (an UNFILTERED result presented as a filtered
    one). Measured 2026-07-19."""
    client = OlogClient("http://olog")

    def _boom(*args: object, **kwargs: object) -> Mock:
        raise AssertionError("no request may be issued for a blank filter")

    monkeypatch.setattr(client.session, "get", _boom)
    filters: dict[str, Any] = {field: blank}
    with pytest.raises(OlogFilterValueError):
        client.search_logbook(**filters)


@pytest.mark.asyncio
async def test_blank_filter_surfaces_as_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service maps it to INVALID_INPUT, not to a transport/response code: nothing was sent."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    with pytest.raises(EpicsError) as excinfo:
        await _search_logbook(level="  ")
    assert excinfo.value.error_code == "INVALID_INPUT"


# --- search: the empty-level-filter annotation (OA2) ---
#
# Olog answers an unrecognised level with 200 + 0 hits, so "this level does not exist" and "no
# entries have this level" are the SAME response. Without the annotation a caller reports the
# second when the first is true.


class _FakeSearch:
    """An OlogClient double: a fixed search result plus a levels listing (or a failure).

    ``search_logbook`` mirrors the REAL signature keyword for keyword rather than swallowing
    ``**kwargs``, and records what it was called with. A permissive double is not neutral here: it
    absorbs exactly the defect class these tests exist to catch — a service layer that forwards a
    misspelled keyword, or forwards nothing at all, would still be green against ``**kwargs``.
    """

    entries: ClassVar[list[dict[str, object]]] = []
    levels: ClassVar[tuple[list[str], str | None, str | None]] = (
        ["Info", "Problem"],
        "Info",
        None,
    )
    levels_error: ClassVar[Exception | None] = None
    total_matches: ClassVar[int | None] = None
    seen: ClassVar[dict[str, object]] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def search_logbook(
        self,
        text: str | None = None,
        logbooks: str | None = None,
        tags: str | None = None,
        start: str | None = None,
        end: str | None = None,
        size: int = 50,
        offset: int = 0,
        sort: str = "down",
        level: str | None = None,
        title: str | None = None,
    ) -> tuple[list[dict[str, object]], bool, int | None]:
        type(self).seen = {
            "text": text,
            "logbooks": logbooks,
            "tags": tags,
            "start": start,
            "end": end,
            "size": size,
            "offset": offset,
            "sort": sort,
            "level": level,
            "title": title,
        }
        total = self.total_matches if self.total_matches is not None else len(self.entries)
        return self.entries, False, total

    def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
        if self.levels_error is not None:
            raise self.levels_error
        return self.levels


@pytest.mark.asyncio
async def test_service_forwards_level_and_title_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkers -> client pass-through, which nothing else exercises: a facet the service
    silently fails to forward would leave the search UNFILTERED while looking filtered."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeSearch)
    await _search_logbook(level="Problem", title="vacuum", size=7)
    assert _FakeSearch.seen["level"] == "Problem"
    assert _FakeSearch.seen["title"] == "vacuum"
    assert _FakeSearch.seen["size"] == 7


@pytest.mark.asyncio
async def test_empty_page_past_the_end_is_not_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty PAGE is not an empty RESULT. Paging past the end returns no entries while
    total_matches says something DID match — annotating that would contradict the very payload it
    is attached to, and would blame a level that is doing its job."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _PastTheEnd(_FakeSearch):
        entries: ClassVar[list[dict[str, object]]] = []
        total_matches: ClassVar[int | None] = 12

        def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
            raise AssertionError("/levels must not be fetched for an empty PAGE")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _PastTheEnd)
    result = await _search_logbook(level="Warning", offset=999)
    assert result["total"] == 0
    assert result["total_matches"] == 12
    assert "note" not in result


@pytest.mark.asyncio
async def test_note_does_not_judge_a_wildcard_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """Olog HONOURS a wildcard level (measured: 'Inf*' returns the Info entries), so a wildcard
    part cannot be checked against the name list — declaring it 'not a configured level' would deny
    the real cause. It is named as unchecked instead."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeSearch)
    result = await _search_logbook(level="Zzz*")
    assert "note" not in result  # nothing checkable was unknown -> no verdict at all


@pytest.mark.asyncio
async def test_note_on_a_mixed_or_list_does_not_generalise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OR-ed filter is not all-or-nothing. With one valid and one invalid value the search DID
    run on the valid one, so the note must name the unknown part AND say the search still ran."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeSearch)
    result = await _search_logbook(level="Info,Warnign")
    note = result["note"]
    assert isinstance(note, str)
    assert "'Warnign'" in note
    assert "did still run on 'Info'" in note
    # and it must NOT claim the whole filter was bogus
    assert "may account for the empty result as well" not in note


@pytest.mark.asyncio
async def test_empty_result_names_an_unknown_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeSearch)
    result = await _search_logbook(level="Warning")
    note = result["note"]
    assert isinstance(note, str)
    assert "Warning" in note
    assert "does not name a configured level" in note
    # It states a fact about the VALUE and must NOT claim to know why the result is empty — other
    # filters in the same search can produce the identical 0.
    assert "may account for the empty result as well" in note


@pytest.mark.asyncio
async def test_empty_result_for_a_known_level_is_not_annotated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured level that simply has no entries is an honest 0. Annotating it would be noise,
    and noise trains the reader to skip the note that matters. Also pins the case-insensitive
    comparison — the server matches case-insensitively, so the cross-check must too."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeSearch)
    result = await _search_logbook(level="problem")
    assert "note" not in result


@pytest.mark.asyncio
async def test_nonempty_result_is_never_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extra /levels lookup runs ONLY on an empty result — a result that found something needs
    no excuse, and must not pay for a second round trip."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _Found(_FakeSearch):
        entries: ClassVar[list[dict[str, object]]] = [{"id": 1}]

        def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
            raise AssertionError("/levels must not be fetched when the search found something")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Found)
    result = await _search_logbook(level="Warning")
    assert "note" not in result


@pytest.mark.asyncio
async def test_unreadable_levels_lookup_says_so_and_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cross-check must neither overturn a search that succeeded (withheld is not no) nor
    be swallowed into what would read as a clean bill of health."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(olog_url="http://olog"),
    )

    class _LevelsDown(_FakeSearch):
        levels_error: ClassVar[Exception | None] = OlogResponseError("levels unreadable")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _LevelsDown)
    result = await _search_logbook(level="Warning")
    assert result["enabled"] is True  # the search itself still succeeded
    note = result["note"]
    assert isinstance(note, str)
    assert "Could not verify" in note
