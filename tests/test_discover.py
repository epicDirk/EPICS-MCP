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
