"""p4p wrapper — singleton Context with async public API.

p4p is synchronous; every blocking call is dispatched via ``asyncio.to_thread``
so the FastMCP event loop is never blocked.
"""

import asyncio
import atexit
import logging
import threading

from p4p.client.thread import Context

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import (
    EpicsConnectionError,
    EpicsError,
    PVNotFoundError,
    PVTimeoutError,
)

logger = logging.getLogger(__name__)

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


def _classify_p4p_error(name: str, exc: BaseException, *, action: str) -> EpicsError:
    """Klassifiziere eine generische (Nicht-Timeout-)p4p-Exception.

    p4p hat keinen eigenen „PV not found"-Exceptiontyp — dieser Subfall ist nur
    an der Fehlermeldung erkennbar. Diese eine Stelle ersetzt die zuvor in
    pv_get / pv_put / pv_monitor wortgleich duplizierte String-Klassifikation
    (Low-Level raised, EINE Schicht fängt + übersetzt — QUALITY-STANDARD §1).
    """
    msg = str(exc).lower()
    if "not found" in msg or "search" in msg:
        return PVNotFoundError(f"PV '{name}' not found", details={"pv_name": name})
    return EpicsConnectionError(f"Error {action} PV '{name}': {exc}", details={"pv_name": name})


async def pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
    """Get a single PV value. Returns a formatted dict."""
    cfg = get_config()
    timeout = timeout if timeout is not None else cfg.default_timeout
    ctxt = get_context()
    try:
        value = await asyncio.to_thread(ctxt.get, name, timeout=timeout)
        return _format_value(name, value)
    except TimeoutError as e:
        raise PVTimeoutError(
            f"Timeout getting PV '{name}' after {timeout}s",
            details={"pv_name": name, "timeout": timeout},
        ) from e
    except Exception as e:
        raise _classify_p4p_error(name, e, action="accessing") from e


