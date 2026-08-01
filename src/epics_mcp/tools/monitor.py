"""Tool functions for monitoring EPICS PV value changes over time."""

from epics_mcp.config import get_config
from epics_mcp.services.epics_client import pv_monitor


async def _monitor_pv(
    name: str, duration: float = 10.0, max_events: int = 100
) -> dict[str, object]:
    """Monitor PV for value changes over a given duration.

    Duration and max_events are clamped to configured maximums by the service layer.

    Carries ``connection`` so an empty ``events`` list is readable on its own (QA-31): zero
    events used to mean either "quiet PV" or "no such PV", and ``get_pv_value`` answered the
    second case with a ``PVTimeoutError``, so the two tools contradicted each other.
    """
    cfg = get_config()
    # Clamp to configured limits (single point of truth: service layer also clamps)
    effective_duration = min(duration, cfg.max_monitor_duration)
    effective_max_events = min(max_events, cfg.max_monitor_events)

    outcome = await pv_monitor(name, effective_duration, effective_max_events)

    result: dict[str, object] = {
        "pv_name": name,
        "duration_seconds": effective_duration,
        "events": outcome.events,
        "total_events": len(outcome.events),
        "truncated": outcome.truncated,
        "connection": outcome.connection,
    }
    # Omitted when there is nothing to explain, so a healthy run stays as terse as it was.
    if outcome.connection_detail is not None:
        result["connection_detail"] = outcome.connection_detail
    return result
