"""Tool functions for monitoring EPICS PV value changes over time."""

from epics_pv_mcp.config import get_config
from epics_pv_mcp.services.epics_client import pv_monitor


async def _monitor_pv(name: str, duration: float = 10.0, max_events: int = 100) -> dict:
    """Monitor PV for value changes over a given duration.

    Duration and max_events are clamped to configured maximums by the service layer.
    """
    cfg = get_config()
    # Clamp to configured limits (single point of truth: service layer also clamps)
    effective_duration = min(duration, cfg.max_monitor_duration)
    effective_max_events = min(max_events, cfg.max_monitor_events)

    events = await pv_monitor(name, effective_duration, effective_max_events)

    return {
        "pv_name": name,
        "duration_seconds": effective_duration,
        "events": events,
        "total_events": len(events),
        "truncated": len(events) >= effective_max_events,
    }
