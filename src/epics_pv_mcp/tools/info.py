"""Tool functions for retrieving EPICS PV metadata."""

from epics_pv_mcp.services.epics_client import pv_get


async def _get_pv_info(pv_name: str, timeout: float = 5.0) -> dict[str, object]:
    """Get detailed PV metadata including alarm status and timestamp."""
    result = await pv_get(pv_name, timeout)
    result["status"] = "success"
    return result
