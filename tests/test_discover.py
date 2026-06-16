"""Tests for epics_pv_mcp.tools.discover."""

from unittest.mock import AsyncMock, patch

from epics_pv_mcp.errors import PVNotFoundError
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
