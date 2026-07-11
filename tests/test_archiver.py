"""Offline tests for the Archiver Appliance client + tools (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services._http import http_status, is_ssl_error
from epics_pv_mcp.services.archiver_client import ArchiverClient, HistoryResult, Sample
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverError,
    ArchiverResponseError,
)
from epics_pv_mcp.tools.archiver import (
    _get_archive_info,
    _get_pv_history,
    _is_archived,
    _list_archived_pvs,
)


def _resp(payload: object, *, ok: bool = True) -> Mock:
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- client: is_archived / get_archive_status ---


def test_is_archived_true(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"pvName": "X", "status": "Being archived"}])),
    )
    archived, status = client.is_archived("X")
    assert archived is True
    assert status == "Being archived"


def test_is_archived_false(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp([{"pvName": "X", "status": "Paused"}]))
    )
    archived, status = client.is_archived("X")
    assert archived is False
    assert status == "Paused"


# --- client: get_pv_history (DS-4B extended return contract) ---


def test_get_pv_history_status_ok_projects_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [
        {
            "meta": {"name": "X"},
            "data": [
                {"secs": 1, "nanos": 0, "val": 1.0, "severity": 0, "status": 0},
                {"secs": 2, "nanos": 0, "val": 2.0, "severity": 1, "status": 0},
                {"secs": 3, "nanos": 0, "val": 3.0, "severity": 0, "status": 0},
            ],
        }
    ]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history(
        "X", "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", max_points=2
    )
    assert result["status"] == "ok"
    assert result["withheld_reason"] is None
    assert result["capped"] is True
    assert len(result["samples"]) == 2
    assert result["samples"][0]["secs"] == 1
    assert result["samples"][1]["val"] == 2.0
    assert result["meta"] == {"name": "X"}  # DS-4A: getData.json meta block (EGU/PREC)


def test_get_pv_history_status_empty_keeps_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4B: a VALID response with an empty data array is genuinely-empty history (status
    'empty', no withheld_reason) — NOT conflated with a malformed response. The meta block
    (units/precision) is still surfaced for the window."""
    raw = [{"meta": {"name": "X", "EGU": "V", "PREC": "2"}, "data": []}]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", "a", "b")
    assert result["status"] == "empty"
    assert result["withheld_reason"] is None
    assert result["samples"] == []
    assert result["capped"] is False
    assert result["meta"] == {"name": "X", "EGU": "V", "PREC": "2"}


@pytest.mark.parametrize(
    "payload",
    [
        [],  # malformed top-level (empty)
        "nope",  # malformed top-level (not a list)
        [123],  # top-level list whose element is not a dict
        [{"meta": {"name": "X"}}],  # block present but no 'data' key
        [{"meta": {"name": "X"}, "data": "not-a-list"}],  # 'data' is not a list
    ],
)
def test_get_pv_history_withheld_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """DS-4B: an uninterpretable response is WITHHELD (status 'withheld',
    withheld_reason 'unexpected_payload') — a bare [] must never masquerade as 'empty history'
    when the truth is 'could not read'."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    result = client.get_pv_history("X", "a", "b")
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_payload"
    assert result["samples"] == []
    assert result["capped"] is False


def test_get_pv_history_meta_none_and_nondict_coerced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-non-dict meta (JSON null / list) coerces to {} and does NOT make the result
    withheld — the data array is still the source of truth for empty/ok."""
    payloads: list[object] = [[{"meta": None, "data": []}], [{"meta": ["x"], "data": []}]]
    for payload in payloads:
        client = ArchiverClient("http://arch")
        monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
        result = client.get_pv_history("X", "a", "b")
        assert result["meta"] == {}
        assert result["status"] == "empty"
        assert result["withheld_reason"] is None


