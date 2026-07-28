"""Tests for epics_mcp.tools.monitor."""

from unittest.mock import AsyncMock, patch

from epics_mcp.tools.monitor import _monitor_pv


async def test_monitor_success() -> None:
    mock_events = [
        {"pv_name": "TEST:PV", "value": 1.0},
        {"pv_name": "TEST:PV", "value": 2.0},
    ]
    with patch(
        "epics_mcp.tools.monitor.pv_monitor",
        new_callable=AsyncMock,
        return_value=(mock_events, False),
    ):
        result = await _monitor_pv("TEST:PV", 5.0, 100)

    assert result["pv_name"] == "TEST:PV"
    assert result["events"] == mock_events
    assert result["total_events"] == 2
    assert result["truncated"] is False


async def test_monitor_clamped_duration() -> None:
    """Duration exceeding max_monitor_duration (60.0) should be clamped."""
    mock_monitor = AsyncMock(return_value=([], False))
    with patch("epics_mcp.tools.monitor.pv_monitor", mock_monitor):
        await _monitor_pv("TEST:PV", 999.0, 100)

    # Default max_monitor_duration is 60.0
    call_args = mock_monitor.call_args
    assert call_args[0][1] == 60.0


async def test_monitor_truncated() -> None:
    """_monitor_pv surfaces the service's truncated flag verbatim, the honest over-fetch
    detection lives in pv_monitor (see test_epics_client.py). With the service mocked, the
    tool must pass (events, truncated) straight through."""
    mock_events = [{"pv_name": "TEST:PV", "value": float(i)} for i in range(100)]
    with patch(
        "epics_mcp.tools.monitor.pv_monitor",
        new_callable=AsyncMock,
        return_value=(mock_events, True),
    ):
        result = await _monitor_pv("TEST:PV", 5.0, 100)

    assert result["total_events"] == 100
    assert result["truncated"] is True
