"""Tests for write tool functions (_set_pv_value) with safety checks."""

import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import epics_pv_mcp.config as config_module
import epics_pv_mcp.safety as safety_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import PVTimeoutError, PVWriteDeniedError, RateLimitError
from epics_pv_mcp.safety import SafetyLayer
from epics_pv_mcp.tools.write import _set_pv_value


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
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
    async def test_set_pv_value_success(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
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
    async def test_set_pv_value_denied(self, mock_get_safety: MagicMock) -> None:
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
    async def test_set_pv_value_rate_limited(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
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


class TestSetPvValueFailed:
    """A write that passes the gate but fails at pv_put: audited (FAILED) + re-raised.

    No real PV is touched — pv_put is mocked to raise (AsyncMock side_effect).
    """

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_pv_put_failure_audits_and_reraises(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, write_rate_limit=10)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = PVTimeoutError("put timed out")

        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(PVTimeoutError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        assert "event=FAILED" in caplog.text
        assert "error_code=PV_TIMEOUT" in caplog.text
        # The failed write must NOT also emit a success record.
        assert "event=ALLOW" not in caplog.text
        mock_pv_put.assert_awaited_once_with("TEST:PV", "2.0", 5.0)

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_non_epics_error_audited_as_internal(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A non-EpicsError (a bug below the tool layer) must still be audited,
        # tagged INTERNAL, and re-raised unchanged.
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, write_rate_limit=10)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = ValueError("unexpected boom")

        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(ValueError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        assert "event=FAILED" in caplog.text
        assert "error_code=INTERNAL" in caplog.text

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_failed_write_consumes_rate_token(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        # Contract pin: a write that passes the gate but fails at pv_put STILL
        # consumed its rate-limit token (append happens in check_write_allowed,
        # before pv_put), so the next attempt is rejected before reaching pv_put.
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, write_rate_limit=1)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = PVTimeoutError("put timed out")

        with pytest.raises(PVTimeoutError):
            await _set_pv_value("TEST:PV", "1.0")
        with pytest.raises(RateLimitError):
            await _set_pv_value("TEST:PV", "2.0")
        mock_pv_put.assert_awaited_once()  # second attempt never reached pv_put
