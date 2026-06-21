"""Tool functions for retrieving EPICS PV metadata."""

from epics_pv_mcp.services.epics_client import pv_get


async def _get_pv_info(pv_name: str, timeout: float = 5.0) -> dict[str, object]:
    """Get detailed PV metadata.

    Returns value plus best-effort meta-data: alarm (severity/status incl. text
    and message), timestamp, display (units, limits, precision OR format,
    description), control (drive limits), value_alarm (active + HIHI/HIGH/LOW/LOLO
    limits and per-level severities, only when active) and — for enum PVs — the
    enum index/label/choices. Unset (zero-width) limit pairs are omitted.
    """
    result = await pv_get(pv_name, timeout)
    result["status"] = "success"
    return result
