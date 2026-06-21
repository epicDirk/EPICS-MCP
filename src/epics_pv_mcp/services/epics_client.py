"""p4p wrapper — singleton Context with async public API.

p4p is synchronous; every blocking call is dispatched via ``asyncio.to_thread``
so the FastMCP event loop is never blocked.
"""

import asyncio
import atexit
import logging
import threading
from collections.abc import Callable

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
                    # ein Monitor-Callback darf den Worker-Thread nie crashen; value=None
                    # statt str(value) — der Wrapper-str() ergäbe ctime-Müll (s. _format_value).
                    logger.debug("monitor format failed for PV %s", name, exc_info=True)
                    collected.append({"pv_name": name, "value": None})

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


# A field-mapping spec: (p4p attribute, output key, cast). ``Callable[..., object]`` is
# the only mypy-strict-clean annotation — bare ``Callable`` needs type args, and
# ``Callable[[object], object]`` rejects the ``float``/``int`` casts (their __init__ is
# not object->object).
_FieldSpec = list[tuple[str, str, Callable[..., object]]]

_DISPLAY_SPEC: _FieldSpec = [
    ("units", "units", str),
    ("limitLow", "limit_low", float),
    ("limitHigh", "limit_high", float),
    ("precision", "precision", int),
    ("format", "format", str),  # form=False IOCs carry `format` (e.g. "%.3f") instead of precision
    ("description", "description", str),
]
_CONTROL_SPEC: _FieldSpec = [
    ("limitLow", "limit_low", float),
    ("limitHigh", "limit_high", float),
    ("minStep", "min_step", float),
]
_VALUE_ALARM_SPEC: _FieldSpec = [
    ("lowAlarmLimit", "low_alarm", float),
    ("lowWarningLimit", "low_warning", float),
    ("highWarningLimit", "high_warning", float),
    ("highAlarmLimit", "high_alarm", float),
    ("lowAlarmSeverity", "low_alarm_severity", int),
    ("lowWarningSeverity", "low_warning_severity", int),
    ("highWarningSeverity", "high_warning_severity", int),
    ("highAlarmSeverity", "high_alarm_severity", int),
]


def _collect(struct: object, spec: _FieldSpec) -> dict[str, object]:
    """Map present p4p struct fields to output keys via their cast.

    A single malformed field (e.g. an unset limit serialised as ``None``) is skipped,
    never aborting the whole block — that is the per-field robustness guard.
    """
    out: dict[str, object] = {}
    for attr, key, cast in spec:
        if not hasattr(struct, attr):
            continue
        try:
            out[key] = cast(getattr(struct, attr))
        except (TypeError, ValueError):
            continue
    return out


def _drop_degenerate_limits(d: dict[str, object]) -> None:
    """A zero-width range (``limit_low == limit_high``) is an unset pair — drop both.

    EPICS display/control limits default to ``0.0/0.0`` when unconfigured, which would
    otherwise read as a real ``[0, 0]`` engineering range. control/display carry no
    ``active`` flag, so equal bounds are the deterministic "unset" signal.
    """
    if "limit_low" in d and "limit_high" in d and d["limit_low"] == d["limit_high"]:
        del d["limit_low"]
        del d["limit_high"]


def _extract_value(raw: object) -> tuple[object, dict[str, object] | None]:
    """Return ``(value, enum_or_none)``. For NTEnum the value stays the numeric index."""
    val_field = getattr(raw, "value", raw)
    choices = getattr(val_field, "choices", None)
    if choices is not None:
        # NTEnum: the value field is a struct {index, choices}.
        index = int(getattr(val_field, "index", 0))
        labels = [str(c) for c in choices]
        label = labels[index] if 0 <= index < len(labels) else None
        return index, {"index": index, "label": label, "choices": labels}
    # numpy array -> list (real unwrapped scalars are already plain float/int/str).
    tolist = getattr(val_field, "tolist", None)
    if callable(tolist):
        val_field = tolist()
    return val_field, None


def _extract_alarm(raw: object) -> dict[str, object] | None:
    """Alarm: severity/status as code + human-readable text, plus the alarm message."""
    alarm = getattr(raw, "alarm", None)
    if alarm is None:
        return None
    severity = int(getattr(alarm, "severity", 0))
    status = int(getattr(alarm, "status", 0))
    out: dict[str, object] = {
        "severity": severity,
        "severity_text": _SEVERITY_TEXT.get(severity, str(severity)),
        "status": status,
        "status_text": _ALARM_STATUS_TEXT.get(status, str(status)),
    }
    # On real p4p the message field is always present (often ""); a fake may omit it.
    message = getattr(alarm, "message", None)
    if message is not None:
        out["message"] = str(message)
    return out


