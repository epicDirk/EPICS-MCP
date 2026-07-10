"""Offline tests for the Archiver Appliance client + tools (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services.archiver_client import ArchiverClient, Sample
from epics_pv_mcp.services.archiver_exceptions import ArchiverConnectionError
from epics_pv_mcp.tools.archiver import _get_pv_history, _is_archived


def _resp(payload: object, *, ok: bool = True) -> Mock:
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- client ---


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


def test_get_pv_history_projects_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
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
    samples, capped, meta = client.get_pv_history(
        "X", "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", max_points=2
    )
    assert capped is True
    assert len(samples) == 2
    assert samples[0]["secs"] == 1
    assert samples[1]["val"] == 2.0
    assert meta == {"name": "X"}  # DS-4A: the getData.json meta block (EGU/PREC) is now surfaced


def test_get_pv_history_returns_meta_egu_prec(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4A: the getData.json meta block (units/precision) was discarded before; now returned."""
    raw = [{"meta": {"name": "X", "EGU": "V", "PREC": "2"}, "data": []}]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    _samples, _capped, meta = client.get_pv_history("X", "a", "b")
    assert meta == {"name": "X", "EGU": "V", "PREC": "2"}


@pytest.mark.parametrize(
    "payload",
    [
        [{"data": []}],  # no meta key
        [{"meta": None, "data": []}],  # meta is JSON null
        [{"meta": ["not", "a", "dict"], "data": []}],  # meta is a list
        [],  # malformed top-level (empty)
        "nope",  # malformed top-level (not a list)
    ],
)
def test_get_pv_history_meta_coerced_to_empty_dict(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """DS-4A: an absent / non-dict meta (or a malformed top-level response) must coerce to ``{}`` —
    never leak a non-mapping to consumers that do ``dict(meta)``. Pins the isinstance guard so a
    future edit dropping it fails loudly."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    samples, capped, meta = client.get_pv_history("X", "a", "b")
    assert meta == {}
    assert samples == []
    assert capped is False


def test_get_archive_status_enriches_present_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4A: getPVStatus already returns the full MGMT record; get_archive_status surfaces the
    useful extra fields (connectionState/lastEvent/isMonitored/...) at ~zero cost, plus
    archived/status derived from the same single call."""
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
    """When the MGMT record lacks the extra fields (Unknown/fallback / paused PV), they are omitted
    (not surfaced as null noise); archived/status are always present."""
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


def test_get_pv_history_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ArchiverConnectionError):
        client.get_pv_history("X", "a", "b")


# --- tools ---


@pytest.mark.asyncio
async def test_is_archived_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gating + client construction moved to services/checkers.query_archived (M9); _is_archived
    # is now a thin delegator, so the config/client are patched there.
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
async def test_is_archived_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # _is_archived delegates to services/checkers.query_archived (M9) — patch the client there.
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


# --- two-URL routing (ESS 4-instance topology: mgmt :17665 vs retrieval :17668) ---


def test_two_url_routing_mgmt_vs_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_archived must hit the MGMT base_url; get_pv_history the separate retrieval_url.

    In the ESS 4-instance appliance /mgmt and /retrieval live on different Tomcats/ports,
    so the two calls must NOT share one base URL.
    """
    client = ArchiverClient("http://arch:17665", retrieval_url="http://arch:17668")
    captured: list[str] = []

    def _get(url: str, params: object = None, timeout: object = None) -> Mock:
        captured.append(url)
        if "getPVStatus" in url:
            return _resp([{"pvName": "X", "status": "Being archived"}])
        return _resp([{"meta": {"name": "X"}, "data": []}])

    monkeypatch.setattr(client.session, "get", _get)
    client.is_archived("X")
    client.get_pv_history("X", "a", "b")
    assert captured[0] == "http://arch:17665/mgmt/bpl/getPVStatus"
    assert captured[1] == "http://arch:17668/retrieval/data/getData.json"


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
        ) -> tuple[list[Sample], bool, dict[str, object]]:
            return [Sample(secs=1, nanos=0, val=1.0, severity=0, status=0)], False, {}

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", "a", "b")
    assert result["enabled"] is True
    assert captured["base_url"] == "http://arch:17665"
    assert captured["retrieval_url"] == "http://arch:17668"


@pytest.mark.asyncio
async def test_get_pv_history_tool_surfaces_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4A: the tool result surfaces the getData.json meta block (EGU/PREC) the client returns."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> tuple[list[Sample], bool, dict[str, object]]:
            samples = [Sample(secs=1, nanos=0, val=1.0, severity=0, status=0)]
            return samples, False, {"EGU": "V", "PREC": "2"}

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", "a", "b")
    assert result["enabled"] is True
    assert result["total"] == 1
    assert result["meta"] == {"EGU": "V", "PREC": "2"}
