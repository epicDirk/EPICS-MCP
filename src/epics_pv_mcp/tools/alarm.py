"""Tool functions for the Phoebus Alarm Logger (read-only).

Default-disabled: when ``EPICS_MCP_ALARM_URL`` is unset, returns a structured
``enabled: false`` result and makes **no** network call (preserves localhost isolation).
"""

from __future__ import annotations

import asyncio

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG, AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmError

_DISABLED_NOTE = (
    "Phoebus Alarm Logger is disabled. Set EPICS_MCP_ALARM_URL to the logger REST root "
    "(e.g. http://localhost:8081)."
)


async def _is_alarm_configured(
    pv: str,
    config_name: str = DEFAULT_ALARM_CONFIG,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Report whether *pv* has an alarm configuration (Alarm Logger /search/alarm/config)."""
    cfg = get_config()
    if not cfg.alarm_url:
        return {"enabled": False, "pv": pv, "configured": None, "note": _DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = AlarmClient(cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None)
        configured, detail = client.is_alarm_configured(pv, config_name=config_name)
        return {
            "enabled": True,
            "pv": pv,
            "config": config_name,
            "configured": configured,
            "detail": detail,
        }

    try:
        return await asyncio.to_thread(_run)
    except AlarmError as exc:
        raise EpicsConnectionError(f"Alarm Logger: {exc}") from exc
