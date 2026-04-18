"""Tool functions for writing EPICS PV values with safety checks."""

from epics_pv_mcp.safety import get_safety
from epics_pv_mcp.services.epics_client import pv_get, pv_put


async def _set_pv_value(pv_name: str, value: str, timeout: float = 5.0) -> dict:
    """Set PV value with safety checks.

    Performs pre-write safety validation, reads the old value for audit
    purposes, writes the new value, and logs the change.
    """
    safety = get_safety()
    safety.check_write_allowed(pv_name)  # raises PVWriteDeniedError or RateLimitError

    # Read old value for audit trail
    old = await pv_get(pv_name, timeout)
    old_value = old.get("value")

    # Write new value
    await pv_put(pv_name, value, timeout)

    # Audit the write
    safety.audit_write(pv_name, old_value, value)

    return {
        "status": "success",
        "pv_name": pv_name,
        "old_value": old_value,
        "new_value": value,
    }