def test_get_pv_history_withheld_unexpected_sample_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-4B: a non-empty data array holding no readable {secs,...} dicts is withheld
    ('unexpected_sample_shape'), not silently reported as ok with zero samples."""
    raw = [{"meta": {"name": "X"}, "data": ["junk", 42, None]}]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", "a", "b")
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_sample_shape"
    assert result["samples"] == []


@pytest.mark.parametrize("max_points", [0, -1])
def test_get_pv_history_nonpositive_max_points_not_withheld(
    monkeypatch: pytest.MonkeyPatch, max_points: int
) -> None:
    """DS-4B regression: a non-positive cap must NOT turn a valid, readable response into
    status='withheld' / drop samples. The cap is clamped to >=1, so one sample survives and the
    result is honest ('ok'), never the false 'unexpected_sample_shape'."""
    raw = [
        {
            "meta": {"name": "X"},
            "data": [{"secs": 1, "nanos": 0, "val": 1.5, "severity": 0, "status": 0}],
        }
    ]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", "a", "b", max_points=max_points)
    assert result["status"] == "ok"
    assert result["withheld_reason"] is None
    assert len(result["samples"]) == 1
    assert result["samples"][0]["val"] == 1.5


def test_get_pv_history_mixed_valid_and_junk_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A data array mixing readable sample dicts with junk yields status='ok' with only the
    readable samples (junk silently skipped) — NOT withheld, because at least one dict was read."""
    raw = [
        {
            "meta": {"name": "X"},
            "data": [
                {"secs": 1, "nanos": 0, "val": 1.0, "severity": 0, "status": 0},
                "junk",
                None,
                {"secs": 2, "nanos": 0, "val": 2.0, "severity": 0, "status": 0},
            ],
        }
    ]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", "a", "b")
    assert result["status"] == "ok"
    assert [s["val"] for s in result["samples"]] == [1.0, 2.0]


def test_get_pv_history_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ArchiverConnectionError):
        client.get_pv_history("X", "a", "b")


# --- client: get_pv_type_info (DS-4B — archive configuration via getPVTypeInfo) ---


def test_get_pv_type_info_projects_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4B: getPVTypeInfo surfaces the archive CONFIGURATION — sampling (method/period),
    retention (the STS/MTS/LTS data stores), computed rates, DBRType, archived fields, source
    host and creation time — projected onto snake_case keys."""
    record = {
        "pvName": "X",
        "DBRType": "DBR_SCALAR_DOUBLE",
        "samplingMethod": "MONITOR",
        "samplingPeriod": "1.0",
        "computedEventRate": "0.5",
        "computedStorageRate": "4.0",
        "computedBytesPerEvent": "8",
        "elementCount": "1",
        "archiveFields": ["HIHI", "LOLO"],
        "dataStores": ["pb://localhost?name=STS", "pb://localhost?name=MTS"],
        "hostName": "ioc-host",
        "creationTime": "2026-06-01T00:00:00.000Z",
        "applianceIdentity": "appliance0",
        "paused": "false",
        "userParams": "SHOULD NOT be surfaced",
    }
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(record)))
    result = client.get_pv_type_info("X")
    # Exact-equality pins the invariant "surface ONLY the _TYPE_INFO_FIELDS allowlist" — it excludes
    # userParams (free text) AND pvName (a non-allowlisted field in the input) AND any future
    # non-allowlisted field a denylist refactor might leak, not merely "the one named field".
    assert result == {
        "found": True,
        "dbr_type": "DBR_SCALAR_DOUBLE",
        "sampling_method": "MONITOR",
        "sampling_period": "1.0",
        "event_rate": "0.5",
        "storage_rate": "4.0",
        "bytes_per_event": "8",
        "element_count": "1",
        "archive_fields": ["HIHI", "LOLO"],
        "data_stores": ["pb://localhost?name=STS", "pb://localhost?name=MTS"],
        "host_name": "ioc-host",
        "creation_time": "2026-06-01T00:00:00.000Z",
        "appliance": "appliance0",
        "paused": "false",
    }
    assert "pvName" not in result  # non-allowlisted input field must not leak
    assert "userParams" not in result  # free-text field deliberately dropped


def test_get_pv_type_info_omits_absent_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sparse record surfaces found=True but omits the fields it lacks (no null noise)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp({"DBRType": "DBR_SCALAR_INT"}))
    )
    result = client.get_pv_type_info("X")
    assert result == {"found": True, "dbr_type": "DBR_SCALAR_INT"}


def test_get_pv_type_info_unwraps_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some appliance versions wrap the record in a 1-element list (like getPVStatus)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp([{"samplingMethod": "SCAN"}]))
    )
    result = client.get_pv_type_info("X")
    assert result == {"found": True, "sampling_method": "SCAN"}


@pytest.mark.parametrize("payload", [{}, [{}], [], "nope", None, [123]])
def test_get_pv_type_info_not_found(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """An empty / non-record payload (unknown PV, appliance returns no type info) -> found=False,
    never a raw passthrough or a crash. [{}] pins the list-wrap ``data[0] or None`` branch."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    result = client.get_pv_type_info("X")
    assert result == {"found": False}


