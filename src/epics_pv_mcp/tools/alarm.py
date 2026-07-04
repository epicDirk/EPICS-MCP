"""MCP adapter for the Phoebus Alarm Logger 'is this PV configured?' query (read-only).

Thin wrapper: the config-gated, off-loop query lives in the services layer
(:func:`epics_pv_mcp.services.checkers.query_alarm_configured`) so
:mod:`~epics_pv_mcp.services.diagnose` reuses it without an upward ``services → tools`` import (M9).
Default-disabled behaviour (no ``EPICS_MCP_ALARM_URL`` → no network call) is enforced there.
"""

from __future__ import annotations

from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG
from epics_pv_mcp.services.checkers import query_alarm_configured


async def _is_alarm_configured(
    pv: str,
    config_name: str = DEFAULT_ALARM_CONFIG,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Report whether *pv* has an alarm configuration (Alarm Logger /search/alarm/config).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_alarm_configured`.
    """
    return await query_alarm_configured(pv, config_name=config_name, timeout=timeout)
