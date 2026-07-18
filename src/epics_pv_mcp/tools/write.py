"""Tool functions for writing EPICS PV values with safety checks."""

import asyncio
import itertools
import logging

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.safety import get_safety
from epics_pv_mcp.services.epics_client import pv_get, pv_put

logger = logging.getLogger(__name__)

# Monotonic per-process correlation id joining a write's ATTEMPT audit line to its terminal
# ALLOW/FAILED/UNKNOWN_PENDING line. Deterministic + collision-free within a run (no uuid/clock);
# O5 can widen it to a cross-session id via the audit envelope without touching this call site.
_operation_counter = itertools.count(1)


def _next_operation_id() -> str:
    """Return the next audit correlation id (``w1``, ``w2``, …). ``count.__next__`` is atomic."""
    return f"w{next(_operation_counter)}"


async def _set_pv_value(
    pv_name: str, value: str, timeout: float | None = None
) -> dict[str, object]:
    """Set PV value with safety checks.

    Performs pre-write safety validation, reads the old value for audit
    purposes, writes the new value, and logs the change.
    """
    safety = get_safety()
    safety.check_write_allowed(pv_name)  # raises PVWriteDeniedError or RateLimitError

    # Read old value for the audit trail. A failure HERE (the pre-read) surfaces as
    # the tool error but is intentionally NOT a PV_WRITE audit event — no write was
    # attempted yet. Only the put below yields ATTEMPT/ALLOW/FAILED/UNKNOWN_PENDING records.
    old = await pv_get(pv_name, timeout)
    old_value = old.get("value")

    # Durable ATTEMPT record BEFORE the I/O (S24/N01). Minted + emitted AFTER the pre-read so a
    # cancelled/failed pre-read stays a bare tool error, and just before the put so a write that is
    # cancelled mid-flight (below) always has a correlating forensic anchor.
    operation_id = _next_operation_id()
    safety.audit_write_attempt(pv_name, value, operation_id)

    # Write new value.
    #  - except CancelledError (a BaseException, so the broad ``except Exception`` below does NOT
    #    catch it): a cancel does NOT stop the ``asyncio.to_thread`` worker running the p4p put, so
    #    the value may still land at the IOC AFTER the caller sees the cancel (S24/N01, "der stille
    #    Irrtum"). Record UNKNOWN_PENDING so the ATTEMPT is not left dangling, then ALWAYS re-raise
    #    the cancel unchanged — never a FAILED (that implies no write), never a blind retry (that
    #    could double-write). The emit is guarded so a broken audit sink can never swallow or
    #    replace the CancelledError. (Non-cancel BaseExceptions = process shutdown, propagate.)
    #  - except Exception (broad, deliberate): any non-EpicsError below the tool layer still leaves
    #    a FAILED record, then re-raises unchanged (the README "every write is logged" promise).
    try:
        await pv_put(pv_name, value, timeout)
    except asyncio.CancelledError:
        try:
            safety.audit_write_unknown(pv_name, old_value, value, operation_id)
        except Exception:  # the audit emit must NEVER mask or replace the cancellation
            logger.error("audit UNKNOWN_PENDING emit failed for %s", pv_name, exc_info=True)
        raise
    except Exception as exc:  # broad on purpose: audit ANY failed put, then re-raise unchanged
        error_code = exc.error_code if isinstance(exc, EpicsError) else "INTERNAL"
        safety.audit_write_failed(pv_name, old_value, value, error_code, operation_id=operation_id)
        raise

    # Audit the successful write. There is NO await between pv_put returning and this line, so a
    # pending cancel is delivered AT the put await (→ UNKNOWN_PENDING) or not at all — the ALLOW
    # record can never be lost to a late cancel. Keep this tail await-free.
    safety.audit_write(pv_name, old_value, value, operation_id=operation_id)

    return {
        "status": "success",
        "pv_name": pv_name,
        "old_value": old_value,
        "new_value": value,
    }