async def pv_get_batch(names: list[str], timeout: float | None = None) -> dict[str, object]:
    """Batch-get PVs. Returns ``{"results": [...], "errors": [...]}``."""
    cfg = get_config()
    timeout = timeout if timeout is not None else cfg.default_timeout

    if len(names) > cfg.max_batch_size:
        raise EpicsError(
            f"Batch size {len(names)} exceeds maximum {cfg.max_batch_size}",
            error_code="BATCH_TOO_LARGE",
        )

    ctxt = get_context()
    results: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    # Try native batch get first
    try:
        values = await asyncio.to_thread(ctxt.get, names, timeout=timeout)
        for name, value in zip(names, values, strict=False):
            try:
                results.append(_format_value(name, value))
            except Exception as exc:  # noqa: BLE001
                # ein kaputter Einzelwert darf den Batch nicht abbrechen
                errors.append({"pv_name": name, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        # Batch fehlgeschlagen -> Einzelabfrage-Fallback. Die Wurzel des
        # Batch-Fehlers nicht still verschlucken (für die Diagnose loggen); die
        # Einzelabfragen liefern danach je PV einen genauen Fehler.
        logger.debug("Batch get failed, falling back to individual gets: %s", exc)
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
    except TimeoutError as e:
        raise PVTimeoutError(
            f"Timeout writing PV '{name}' after {timeout}s",
            details={"pv_name": name, "timeout": timeout},
        ) from e
    except Exception as e:
        raise _classify_p4p_error(name, e, action="writing") from e


async def pv_monitor(
    name: str,
    duration: float | None = None,
    max_events: int | None = None,
) -> list[dict[str, object]]:
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
    collected: list[dict[str, object]] = []
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
                except Exception:  # noqa: BLE001
                    # ein Monitor-Callback darf den Worker-Thread nie crashen
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
        except Exception as exc:  # noqa: BLE001
            # jeden Monitor-Fehler übersetzen + sammeln (eine Schicht fängt)
            error_holder.append(_classify_p4p_error(name, exc, action="monitoring"))
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

# EPICS-Normative-Type Alarm-Enums (pvData-Standard) — Integer -> menschenlesbar.
# Severity = Schweregrad des Alarms; Status = NT-Kategorie der Quelle (NICHT die
# CA-STAT-Detail-Liste wie HIHI/HIGH — der Klartext dazu steht in alarm.message).
_SEVERITY_TEXT: dict[int, str] = {
    0: "NO_ALARM",
    1: "MINOR",
    2: "MAJOR",
    3: "INVALID",
    4: "UNDEFINED",
}
_ALARM_STATUS_TEXT: dict[int, str] = {
    0: "NONE",
    1: "DEVICE",
    2: "DRIVER",
    3: "RECORD",
    4: "DB",
    5: "CONF",
    6: "UNDEFINED",
    7: "CLIENT",
}


def _format_value(pv_name: str, value: object) -> dict[str, object]:
    """Convert a p4p value into a plain, JSON-serialisable dict.

    p4p's ``Context`` unwraps Normative Types by default, so ``ctxt.get`` returns
    value-wrappers (``ntfloat``/``ntint``/``ntenum``/…) whose meta-data lives on the
    underlying ``p4p.Value`` exposed via ``.raw`` — NOT directly on the wrapper.
    We therefore route every field through ``raw`` (``getattr(value, "raw", value)``
    also handles the un-unwrapped case if a Context is ever built with ``nt=False``).

    Surfaced fields (all best-effort — absent on records that do not define them):
    ``value`` (scalar/array, or enum index), ``enum`` (index/label/choices for NTEnum),
    ``alarm`` (severity + severity_text, status + status_text, message),
    ``timestamp`` (seconds/nanoseconds), ``display`` (units/limits/precision/description),
    ``control`` (limits/min_step), ``value_alarm`` (low/high alarm + warning limits).
    """
    result: dict[str, object] = {"pv_name": pv_name, "value": None}
    try:
        # The unwrapped wrapper exposes the raw p4p.Value under `.raw`; a raw Value
        # (nt=False) has no `.raw` and is used directly.
        raw = getattr(value, "raw", value)
        val_field = raw.value if hasattr(raw, "value") else raw

        if hasattr(val_field, "choices"):
            # NTEnum: the value field is a struct {index, choices}.
            index = int(val_field.index) if hasattr(val_field, "index") else 0
            choices = [str(c) for c in val_field.choices]
            label = choices[index] if 0 <= index < len(choices) else None
            result["value"] = index  # back-compat: `value` stays a number
            result["enum"] = {"index": index, "label": label, "choices": choices}
        else:
            # numpy array/scalar -> native Python. ``tolist`` first: it works for
            # BOTH (scalar -> Python scalar, array -> list), whereas ``item`` raises
            # on multi-element arrays. ``item`` stays as a fallback for scalar-only
            # objects that lack ``tolist``.
            if hasattr(val_field, "tolist"):
                val_field = val_field.tolist()
            elif hasattr(val_field, "item"):
                val_field = val_field.item()
            result["value"] = val_field

        # Alarm metadata (severity/status + human-readable text + message).
        if hasattr(raw, "alarm"):
            alarm = raw.alarm
            severity = int(alarm.severity) if hasattr(alarm, "severity") else 0
            status = int(alarm.status) if hasattr(alarm, "status") else 0
            alarm_dict: dict[str, object] = {
                "severity": severity,
                "severity_text": _SEVERITY_TEXT.get(severity, str(severity)),
                "status": status,
                "status_text": _ALARM_STATUS_TEXT.get(status, str(status)),
            }
            if hasattr(alarm, "message"):
                alarm_dict["message"] = str(alarm.message)
            result["alarm"] = alarm_dict

        # Timestamp metadata.
        if hasattr(raw, "timeStamp"):
            ts = raw.timeStamp
            result["timestamp"] = {
                "seconds": (int(ts.secondsPastEpoch) if hasattr(ts, "secondsPastEpoch") else 0),
                "nanoseconds": (int(ts.nanoseconds) if hasattr(ts, "nanoseconds") else 0),
            }

        # Display metadata (units, display limits, precision, description).
        if hasattr(raw, "display"):
            disp = raw.display
            display_dict: dict[str, object] = {}
            if hasattr(disp, "units"):
                display_dict["units"] = str(disp.units)
            if hasattr(disp, "limitLow"):
                display_dict["limit_low"] = float(disp.limitLow)
            if hasattr(disp, "limitHigh"):
                display_dict["limit_high"] = float(disp.limitHigh)
            if hasattr(disp, "precision"):
                display_dict["precision"] = int(disp.precision)
            if hasattr(disp, "description"):
                display_dict["description"] = str(disp.description)
            if display_dict:
                result["display"] = display_dict

        # Control metadata (drive limits + minimum step).
        if hasattr(raw, "control"):
            ctrl = raw.control
            control_dict: dict[str, object] = {}
            if hasattr(ctrl, "limitLow"):
                control_dict["limit_low"] = float(ctrl.limitLow)
            if hasattr(ctrl, "limitHigh"):
                control_dict["limit_high"] = float(ctrl.limitHigh)
            if hasattr(ctrl, "minStep"):
                control_dict["min_step"] = float(ctrl.minStep)
            if control_dict:
                result["control"] = control_dict

        # Value-alarm metadata (the HIHI/HIGH/LOW/LOLO limits).
        if hasattr(raw, "valueAlarm"):
            va = raw.valueAlarm
            value_alarm_dict: dict[str, object] = {}
            if hasattr(va, "lowAlarmLimit"):
                value_alarm_dict["low_alarm"] = float(va.lowAlarmLimit)
            if hasattr(va, "lowWarningLimit"):
                value_alarm_dict["low_warning"] = float(va.lowWarningLimit)
            if hasattr(va, "highWarningLimit"):
                value_alarm_dict["high_warning"] = float(va.highWarningLimit)
            if hasattr(va, "highAlarmLimit"):
                value_alarm_dict["high_alarm"] = float(va.highAlarmLimit)
            if value_alarm_dict:
                result["value_alarm"] = value_alarm_dict

    except Exception:  # noqa: BLE001
        # _format_value MUSS jeden p4p-Wert robust in ein dict wandeln und darf
        # nie crashen — Last resort: stringify.
        result["value"] = str(value)

    return result
