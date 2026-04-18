"""Tests for read tool functions (_get_pv_value, _get_pvs)."""

from unittest.mock import AsyncMock, patch

import pytest

import epics_pv_mcp.config as config_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.tools.read import _get_pv_value, _get_pvs


@pytest.fixture(autouse=True)
def _reset_config():
    """Ensure get_config() returns a fresh default config for each test."""
    config_module._config = EpicsConfig()
    yield
    config_module._config = None


class TestGetPvValue:
    """Single PV read via _get_pv_value."""

    @patch("epics_pv_mcp.tools.read.pv_get", new_callable=AsyncMock)
    async def test_get_pv_value_returns_result(self, mock_pv_get):
        mock_pv_get.return_value = {
            "pv_name": "TEST:PV",
            "value": 42.0,
            "alarm": {"severity": 0, "status": 0},
        }

        result = await _get_pv_value("TEST:PV")

        mock_pv_get.assert_awaited_once_with("TEST:PV", 5.0)
        assert result["pv_name"] == "TEST:PV"
        assert result["value"] == 42.0

    @patch("epics_pv_mcp.tools.read.pv_get", new_callable=AsyncMock)
    async def test_get_pv_value_custom_timeout(self, mock_pv_get):
        mock_pv_get.return_value = {"pv_name": "X:PV", "value": 1.0}

        await _get_pv_value("X:PV", timeout=10.0)

        mock_pv_get.assert_awaited_once_with("X:PV", 10.0)


class TestGetPvs:
    """Batch PV read via _get_pvs."""

    @patch("epics_pv_mcp.tools.read.pv_get_batch", new_callable=AsyncMock)
    async def test_get_pvs_success(self, mock_batch):
        mock_batch.return_value = {
            "results": [
                {"pv_name": "A:PV", "value": 1.0},
                {"pv_name": "B:PV", "value": 2.0},
                {"pv_name": "C:PV", "value": 3.0},
            ],
            "errors": [],
        }

        result = await _get_pvs(["A:PV", "B:PV", "C:PV"])

        mock_batch.assert_awaited_once_with(["A:PV", "B:PV", "C:PV"], 5.0)
        assert len(result["results"]) == 3
        assert result["errors"] == []

    async def test_get_pvs_empty_list_raises(self):
        with pytest.raises(EpicsError) as exc_info:
            await _get_pvs([])

        assert exc_info.value.error_code == "INVALID_INPUT"

    async def test_get_pvs_exceeds_batch_size_raises(self):
        names = [f"PV:{i}" for i in range(101)]

        with pytest.raises(EpicsError) as exc_info:
            await _get_pvs(names)

        assert exc_info.value.error_code == "BATCH_TOO_LARGE"

    @patch("epics_pv_mcp.tools.read.pv_get_batch", new_callable=AsyncMock)
    async def test_get_pvs_at_batch_limit(self, mock_batch):
        """Exactly max_batch_size PVs should be accepted."""
        mock_batch.return_value = {"results": [], "errors": []}
        names = [f"PV:{i}" for i in range(100)]

        result = await _get_pvs(names)

        mock_batch.assert_awaited_once()
        assert "results" in result
