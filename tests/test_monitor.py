"""Tests for epics_mcp.tools.monitor."""

from unittest.mock import AsyncMock, patch

from epics_mcp.services.epics_client import MonitorOutcome
from epics_mcp.tools.monitor import _monitor_pv


async def test_monitor_success() -> None:
    mock_events = [
        {"pv_name": "TEST:PV", "value": 1.0},
        {"pv_name": "TEST:PV", "value": 2.0},
    ]
    with patch(
        "epics_mcp.tools.monitor.pv_monitor",
        new_callable=AsyncMock,
        return_value=MonitorOutcome(mock_events, False, "connected", None),
    ):
        result = await _monitor_pv("TEST:PV", 5.0, 100)

    assert result["pv_name"] == "TEST:PV"
    assert result["events"] == mock_events
    assert result["total_events"] == 2
    assert result["truncated"] is False
    assert result["connection"] == "connected"


async def test_monitor_clamped_duration() -> None:
    """Duration exceeding max_monitor_duration (60.0) should be clamped."""
    mock_monitor = AsyncMock(return_value=MonitorOutcome([], False, "connected", None))
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
        return_value=MonitorOutcome(mock_events, True, "connected", None),
    ):
        result = await _monitor_pv("TEST:PV", 5.0, 100)

    assert result["total_events"] == 100
    assert result["truncated"] is True


# --- QA-31: an empty result explains itself ---


async def test_never_connected_pv_says_so_instead_of_looking_quiet() -> None:
    """THE defect QA-31 names. Before this, a PV that was never there answered exactly like a
    healthy but quiet one, while ``get_pv_value`` raised PVTimeoutError on the same PV: two
    tools contradicting each other about one fact. The detail line is asserted too, because a
    bare enum value still asks the reader to know what it implies for ``total_events``."""
    with patch(
        "epics_mcp.tools.monitor.pv_monitor",
        new_callable=AsyncMock,
        return_value=MonitorOutcome([], False, "disconnected", "the channel never connected"),
    ):
        result = await _monitor_pv("NO:SUCH:PV", 5.0, 100)

    assert result["total_events"] == 0
    assert result["connection"] == "disconnected"
    assert result["connection_detail"] == "the channel never connected"


async def test_a_healthy_run_carries_no_detail_line() -> None:
    """The complementary control. The detail key is OMITTED rather than set to None, so the
    common case stays as terse as it was and the key's presence is itself a signal."""
    with patch(
        "epics_mcp.tools.monitor.pv_monitor",
        new_callable=AsyncMock,
        return_value=MonitorOutcome(
            [{"pv_name": "TEST:PV", "value": 1.0}], False, "connected", None
        ),
    ):
        result = await _monitor_pv("TEST:PV", 5.0, 100)

    assert result["connection"] == "connected"
    assert "connection_detail" not in result
