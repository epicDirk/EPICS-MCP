"""Offline tests for the ChannelFinder client + tool (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services.channelfinder_client import ChannelFinderClient, ChannelInfo
from epics_pv_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderResponseError,
)
from epics_pv_mcp.tools.channelfinder import _find_channels


def _resp(payload: object, *, ok: bool = True) -> Mock:
    """Build a fake requests response with the given JSON payload."""
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")
    return resp


# --- client ---


def test_project_extracts_ioc_host_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf:8080/ChannelFinder")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "cf",
            "properties": [
                {"name": "iocName", "value": "IOC1"},
                {"name": "hostName", "value": "host1"},
            ],
            "tags": [{"name": "archived"}, {"name": "alarm"}],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert len(out) == 1
    assert out[0]["name"] == "SYS:PV1"
    assert out[0]["ioc_name"] == "IOC1"
    assert out[0]["host_name"] == "host1"
    assert out[0]["tags"] == ("alarm", "archived")  # sorted, deterministic


def test_project_redacts_person_owner_and_recceiverid(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY: a service-account owner (recceiver) is kept, a person's username owner is
    redacted to "", and the opaque properties['recceiverID'] is dropped — technical provenance
    (iocName/hostName) is untouched."""
    client = ChannelFinderClient("http://cf")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "recceiver",
            "properties": [
                {"name": "iocName", "value": "IOC1"},
                {"name": "recceiverID", "value": "abc-123"},
            ],
        },
        {
            "name": "SYS:PV2",
            "owner": "jdoe",  # a person's ESS username (CF web-UI channel)
            "properties": [{"name": "iocName", "value": "IOC2"}],
        },
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert out[0]["owner"] == "recceiver"  # service account kept
    assert "recceiverID" not in out[0]["properties"]  # opaque id dropped (not on allowlist)
    assert out[0]["ioc_name"] == "IOC1"  # technical provenance untouched
    assert out[1]["owner"] == ""  # person's username redacted


def test_project_allowlists_properties_drops_person_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-PRIVACY: the pre-live-smoke audit exhibited a person-name leak through a property VALUE —
    the reccaster ENGINEER/LOCATION env-var convention (devIocStats) or a cfstore custom field. The
    surfaced properties must be an ALLOWLIST of known-technical names, dropping any other property
    by default; the technical iocName/hostName survive (and still feed ioc_name/host_name)."""
    client = ChannelFinderClient("http://cf")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "recceiver",  # safe RecSync service account — owner gives no protection here
            "properties": [
                {"name": "iocName", "value": "IOC1", "owner": "recceiver"},
                {"name": "hostName", "value": "host1", "owner": "recceiver"},
                {"name": "ENGINEER", "value": "John Smith", "owner": "recceiver"},
                {"name": "LOCATION", "value": "Hall B, desk of A. Person", "owner": "recceiver"},
            ],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    props = out[0]["properties"]
    assert props == {"iocName": "IOC1", "hostName": "host1"}  # only technical names survive
    assert "ENGINEER" not in props  # person name in the VALUE dropped by the allowlist
    assert "LOCATION" not in props
    assert out[0]["ioc_name"] == "IOC1"  # allowlisted property still feeds the dedicated field
    assert out[0]["host_name"] == "host1"


def test_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    assert client.find_channels("NOPE:*") == []


def test_connection_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ChannelFinderConnectionError):
        client.find_channels("X")


def test_non_list_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({"oops": 1})))
    with pytest.raises(ChannelFinderResponseError):
        client.find_channels("X")


# --- tool ---


@pytest.mark.asyncio
async def test_tool_disabled_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no URL configured, the tool returns enabled=false and never constructs a client.

    Gating + client construction moved to services/checkers.query_channels (M9); _find_channels is
    a thin delegator, so config/client are patched there.
    """
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url=""),
    )

    def _boom(*args: object, **kwargs: object) -> ChannelFinderClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _boom)
    result = await _find_channels("X")
    assert result["enabled"] is False
    assert result["channels"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_tool_enabled_returns_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def find_channels(self, pattern: str, max_results: int = 500) -> list[ChannelInfo]:
            return [
                ChannelInfo(
                    name="P",
                    owner="o",
                    ioc_name="I",
                    host_name="h",
                    properties={"iocName": "I"},
                    tags=("t",),
                )
            ]

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _Fake)
    result = await _find_channels("P*")
    assert result["enabled"] is True
    assert result["total"] == 1
    channels = result["channels"]
    assert isinstance(channels, list)
    assert channels[0]["name"] == "P"


def _ch(name: str) -> ChannelInfo:
    return ChannelInfo(name=name, owner="o", ioc_name=None, host_name=None, properties={}, tags=())


class _BoundedCFClient:
    """Emulates CF's ~size truncation: never returns more than the requested max_results."""

    available = 3

    def __init__(self, *args: object, **kwargs: object) -> None: ...

    def find_channels(self, pattern: str, max_results: int = 500) -> list[ChannelInfo]:
        return [_ch(f"c{i}") for i in range(min(_BoundedCFClient.available, max_results))]


async def test_find_channels_capped_is_honest_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S8-6: exactly max_results real channels → capped False; max_results+1 → capped True. The tool
    fetches max_results+1 and truncates, so 'capped' is an honest '> max_results', not '>='."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _BoundedCFClient)

    _BoundedCFClient.available = 3  # exactly the cap → NOT capped (the off-by-one case)
    result = await _find_channels("X*", max_results=3)
    assert result["capped"] is False
    assert result["total"] == 3

    _BoundedCFClient.available = 4  # one over the cap → capped, truncated to the cap
    result = await _find_channels("X*", max_results=3)
    assert result["capped"] is True
    assert result["total"] == 3
