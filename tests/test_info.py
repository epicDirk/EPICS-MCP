"""Tests for epics_pv_mcp.tools.info."""

from unittest.mock import AsyncMock, patch

import pytest

from epics_pv_mcp.errors import PVTimeoutError
from epics_pv_mcp.tools.info import _get_pv_info


async def test_get_pv_info_success():
    mock_return = {
        "pv_name": "TEST:PV",
        "value": 42.0,
        "alarm": {"severity": 0, "status": 0},
    }
    with patch(
        "epics_pv_mcp.tools.info.pv_get",
        new_callable=AsyncMock,
        return_value=mock_return,
    ):
        result = await _get_pv_info("TEST:PV")

    assert result["status"] == "success"
    assert result["pv_name"] == "TEST:PV"
    assert result["value"] == 42.0
    assert result["alarm"]["severity"] == 0
    assert result["alarm"]["status"] == 0


async def test_get_pv_info_timeout():
    with patch(
        "epics_pv_mcp.tools.info.pv_get",
        new_callable=AsyncMock,
        side_effect=PVTimeoutError("Timeout getting PV 'TEST:PV' after 5.0s"),
    ), pytest.raises(PVTimeoutError):
        await _get_pv_info("TEST:PV")
