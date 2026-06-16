"""Tests for server-level tool wrappers (EpicsError → ToolError conversion)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from epics_pv_mcp.errors import PVNotFoundError, PVTimeoutError


@pytest.mark.asyncio
async def test_get_pv_value_converts_epics_error_to_tool_error() -> None:
    """Server wrapper must convert EpicsError to ToolError."""
    from epics_pv_mcp.server import get_pv_value

    with (
        patch(
            "epics_pv_mcp.tools.read.pv_get",
            new_callable=AsyncMock,
            side_effect=PVNotFoundError("PV 'MISSING:PV' not found"),
        ),
        pytest.raises(ToolError, match="PV_NOT_FOUND"),
    ):
        await get_pv_value("MISSING:PV")


@pytest.mark.asyncio
async def test_get_pv_value_converts_generic_exception_to_tool_error() -> None:
    """Server wrapper must convert unexpected Exception to ToolError."""
    from epics_pv_mcp.server import get_pv_value

    with (
        patch(
            "epics_pv_mcp.tools.read.pv_get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ),
        pytest.raises(ToolError, match="unexpected"),
    ):
        await get_pv_value("ANY:PV")


@pytest.mark.asyncio
async def test_set_pv_value_converts_write_denied_to_tool_error() -> None:
    """set_pv_value with writes disabled must raise ToolError."""
    from epics_pv_mcp.server import set_pv_value

    with pytest.raises(ToolError, match="PV_WRITE_DENIED"):
        await set_pv_value("TEST:PV", "42")


@pytest.mark.asyncio
async def test_monitor_pv_converts_timeout_to_tool_error() -> None:
    """monitor_pv timeout must raise ToolError."""
    from epics_pv_mcp.server import monitor_pv

    with (
        patch(
            "epics_pv_mcp.tools.monitor.pv_monitor",
            new_callable=AsyncMock,
            side_effect=PVTimeoutError("Timeout monitoring PV 'X'"),
        ),
        pytest.raises(ToolError, match="PV_TIMEOUT"),
    ):
        await monitor_pv("X", duration=1.0)
