"""Tool functions for writing EPICS PV values with safety checks."""

import asyncio
import itertools
import logging

from epics_pv_mcp.bounds import check_value_in_bounds
from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsError, PVWriteBoundsError
from epics_pv_mcp.readback import ReadbackVerification, WriteResult, verify_readback
from epics_pv_mcp.safety import get_safety
from epics_pv_mcp.services.epics_client import pv_get, pv_put

logger = logging.getLogger(__name__)

# Monotonic per-process correlation id joining a write's ATTEMPT audit line to its terminal
# ALLOW/FAILED/UNKNOWN_PENDING line. Deterministic + collision-free within a run (no uuid/clock);
# O5 can widen it to a cross-session id via the audit envelope without touching this call site.
_operation_counter = itertools.count(1)


def _next_operation_id() -> str:
    """Return the next audit correlation id (``w1``, ``w2``, ...). ``count.__next__`` is atomic."""
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
    # the tool error but is intentionally NOT a PV_WRITE audit event, no write was
    # attempted yet. Only the put below yields ATTEMPT/ALLOW/FAILED/UNKNOWN_PENDING records.
    old = await pv_get(pv_name, timeout)
    old_value = old.get("value")

    # O2 value bounds (always-on, pre-put). The name/rate gate above allowlists only the PV NAME,
    # never the value. Verify the written value against the record's OWN drive limits (control_t
    # DRVL/DRVH, already on the pre-read `old`), and REFUSE an out-of-range value HERE, before the
    # ATTEMPT/put, so it never reaches the IOC. A record that declares no drive limits (no control
    # block, dropped DRVL==DRVH, or a non-numeric value) is not bounds-checkable → the write
    # proceeds (fail-open) with an honest note in the result. No extra pv_get, this reuses `old`.
    bounds = check_value_in_bounds(value, old)
    if bounds.in_bounds is False:
        safety.audit_bounds_deny(pv_name, value, bounds.limit_low, bounds.limit_high)
        raise PVWriteBoundsError(
            f"Value {value!r} is outside the drive limits "
            f"[{bounds.limit_low}, {bounds.limit_high}] of PV '{pv_name}', write refused.",
            details={
                "pv_name": pv_name,
                "value": value,
                "limit_low": bounds.limit_low,
                "limit_high": bounds.limit_high,
            },
        )

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
    #    the cancel unchanged, never a FAILED (that implies no write), never a blind retry (that
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
    # pending cancel is delivered AT the put await (→ UNKNOWN_PENDING) or not at all, the ALLOW
    # record can never be lost to a late cancel. Keep this tail await-free.
    safety.audit_write(pv_name, old_value, value, operation_id=operation_id)

    # O3 readback verification (always-on). The write already SUCCEEDED and is ALLOW-audited above;
    # read the just-written value back and compare it to what was written, so a wrong / not-landed
    # value becomes LOUD, a structured verdict plus a READBACK audit event, instead of a bare
    # "success" (the "stiller Irrtum" countermeasure). A NEW await is legal ONLY here, after the
    # await-free ALLOW tail: moving it above the audit_write line would break the UNKNOWN_PENDING
    # vs ALLOW ordering. A readback that fails (timeout / unreadable) or yields no live value is
    # "not verifiable", never a tool error, never a mismatch (the write happened regardless); only
    # a genuine value difference is a mismatch. A cancel of the readback await is a BaseException,
    # so it propagates unchanged (never swallowed here, never mislabelled FAILED).
    try:
        readback_raw = await pv_get(pv_name, timeout)
    except Exception:  # noqa: BLE001 (a failed readback is "not verifiable", not a write failure)
        logger.warning("readback failed for %s; write not verifiable", pv_name, exc_info=True)
        verification = ReadbackVerification(
            verified=None, note="not verifiable: readback pv_get failed (timeout/unreadable)"
        )
    else:
        verification = verify_readback(value, readback_raw, get_config().readback_tolerance)

    safety.audit_readback(
        pv_name, value, verification.readback, verification.verified, operation_id=operation_id
    )

    return WriteResult(
        status="success",
        pv_name=pv_name,
        old_value=old_value,
        new_value=value,
        readback=verification.readback,
        verified=verification.verified,
        tolerance=verification.tolerance,
        note=verification.note,
        bounds_note=bounds.note,
    ).model_dump()
