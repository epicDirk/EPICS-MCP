"""Tool functions for reading EPICS PV values (single and batch)."""

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.epics_client import pv_get, pv_get_batch


async def _get_pv_value(pv_name: str, timeout: float = 5.0) -> dict[str, object]:
    """Get single PV value."""
    return await pv_get(pv_name, timeout)


async def _get_pvs(names: list[str], timeout: float = 5.0) -> dict[str, object]:
    """Batch read up to max_batch_size PVs."""
    cfg = get_config()
    if not names:
        raise EpicsError("PV list cannot be empty", error_code="INVALID_INPUT")
    if len(names) > cfg.max_batch_size:
        raise EpicsError(
            f"Batch size {len(names)} exceeds maximum {cfg.max_batch_size}",
            error_code="BATCH_TOO_LARGE",
        )
    return await pv_get_batch(names, timeout)
