"""Tool functions for writing EPICS PV values with safety checks."""

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.safety import get_safety
from epics_pv_mcp.services.epics_client import pv_get, pv_put


async def _set_pv_value(pv_name: str, value: str, timeout: float = 5.0) -> dict[str, object]:
    """Set PV value with safety checks.

    Performs pre-write safety validation, reads the old value for audit
    purposes, writes the new value, and logs the change.
    """
    safety = get_safety()
    safety.check_write_allowed(pv_name)  # raises PVWriteDeniedError or RateLimitError

    # Read old value for the audit trail. A failure HERE (the pre-read) surfaces as
    # the tool error but is intentionally NOT a PV_WRITE audit event — no write was
    # attempted yet. Only the put below yields ALLOW/FAILED records.
    old = await pv_get(pv_name, timeout)
    old_value = old.get("value")

    # Write new value. Audit a failed put before re-raising so the README's
    # "every write is logged" promise holds for failures too. Broad except
    # (BLE001) is deliberate: any non-EpicsError below the tool layer must still
    # leave a FAILED record. CancelledError is a BaseException and intentionally
    # NOT caught — a cancelled write is not a FAILED write and must propagate.
    try:
        await pv_put(pv_name, value, timeout)
    except Exception as exc:  # broad on purpose: audit ANY failed put, then re-raise unchanged
        error_code = exc.error_code if isinstance(exc, EpicsError) else "INTERNAL"
        safety.audit_write_failed(pv_name, old_value, value, error_code)
        raise

    # Audit the successful write
    safety.audit_write(pv_name, old_value, value)

    return {
        "status": "success",
        "pv_name": pv_name,
        "old_value": old_value,
        "new_value": value,
    }