def test_get_pv_type_info_404_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The appliance answers getPVTypeInfo with HTTP 404 for a never-archived PV (unlike
    getPVStatus, which 200s). That 404 must map to found:False, NOT a raised error — otherwise a
    normal "not archived" PV is indistinguishable from an unreachable appliance."""
    client = ArchiverClient("http://arch")
    http_error = requests.exceptions.HTTPError("404")
    http_error.response = Mock(status_code=404)
    resp = Mock()
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    result = client.get_pv_type_info("X")
    assert result == {"found": False}


def test_get_pv_type_info_non_404_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-404 failure (5xx / unreachable) must PROPAGATE as an ArchiverError — a could-not-read
    is never silently reported as "not archived" (the inverse of the 404 case)."""
    client = ArchiverClient("http://arch")
    http_error = requests.exceptions.HTTPError("500")
    http_error.response = Mock(status_code=500)
    resp = Mock()
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    with pytest.raises(ArchiverError):
        client.get_pv_type_info("X")


# --- two-URL routing (split deployment: mgmt :17665 vs retrieval :17668) ---


def test_two_url_routing_mgmt_vs_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_archived / get_pv_type_info hit the MGMT base_url; get_pv_history the retrieval_url.

    In a split deployment /mgmt and /retrieval live on different Tomcats/ports,
    so the calls must NOT share one base URL.
    """
    client = ArchiverClient("http://arch:17665", retrieval_url="http://arch:17668")
    captured: list[str] = []

    def _get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured.append(url)
        if "getPVStatus" in url:
            return _resp([{"pvName": "X", "status": "Being archived"}])
        if "getPVTypeInfo" in url:
            return _resp({"DBRType": "DBR_SCALAR_DOUBLE"})
        return _resp([{"meta": {"name": "X"}, "data": []}])

    monkeypatch.setattr(client.session, "get", _get)
    client.is_archived("X")
    client.get_pv_type_info("X")
    client.get_pv_history("X", "a", "b")
    assert captured[0] == "http://arch:17665/mgmt/bpl/getPVStatus"
    assert captured[1] == "http://arch:17665/mgmt/bpl/getPVTypeInfo"
    assert captured[2] == "http://arch:17668/retrieval/data/getData.json"


def test_retrieval_url_defaults_to_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-JVM appliance: no retrieval_url -> get_pv_history falls back to base_url."""
    client = ArchiverClient("http://arch:17665")
    assert client.retrieval_url == "http://arch:17665"
    captured: list[str] = []

    def _get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured.append(url)
        return _resp([{"meta": {"name": "X"}, "data": []}])

    monkeypatch.setattr(client.session, "get", _get)
    client.get_pv_history("X", "a", "b")
    assert captured[0] == "http://arch:17665/retrieval/data/getData.json"


# --- client: get_archive_status (DS-4A — enriched getPVStatus fields) ---


