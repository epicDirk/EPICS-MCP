"""Tool functions for discovering EPICS PVs by name or pattern."""

from epics_pv_mcp.errors import EpicsConnectionError, PVNotFoundError, PVTimeoutError
from epics_pv_mcp.services.epics_client import pv_get


async def _discover_pvs(pattern: str, timeout: float = 5.0) -> dict[str, object]:
    """Discover PVs matching a pattern.

    Limitations: p4p does not have a universal PV discovery mechanism.
    - Concrete PV names: tries to connect and returns status.
    - Wildcard patterns: not supported without ChannelFinder/OLOG infrastructure.

    For wildcard discovery, consider integrating with EPICS ChannelFinder or OLOG.
    """
    # Check if pattern contains wildcards
    if any(c in pattern for c in "*?[]"):
        return {
            "pattern": pattern,
            "pvs": [],
            "total": 0,
            "note": (
                "Wildcard PV discovery requires ChannelFinder or similar "
                "infrastructure. p4p alone cannot enumerate PVs by pattern. "
                "Try a concrete PV name instead."
            ),
        }

    # Treat as concrete PV name — try to connect
    try:
        result = await pv_get(pattern, timeout)
        return {
            "pattern": pattern,
            "pvs": [{"pv_name": pattern, "status": "found", "value": result.get("value")}],
            "total": 1,
        }
    except (PVTimeoutError, PVNotFoundError, EpicsConnectionError):
        return {
            "pattern": pattern,
            "pvs": [{"pv_name": pattern, "status": "not_found"}],
            "total": 0,
        }