def _extract_timestamp(raw: object) -> dict[str, object] | None:
    ts = getattr(raw, "timeStamp", None)
    if ts is None:
        return None
    return {
        "seconds": int(getattr(ts, "secondsPastEpoch", 0)),
        "nanoseconds": int(getattr(ts, "nanoseconds", 0)),
    }


def _extract_display(raw: object) -> dict[str, object] | None:
    disp = getattr(raw, "display", None)
    if disp is None:
        return None
    out = _collect(disp, _DISPLAY_SPEC)
    _drop_degenerate_limits(out)
    return out or None


def _extract_control(raw: object) -> dict[str, object] | None:
    ctrl = getattr(raw, "control", None)
    if ctrl is None:
        return None
    out = _collect(ctrl, _CONTROL_SPEC)
    _drop_degenerate_limits(out)
    return out or None


def _extract_value_alarm(raw: object) -> dict[str, object] | None:
    """value_alarm gated on ``active``: surface limits/severities only when alarming is on.

    Unconfigured valueAlarm structs default to ``active=False`` with ``0.0`` limits, which
    would otherwise look like real HIHI/HIGH/LOW/LOLO thresholds. A valueAlarm struct that
    lacks the ``active`` field (non-NT-conformant producer) is treated conservatively as
    inactive — limits are not shown.
    """
    va = getattr(raw, "valueAlarm", None)
    if va is None:
        return None
    active = bool(getattr(va, "active", False))
    out: dict[str, object] = {"active": active}
    if active:
        out.update(_collect(va, _VALUE_ALARM_SPEC))
    return out


# Metadata blocks, each extracted independently so a malformed one cannot corrupt the
# value or the other blocks (per-block robustness).
_BLOCK_EXTRACTORS: list[tuple[str, Callable[[object], dict[str, object] | None]]] = [
    ("alarm", _extract_alarm),
    ("timestamp", _extract_timestamp),
    ("display", _extract_display),
    ("control", _extract_control),
    ("value_alarm", _extract_value_alarm),
]


def _format_value(pv_name: str, value: object) -> dict[str, object]:
    """Convert a p4p value into a plain, JSON-serialisable dict.

    p4p's ``Context`` unwraps Normative Types by default, so ``ctxt.get`` returns
    value-wrappers (``ntfloat``/``ntint``/``ntenum``/…) whose meta-data lives on the
    underlying ``p4p.Value`` exposed via ``.raw`` — NOT directly on the wrapper. Every
    field is routed through ``raw`` (``getattr(value, "raw", value)`` also handles the
    un-unwrapped ``nt=False`` case).

    Surfaced fields (all best-effort — absent on records that do not define them):
    ``value`` (scalar/array, or enum index — DBR_CHAR waveforms come back as int lists),
    ``enum`` (index/label/choices for NTEnum), ``alarm`` (severity/status code + text +
    message), ``timestamp`` (seconds/nanoseconds), ``display`` (units, precision OR format,
    description, display limits), ``control`` (drive limits, min_step), ``value_alarm``
    (``active`` + the HIHI/HIGH/LOW/LOLO limits and per-level severities, only when active).
    Display/control limit pairs that are equal (zero-width = unset) are omitted.

    Robustness: each block is extracted independently; a malformed block is skipped (logged
    at debug) and never corrupts the value or the other blocks. The function never raises.
    """
    result: dict[str, object] = {"pv_name": pv_name, "value": None}
    # The unwrapped wrapper exposes the raw p4p.Value under `.raw`; a raw Value
    # (nt=False) has no `.raw` and is used directly.
    raw = getattr(value, "raw", value)

    try:
        result["value"], enum = _extract_value(raw)
        if enum is not None:
            result["enum"] = enum
    except Exception:  # noqa: BLE001
        # Honest fallback: value stays None (NEVER str(value) — the wrapper's __str__
        # prepends a ctime() and would emit garbage like "Thu Jan  1 1970 4.2").
        logger.debug("value extraction failed for PV %s", pv_name, exc_info=True)

    for key, extractor in _BLOCK_EXTRACTORS:
        try:
            block = extractor(raw)
        except Exception:  # noqa: BLE001
            logger.debug("%s extraction failed for PV %s", key, pv_name, exc_info=True)
            continue
        if block:
            result[key] = block

    return result