def test_get_archive_status_enriches_present_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4A: get_archive_status surfaces the useful extra MGMT-record fields
    (connectionState/lastEvent/isMonitored/...) at ~zero cost, plus archived/status."""
    client = ArchiverClient("http://arch")
    record = {
        "pvName": "X",
        "status": "Being archived",
        "connectionState": True,
        "lastEvent": "Jun/01/2026 10:00:00 UTC",
        "isMonitored": True,
        "samplingPeriod": "1.0",
        "appliance": "appliance0",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([record])))
    result = client.get_archive_status("X")
    assert result["archived"] is True
    assert result["status"] == "Being archived"
    assert result["connection_state"] is True
    assert result["last_event"] == "Jun/01/2026 10:00:00 UTC"
    assert result["is_monitored"] is True
    assert result["sampling_period"] == "1.0"
    assert result["appliance"] == "appliance0"


def test_get_archive_status_omits_absent_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the MGMT record lacks the extra fields, they are omitted (not null noise)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp([{"pvName": "X", "status": "Paused"}]))
    )
    result = client.get_archive_status("X")
    assert result["archived"] is False
    assert result["status"] == "Paused"
    for absent in (
        "connection_state",
        "last_event",
        "is_monitored",
        "sampling_period",
        "appliance",
    ):
        assert absent not in result


# --- tools: is_archived / get_pv_history / get_archive_info ---


@pytest.mark.asyncio
async def test_is_archived_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gating + client construction live in services/checkers.query_archived (M9); patch there.
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ArchiverClient", _boom)
    result = await _is_archived("X")
    assert result["enabled"] is False
    assert result["archived"] is None


@pytest.mark.asyncio
async def test_get_pv_history_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _get_pv_history("X", "a", "b")
    assert result["enabled"] is False
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_get_archive_info_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _get_archive_info("X")
    assert result["enabled"] is False
    # found is None (NOT checked) when disabled — a disabled plane must never masquerade as a
    # definitive "no archive record" (found:False), mirroring the sibling archived/configured None.
    assert result["found"] is None


@pytest.mark.asyncio
async def test_is_archived_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_archive_status(self, pv: str) -> dict[str, object]:
            return {
                "archived": True,
                "status": "Being archived",
                "connection_state": True,
                "last_event": "Jun/01/2026 10:00:00 UTC",
                "is_monitored": True,
            }

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ArchiverClient", _Fake)
    result = await _is_archived("X")
    assert result["enabled"] is True
    assert result["archived"] is True
    assert result["status"] == "Being archived"
    assert result["connection_state"] is True  # DS-4A: enriched getPVStatus fields surfaced
    assert result["is_monitored"] is True


@pytest.mark.asyncio
async def test_get_pv_history_tool_passes_retrieval_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_pv_history must construct ArchiverClient with the configured retrieval URL."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(
            archiver_url="http://arch:17665",
            archiver_retrieval_url="http://arch:17668",
        ),
    )
    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, base_url: str, *args: object, **kwargs: object) -> None:
            captured["base_url"] = base_url
            captured["retrieval_url"] = kwargs.get("retrieval_url")

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> HistoryResult:
            return HistoryResult(
                samples=[Sample(secs=1, nanos=0, val=1.0, severity=0, status=0)],
                capped=False,
                meta={},
                status="ok",
                note="",
                withheld_reason=None,
            )

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", "a", "b")
    assert result["enabled"] is True
    assert captured["base_url"] == "http://arch:17665"
    assert captured["retrieval_url"] == "http://arch:17668"


@pytest.mark.asyncio
async def test_get_pv_history_tool_surfaces_meta_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-4A: the tool surfaces the meta block (EGU/PREC); DS-4B: it surfaces status/withheld."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> HistoryResult:
            return HistoryResult(
                samples=[Sample(secs=1, nanos=0, val=1.0, severity=0, status=0)],
                capped=False,
                meta={"EGU": "V", "PREC": "2"},
                status="ok",
                note="",
                withheld_reason=None,
            )

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", "a", "b")
    assert result["enabled"] is True
    assert result["total"] == 1
    assert result["meta"] == {"EGU": "V", "PREC": "2"}
    assert result["status"] == "ok"
    assert "withheld_reason" not in result  # omitted on non-withheld results


@pytest.mark.asyncio
async def test_get_pv_history_tool_surfaces_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4B: a withheld client result surfaces status + withheld_reason + note at the tool."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> HistoryResult:
            return HistoryResult(
                samples=[],
                capped=False,
                meta={},
                status="withheld",
                note="Archiver returned an unexpected payload.",
                withheld_reason="unexpected_payload",
            )

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", "a", "b")
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_payload"
    assert result["note"] == "Archiver returned an unexpected payload."
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_get_archive_info_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_archive_info builds the client (MGMT base) and surfaces the projected type info."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch:17665"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_type_info(self, pv: str) -> dict[str, object]:
            return {
                "found": True,
                "dbr_type": "DBR_SCALAR_DOUBLE",
                "sampling_method": "MONITOR",
                "data_stores": ["pb://localhost?name=STS"],
            }

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_archive_info("X")
    assert result["enabled"] is True
    assert result["pv"] == "X"
    assert result["found"] is True
    assert result["dbr_type"] == "DBR_SCALAR_DOUBLE"
    assert result["sampling_method"] == "MONITOR"
    assert result["data_stores"] == ["pb://localhost?name=STS"]


# --- check_connectivity (E2 doctor probe: getApplianceInfo, 2xx required) ---


def test_check_connectivity_probes_appliance_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe hits /mgmt/bpl/getApplianceInfo (no PV) and returns True on a 2xx."""
    client = ArchiverClient("http://arch:17665")
    captured: list[str] = []

    def fake_get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured.append(url)
        return _resp([{"identity": "appliance0"}])

    monkeypatch.setattr(client.session, "get", fake_get)
    assert client.check_connectivity() is True
    assert captured[0] == "http://arch:17665/mgmt/bpl/getApplianceInfo"


