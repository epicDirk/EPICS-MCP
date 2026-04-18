"""Tests for write tool functions (_set_pv_value) with safety checks."""

from unittest.mock import AsyncMock, patch

import pytest

import epics_pv_mcp.config as config_module
import epics_pv_mcp.safety as safety_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import PVWriteDeniedError, RateLimitError
from epics_pv_mcp.safety import SafetyLayer
from epics_pv_mcp.tools.write import _set_pv_value


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset config and safety singletons for each test."""
    config_module._config = None
    safety_module._safety = None
    yield
    config_module._config = None
    safety_module._safety = None


class TestSetPvValueSuccess:
    """Successful write with safety checks passing."""

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_set_pv_value_success(self, mock_get_safety, mock_pv_get, mock_pv_put):
        # Configure safety to allow writes
        cfg = EpicsConfig(allow_pv_write=True, write_rate_limit=10)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        # Mock the old value read
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 10.0}

        # Mock the put (returns None)
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "20.0")

        assert result["status"] == "success"
        assert result["pv_name"] == "TEST:PV"
        assert result["old_value"] == 10.0
        assert result["new_value"] == "20.0"

        mock_pv_get.assert_awaited_once_with("TEST:PV", 5.0)
        mock_pv_put.assert_awaited_once_with("TEST:PV", "20.0", 5.0)


class TestSetPvValueDenied:
    """Write denied by safety layer (writes disabled)."""

    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_set_pv_value_denied(self, mock_get_safety):
        # Configure safety to deny writes
        cfg = EpicsConfig(allow_pv_write=False)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        with pytest.raises(PVWriteDeniedError):
            await _set_pv_value("TEST:PV", "20.0")


class TestSetPvValueRateLimited:
    """Write rejected due to rate limit exhaustion."""

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_set_pv_value_rate_limited(self, mock_get_safety, mock_pv_get, mock_pv_put):
        # Configure safety with rate_limit=2
        cfg = EpicsConfig(allow_pv_write=True, write_rate_limit=2)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 0.0}
        mock_pv_put.return_value = None

        # Exhaust the rate limit
        await _set_pv_value("TEST:PV", "1.0")
        await _set_pv_value("TEST:PV", "2.0")

        # Third call should be rate-limited
        with pytest.raises(RateLimitError):
            await _set_pv_value("TEST:PV", "3.0")
