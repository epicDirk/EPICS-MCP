"""p4p wrapper — singleton Context with async public API.

p4p is synchronous; every blocking call is dispatched via ``asyncio.to_thread``
so the FastMCP event loop is never blocked.
"""

import asyncio
import atexit
import threading

from p4p.client.thread import Context

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import (
    EpicsConnectionError,
    EpicsError,
    PVNotFoundError,
    PVTimeoutError,
)

# ---------------------------------------------------------------------------
# Singleton p4p Context
# ---------------------------------------------------------------------------

_context: Context | None = None
_lock = threading.Lock()


def get_context() -> Context:
    """Return (or create) the process-wide p4p ``Context``."""
    global _context
    with _lock:
        if _context is None:
            cfg = get_config()
            _context = Context(cfg.provider)
            atexit.register(_cleanup)
        return _context


def _cleanup() -> None:
    global _context
    if _context is not None:
        _context.close()
        _context = None


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------


async def pv_get(name: str, timeout: float | None = None) -> dict:
    """Get a single PV value. Returns a formatted dict."""
    cfg = get_config()
    timeout = timeout if timeout is not None else cfg.default_timeout
    ctxt = get_context()
    try:
        value = await asyncio.to_thread(ctxt.get, name, timeout=timeout)
        return _format_value(name, value)
    except TimeoutError:
        raise PVTimeoutError(
            f"Timeout getting PV '{name}' after {timeout}s",
            details={"pv_name": name, "timeout": timeout},
        )
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "search" in msg:
            raise PVNotFoundError(
                f"PV '{name}' not found",
                details={"pv_name": name},
            )
        raise EpicsConnectionError(
            f"Error accessing PV '{name}': {e}",
            details={"pv_name": name},
        )


async def pv_get_batch(names: list[str], timeout: float | None = None) -> dict:
    """Batch-get PVs. Returns ``{"results": [...], "errors": [...]}``."""
    cfg = get_config()
    timeout = timeout if timeout is not None else cfg.default_timeout

    if len(names) > cfg.max_batch_size:
        raise EpicsError(
            f"Batch size {len(names)} exceeds maximum {cfg.max_batch_size}",
            error_code="BATCH_TOO_LARGE",
        )

    ctxt = get_context()
    results: list[dict] = []
    errors: list[dict] = []

    # Try native batch get first
    try:
        values = await asyncio.to_thread(ctxt.get, names, timeout=timeout)
        for name, value in zip(names, values):
            try:
                results.append(_format_value(name, value))
            except Exception as exc:
                errors.append({"pv_name": name, "error": str(exc)})
    except Exception:
        # Batch failed — fall back to individual gets
        for name in names:
            try:
                result = await pv_get(name, timeout=timeout)
                results.append(result)
            except PVNotFoundError:
                errors.append({"pv_name": name, "error": f"PV '{name}' not found"})
            except PVTimeoutError:
                errors.append({"pv_name": name, "error": f"Timeout getting PV '{name}'"})
            except EpicsConnectionError as exc:
                errors.append({"pv_name": name, "error": str(exc)})

    return {"results": results, "errors": errors}


async def pv_put(name: str, value: object, timeout: float | None = None) -> None:
    """Put a single PV value."""
    cfg = get_config()
    timeout = timeout if timeout is not None else cfg.default_timeout
    ctxt = get_context()
    try:
        await asyncio.to_thread(ctxt.put, name, value, timeout=timeout)
    except TimeoutError:
        raise PVTimeoutError(
            f"Timeout writing PV '{name}' after {timeout}s",
            details={"pv_name": name, "timeout": timeout},
        )
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "search" in msg:
            raise PVNotFoundError(
                f"PV '{name}' not found",
                details={"pv_name": name},
            )
        raise EpicsConnectionError(
            f"Error writing PV '{name}': {e}",
            details={"pv_name": name},
        )


async def pv_monitor(
    name: str,
    duration: float | None = None,
    max_events: int | None = None,
) -> list[dict]:
    """Monitor a PV for *duration* seconds, collecting up to *max_events*.

    Runs the p4p subscription in a background thread and uses
    ``threading.Event`` for clean cancellation.
    """
    cfg = get_config()
    duration = duration if duration is not None else cfg.max_monitor_duration
    max_events = max_events if max_events is not None else cfg.max_monitor_events

    # Clamp to configured maximums
    duration = min(duration, cfg.max_monitor_duration)
    max_events = min(max_events, cfg.max_monitor_events)

    ctxt = get_context()
    collected: list[dict] = []
    lock = threading.Lock()
    stop_event = threading.Event()
    error_holder: list[Exception] = []

    def _monitor_thread() -> None:
        """Run in a worker thread — p4p monitor is synchronous."""

        def _on_value(value: object) -> None:
            if stop_event.is_set():
                return
            with lock:
                if len(collected) >= max_events:
                    stop_event.set()
                    return
                try:
                    collected.append(_format_value(name, value))
                except Exception:
                    collected.append({"pv_name": name, "value": str(value)})

        sub = None
        try:
            sub = ctxt.monitor(name, _on_value)
            stop_event.wait(timeout=duration)
        except TimeoutError:
            error_holder.append(
                PVTimeoutError(
                    f"Timeout monitoring PV '{name}'",
                    details={"pv_name": name},
                )
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "search" in msg:
                error_holder.append(
                    PVNotFoundError(
                        f"PV '{name}' not found",
                        details={"pv_name": name},
                    )
                )
            else:
                error_holder.append(
                    EpicsConnectionError(
                        f"Error monitoring PV '{name}': {exc}",
                        details={"pv_name": name},
                    )
                )
        finally:
            if sub is not None:
                sub.close()

    await asyncio.to_thread(_monitor_thread)

    if error_holder:
        raise error_holder[0]

    return collected


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------


def _format_value(pv_name: str, value: object) -> dict:
    """Convert a p4p ``Value`` object into a plain dict.

    Handles NTScalar, NTTable, and other normative types.  Numpy scalars
    are converted to native Python types.
    """
    result: dict = {"pv_name": pv_name, "value": None}
    try:
        # Extract the raw scalar / array
        raw = value.value if hasattr(value, "value") else value
        # Convert numpy scalar to Python native
        if hasattr(raw, "item"):
            raw = raw.item()
        # Convert numpy arrays to Python lists
        elif hasattr(raw, "tolist"):
            raw = raw.tolist()
        result["value"] = raw

        # Alarm metadata
        if hasattr(value, "alarm"):
            alarm = value.alarm
            result["alarm"] = {
                "severity": (int(alarm.severity) if hasattr(alarm, "severity") else 0),
                "status": int(alarm.status) if hasattr(alarm, "status") else 0,
            }

        # Timestamp metadata
        if hasattr(value, "timeStamp"):
            ts = value.timeStamp
            result["timestamp"] = {
                "seconds": (int(ts.secondsPastEpoch) if hasattr(ts, "secondsPastEpoch") else 0),
                "nanoseconds": (int(ts.nanoseconds) if hasattr(ts, "nanoseconds") else 0),
            }

        # Display metadata (units, limits)
        if hasattr(value, "display"):
            disp = value.display
            display_dict: dict = {}
            if hasattr(disp, "units"):
                display_dict["units"] = str(disp.units)
            if hasattr(disp, "limitLow"):
                display_dict["limit_low"] = float(disp.limitLow)
            if hasattr(disp, "limitHigh"):
                display_dict["limit_high"] = float(disp.limitHigh)
            if display_dict:
                result["display"] = display_dict

    except Exception:
        # Last resort: stringify
        result["value"] = str(value)

    return result