def test_check_connectivity_served_non2xx_raises_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A served non-2xx (e.g. ARCHIVER_URL points at the wrong webapp) → ArchiverResponseError,
    NOT ArchiverConnectionError — doctor reads it as 'api_error' (reachable), not 'unreachable'."""
    client = ArchiverClient("http://arch:17665")
    resp = Mock()
    http_error = requests.exceptions.HTTPError("404")
    http_error.response = Mock(status_code=404)
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    with pytest.raises(ArchiverResponseError) as excinfo:
        client.check_connectivity()
    # The from-exc chain the doctor classifier depends on: __cause__ carries the served HTTP status,
    # so a dropped `from exc` would silently mark this reachable-but-erroring host unreachable.
    assert http_status(excinfo.value) == 404


def test_check_connectivity_ssl_error_chains_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real TLS/CA failure chains the SSLError so doctor buckets it ca_error, not unreachable."""
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.SSLError("self-signed"))
    )
    with pytest.raises(ArchiverConnectionError) as excinfo:
        client.check_connectivity()
    assert is_ssl_error(excinfo.value) is True


def test_check_connectivity_transport_failure_raises_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ArchiverConnectionError):
        client.check_connectivity()


# --- client: get_all_pvs / get_pvs_for_this_appliance (E2 list_archived_pvs) ---


def test_get_all_pvs_returns_names_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch:17665")
    captured: dict[str, object] = {}

    def fake_get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured["url"] = url
        return _resp(["SYS:PV1", "SYS:PV2"])

    monkeypatch.setattr(client.session, "get", fake_get)
    names, capped = client.get_all_pvs()
    assert names == ["SYS:PV1", "SYS:PV2"]
    assert capped is False
    assert captured["url"] == "http://arch:17665/mgmt/bpl/getAllPVs"


