"""Tests for epics_pv_mcp.tools.discover."""

from unittest.mock import AsyncMock, patch

from epics_pv_mcp.errors import EpicsConnectionError, PVNotFoundError, PVTimeoutError
from epics_pv_mcp.tools.discover import _discover_pvs


async def test_discover_wildcard() -> None:
    result = await _discover_pvs("TEST:*")

    assert result["total"] == 0
    note = result["note"]
    assert isinstance(note, str)
    assert "ChannelFinder" in note


async def test_discover_concrete_found() -> None:
    with patch(
        "epics_pv_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        return_value={"pv_name": "TEST:PV", "value": 42.0},
    ):
        result = await _discover_pvs("TEST:PV")

    assert result["total"] == 1
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "found"
    assert pvs[0]["value"] == 42.0


async def test_discover_concrete_not_found() -> None:
    with patch(
        "epics_pv_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        side_effect=PVNotFoundError("PV 'MISSING:PV' not found"),
    ):
        result = await _discover_pvs("MISSING:PV")

    assert result["total"] == 0
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "not_found"


async def test_discover_concrete_timeout_is_distinct_from_not_found() -> None:
    """S3-5: a timeout (IOC down / network) reports status 'timeout', NOT 'not_found'."""
    with patch(
        "epics_pv_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        side_effect=PVTimeoutError("Timeout getting PV 'SLOW:PV'"),
    ):
        result = await _discover_pvs("SLOW:PV")
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "timeout"


async def test_discover_concrete_connection_error_is_status_error() -> None:
    """S3-5: a connection error reports status 'error', distinct from 'not_found'/'timeout'."""
    with patch(
        "epics_pv_mcp.tools.discover.pv_get",
        new_callable=AsyncMock,
        side_effect=EpicsConnectionError("broken pipe"),
    ):
        result = await _discover_pvs("BAD:PV")
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert pvs[0]["status"] == "error"


# --- MA-2: wildcard discovery delegates to ChannelFinder (query_channels) --------------------
# A wildcard used to return a permanent empty stub even with ChannelFinder configured. It now
# delegates to the existing CF glob search and maps the registered channels into the discover
# shape. Registration is NOT a liveness probe, so the status is a distinct 'registered' (never
# 'found', which means a real p4p connect succeeded).


def _cf_result(channels: list[dict[str, object]], *, capped: bool = False) -> dict[str, object]:
    """A query_channels success payload (enabled) with the given projected channels."""
    return {"enabled": True, "channels": channels, "total": len(channels), "capped": capped}


async def test_discover_wildcard_delegates_to_channelfinder() -> None:
    """MA-2: a wildcard + active ChannelFinder delegates and maps the channels into the discover
    shape with status 'registered'. Mutant (delegation removed) -> the empty stub -> this fails."""
    channels: list[dict[str, object]] = [
        {"name": "SIM:PS-01:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"},
        {"name": "SIM:PS-02:Cur-RB", "ioc_name": "sim-ioc", "host_name": "simhost"},
    ]
    with patch(
        "epics_pv_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_result(channels),
    ):
        result = await _discover_pvs("SIM:PS-*")

    assert result["total"] == 2
    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert {pv["pv_name"] for pv in pvs} == {"SIM:PS-01:Cur-RB", "SIM:PS-02:Cur-RB"}
    assert all(pv["status"] == "registered" for pv in pvs)
    # Registration != liveness, a status a caller could mistake for a live connect must not appear.
    assert all(pv["status"] not in {"found", "not_found", "timeout", "error"} for pv in pvs)


async def test_discover_wildcard_carries_channelfinder_cap() -> None:
    """MA-2: the CF ``capped`` flag (registry truncated) is carried through, not dropped."""
    channels: list[dict[str, object]] = [{"name": "SIM:A", "ioc_name": None, "host_name": None}]
    with patch(
        "epics_pv_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value=_cf_result(channels, capped=True),
    ):
        result = await _discover_pvs("SIM:*")

    assert result["capped"] is True


async def test_discover_wildcard_cf_disabled_keeps_honest_stub() -> None:
    """MA-2: with ChannelFinder NOT configured, the wildcard branch keeps today's honest stub
    (empty + a note naming ChannelFinder), never a bare empty that reads as 'no such PV'."""
    with patch(
        "epics_pv_mcp.tools.discover.query_channels",
        new_callable=AsyncMock,
        return_value={"enabled": False, "channels": [], "total": 0, "note": "cf disabled"},
    ):
        result = await _discover_pvs("TEST:*")

    assert result["total"] == 0
    assert result["pvs"] == []
    note = result["note"]
    assert isinstance(note, str)
    assert "ChannelFinder" in note
