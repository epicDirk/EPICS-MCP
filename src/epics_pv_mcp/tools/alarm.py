"""MCP adapter for the Phoebus Alarm Logger 'is this PV configured?' query (read-only).

Thin wrapper: the config-gated, off-loop query lives in the services layer
(:func:`epics_pv_mcp.services.checkers.query_alarm_configured`) so
:mod:`~epics_pv_mcp.services.diagnose` reuses it without an upward ``services → tools`` import (M9).
Default-disabled behaviour (no ``EPICS_MCP_ALARM_URL`` → no network call) is enforced there.
"""

from __future__ import annotations

from epics_pv_mcp.services.checkers import (
    AlarmConfiguredResult,
    query_alarm_configured,
    query_alarm_history,
)


async def _is_alarm_configured(
    pv: str,
    config_name: str,
    timeout: float = 5.0,
) -> AlarmConfiguredResult:
    """Report whether *pv* has an alarm configuration (Alarm Logger /search/alarm/config).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_alarm_configured`.
    """
    return await query_alarm_configured(pv, config_name=config_name, timeout=timeout)


async def _get_alarm_history(
    pv: str,
    start: str,
    end: str,
    max_events: int = 100,
    timeout: float = 5.0,
    *,
    root: str | None = None,
    command: str | None = None,
    severity: str | None = None,
    current_severity: str | None = None,
) -> dict[str, object]:
    """Report the alarm state/history of *pv* over ``[start, end]`` (Alarm Logger /search/alarm).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_alarm_history`. The optional
    server-side filters *root*/*command*/*severity*/*current_severity* are passed straight through.
    """
    return await query_alarm_history(
        pv,
        start=start,
        end=end,
        max_events=max_events,
        timeout=timeout,
        root=root,
        command=command,
        severity=severity,
        current_severity=current_severity,
    )