def test_get_all_pvs_forwards_pattern_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name glob maps to the ``pv`` param; over-fetch by one makes ``capped`` honest."""
    client = ArchiverClient("http://arch")
    captured: dict[str, object] = {}

    def fake_get(url: str, params: dict[str, str] | None = None, timeout: object = None) -> Mock:
        captured["params"] = params or {}
        return _resp([f"PV{i}" for i in range(5)])  # 5 names, limit 3 → capped

    monkeypatch.setattr(client.session, "get", fake_get)
    names, capped = client.get_all_pvs(pattern="DEV-TEST01:*", limit=3)
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["pv"] == "DEV-TEST01:*"
    assert params["limit"] == "4"  # limit + 1 over-fetch
    assert names == ["PV0", "PV1", "PV2"]  # sliced to limit
    assert capped is True


def test_get_pvs_for_this_appliance(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch:17665")
    captured: dict[str, object] = {}

    def fake_get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured["url"] = url
        return _resp(["M:PV1"])

    monkeypatch.setattr(client.session, "get", fake_get)
    names, capped = client.get_pvs_for_this_appliance()
    assert names == ["M:PV1"]
    assert capped is False
    assert captured["url"] == "http://arch:17665/mgmt/bpl/getPVsForThisAppliance"


def test_get_all_pvs_non_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({"oops": 1})))
    with pytest.raises(ArchiverResponseError):
        client.get_all_pvs()


def test_get_all_pvs_capped_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly `limit` names → capped False; limit+1 → capped True (pins `len(names) > limit`)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(["A", "B", "C"])))
    names, capped = client.get_all_pvs(limit=3)
    assert names == ["A", "B", "C"]
    assert capped is False  # exactly limit — an off-by-one (>=) regression would flip this
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(["A", "B", "C", "D"])))
    names, capped = client.get_all_pvs(limit=3)
    assert names == ["A", "B", "C"]  # sliced to limit
    assert capped is True


def test_coerce_pv_names_nonpositive_limit_clamped() -> None:
    """A non-positive limit is clamped to >=1 — never a negative slice that silently drops the last
    name (`names[:-1]`) or empties the list, with a falsely-True capped."""
    for bad in (-1, 0):
        names, capped = ArchiverClient._coerce_pv_names(["A", "B", "C"], bad, "getAllPVs")
        assert names == ["A"]  # clamped to limit 1, NOT names[:-1]==["A","B"] nor names[:0]==[]
        assert capped is True  # honestly capped (3 > 1)


# --- tool: list_archived_pvs ---


@pytest.mark.asyncio
async def test_list_archived_pvs_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch:17665"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            return (["A:PV1", "A:PV2"], False)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _list_archived_pvs()
    assert result["enabled"] is True
    assert result["pvs"] == ["A:PV1", "A:PV2"]
    assert result["total"] == 2
    assert result["capped"] is False


@pytest.mark.asyncio
async def test_list_archived_pvs_this_appliance_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            raise AssertionError("this_appliance=True must use getPVsForThisAppliance")

        def get_pvs_for_this_appliance(self, limit: int = 5000) -> tuple[list[str], bool]:
            return (["M:PV1"], True)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _list_archived_pvs(this_appliance=True)
    assert result["pvs"] == ["M:PV1"]
    assert result["capped"] is True


@pytest.mark.asyncio
async def test_list_archived_pvs_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _list_archived_pvs()
    assert result["enabled"] is False
    assert result["pvs"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_list_archived_pvs_forwards_pattern_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool forwards pattern + limit to the client's get_all_pvs (the tool→client wire)."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            captured["pattern"] = pattern
            captured["limit"] = limit
            return (["X"], False)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    await _list_archived_pvs(pattern="DEV-TEST01:*", limit=7)
    assert captured == {"pattern": "DEV-TEST01:*", "limit": 7}


@pytest.mark.asyncio
async def test_list_archived_pvs_this_appliance_ignores_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented behavior: this_appliance=True routes to getPVsForThisAppliance (no `pv` param), so
    pattern is deliberately NOT forwarded — only limit is."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            raise AssertionError("this_appliance=True must not call get_all_pvs")

        def get_pvs_for_this_appliance(self, limit: int = 5000) -> tuple[list[str], bool]:
            captured["limit"] = limit
            return (["M"], False)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _list_archived_pvs(pattern="DEV-TEST01:*", this_appliance=True, limit=9)
    assert result["pvs"] == ["M"]
    assert captured == {"limit": 9}  # pattern silently dropped (endpoint has no pv param)


@pytest.mark.asyncio
async def test_list_archived_pvs_maps_archiver_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client ArchiverError surfaces as EpicsConnectionError with the 'Archiver: ' prefix."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            raise ArchiverConnectionError("boom")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    with pytest.raises(EpicsConnectionError, match="Archiver:"):
        await _list_archived_pvs()
