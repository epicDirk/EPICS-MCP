"""p4p wrapper — singleton Context with async public API.

p4p is synchronous; every blocking call is dispatched via ``asyncio.to_thread``
so the FastMCP event loop is never blocked.
"""

import asyncio
import atexit
import logging
import math
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
from epics_pv_mcp.services._concurrency import get_monitor_executor

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
    """Batch-get PVs. Returns the partial-failure envelope ``{"results": [...], "errors": [...]}``.

    Raises ``EpicsError(error_code="UPSTREAM_CONTRACT_ERROR")`` if the native batch returns a
    different number of values than requested names — a broken provider must fail loudly rather than
    silently drop the surplus PVs (S27/F11 "no silent loss"). Also raises ``BATCH_TOO_LARGE`` if
    ``len(names)`` exceeds ``max_batch_size``.
    """
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
    except Exception as exc:  # noqa: BLE001
        # Batch fehlgeschlagen -> Einzelabfrage-Fallback. Die Wurzel des Batch-Fehlers nicht still
        # verschlucken (für die Diagnose loggen); die Einzelabfragen liefern danach je PV einen
        # genauen Fehler.
        logger.debug("Batch get failed, falling back to concurrent individual gets: %s", exc)
        # M5: run the per-PV reads CONCURRENTLY instead of serially, so ONE disconnected channel
        # degrades the fallback to ~1×timeout instead of n×timeout. return_exceptions=True keeps a
        # bad PV from cancelling the healthy ones; each outcome is classified per element below.
        individual = await asyncio.gather(
            *(pv_get(name, timeout=timeout) for name in names),
            return_exceptions=True,
        )
        # asyncio.gather(return_exceptions=True) garantiert len(individual)==len(names) UND Ordnung
        # -> hier KEIN Längen-Guard (ein strict=True wäre ein Wächter, der nie rot werden kann;
        # Evidence-Regel 5). Der Provider-Vertrag wird im else-Zweig geprüft, wo er feuern kann.
        for name, outcome in zip(names, individual, strict=False):
            if not isinstance(outcome, BaseException):
                results.append(outcome)
            elif isinstance(outcome, PVNotFoundError):
                errors.append({"pv_name": name, "error": f"PV '{name}' not found"})
            elif isinstance(outcome, PVTimeoutError):
                errors.append({"pv_name": name, "error": f"Timeout getting PV '{name}'"})
            elif isinstance(outcome, Exception):
                # EpicsConnectionError and any other Exception → a per-PV error entry (never crash
                # the whole batch on one PV, as the serial loop's un-caught branch could).
                errors.append({"pv_name": name, "error": str(outcome)})
            else:
                # A non-Exception BaseException (e.g. CancelledError) is never swallowed. It did not
                # arise from the native-batch failure, so do not chain it to that context.
                raise outcome from None
    else:
        # Nativer Batch OK. Provider-Längen-Vertrag HIER durchsetzen (außerhalb des except), damit
        # ein Verstoß LAUT als UPSTREAM_CONTRACT_ERROR scheitert statt in den Fallback geschluckt
        # zu werden — ein Raise im try würde vom except Exception gefangen (S27/F11: kein stiller
        # Verlust). p4p prä-sized sein Ergebnis auf len(names) -> feuert nur bei kaputtem Provider.
        if len(values) != len(names):
            raise EpicsError(
                f"EPICS provider returned {len(values)} values for {len(names)} requested PVs",
                error_code="UPSTREAM_CONTRACT_ERROR",
                details={"requested": len(names), "received": len(values)},
            )
        for name, value in zip(names, values, strict=False):
            try:
                results.append(_format_value(name, value))
            except Exception as exc:  # noqa: BLE001
                # ein kaputter Einzelwert darf den Batch nicht abbrechen
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
) -> tuple[list[dict[str, object]], bool]:
    """Monitor a PV for *duration* seconds, returning ``(events, truncated)``.

    Collects up to *max_events* events; ``truncated`` is True iff MORE than *max_events*
    actually arrived — detected by over-collecting exactly one extra "canary" event, then
    trimming it off (the same honest over-fetch as ``get_alarm_history``'s ``size=max+1``).
    A stream that delivers exactly *max_events* and then goes quiet is NOT truncated.

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
                # Over-collect by one: stop only at max_events+1, so a later `len > max_events`
                # honestly distinguishes "the cap cut the stream" (truncated) from "exactly
                # max_events arrived, then it went quiet" (complete). The canary is trimmed off
                # before returning. RED before: `>= max_events` reported truncated on an
                # exactly-full-but-complete stream, and a plain `>` swap would make truncated dead.
                if len(collected) >= max_events + 1:
                    stop_event.set()
                    return
                try:
                    collected.append(_format_value(name, value))
                except Exception:  # noqa: BLE001
                    # ein Monitor-Callback darf den Worker-Thread nie crashen; value=None
                    # statt str(value) — der Wrapper-str() ergäbe ctime-Müll (s. _format_value)
                    # — aber DEKLARIERT, sonst zählt ein Format-Crash als echtes None-Event.
                    logger.debug("monitor format failed for PV %s", name, exc_info=True)
                    collected.append(
                        {
                            "pv_name": name,
                            "value": None,
                            "note": "value extraction failed; value withheld "
                            "(None is NOT a live reading)",
                        }
                    )

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

    # K4 bulkhead: run the blocking p4p subscription on the DEDICATED monitor executor, not the
    # shared asyncio default pool. A monitor holds its thread up to max_monitor_duration (60 s), so
    # dispatching it here (instead of asyncio.to_thread) keeps a burst of monitors from starving the
    # default pool that every other to_thread call — REST checks, PV reads/writes — depends on.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(get_monitor_executor(), _monitor_thread)

    if error_holder:
        raise error_holder[0]

    # Honest truncation via the over-collect above: True only when the +1 canary was reached.
    truncated = len(collected) > max_events
    return collected[:max_events], truncated


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


def _type_id(raw: object) -> str:
    """The p4p normative-type id (``epics:nt/NTTable:1.0`` …), or ``""`` when unavailable."""
    get_id = getattr(raw, "getID", None)
    return str(get_id()) if callable(get_id) else ""


def _is_p4p_value(obj: object) -> bool:
    """True for a p4p ``Value`` (struct / union / wrapper).

    Crucial discriminator: a real p4p ``Value`` exposes BOTH ``todict()`` AND ``tolist()`` — the
    latter is NOT numpy-exclusive — so a struct cannot be told apart from a numpy array by
    ``tolist`` alone. A numpy array has ``tolist`` but no ``todict``. Requiring ``todict`` plus a
    ``getID``/``type`` marker identifies the p4p ``Value`` and routes it through ``todict()`` (a
    struct's ``tolist()`` yields raw ``(name, value)`` tuples with numpy/Value leaves un-converted).
    """
    return callable(getattr(obj, "todict", None)) and (
        callable(getattr(obj, "getID", None)) or getattr(obj, "type", None) is not None
    )


def _struct_to_dict(value_field: object) -> dict[str, object]:
    """A p4p struct's ``todict()`` (or a plain mapping) as a dict; ``{}`` when neither applies."""
    todict = getattr(value_field, "todict", None)
    if callable(todict):
        return dict(todict())
    if isinstance(value_field, dict):
        return dict(value_field)
    return {}


def _summarize_unknown(obj: object) -> dict[str, object]:
    """An object that cannot be converted -> an honest summary, never the raw object."""
    get_id = getattr(obj, "getID", None)
    type_id = str(get_id()) if callable(get_id) else type(obj).__name__
    return {
        "unsupported_type": type_id,
        "note": "value not JSON-serialisable; surfaced as a summary only",
    }


def _jsonify(obj: object) -> object:
    """Return a JSON-serialisable form of ANY p4p / numpy / plain value, recursively.

    The single robust converter behind DS-6. A p4p ``Value`` goes via ``todict()`` (NOT
    ``tolist()``); a numpy array via ``tolist()``; dicts/lists/tuples recurse element-wise (so a
    ``structure[]`` list-of-Value or a union list-of-ndarray is fully converted — a plain ``list``
    is never trusted wholesale); scalars pass through; anything else becomes an honest summary.
    Guarantees the result never contains a raw p4p object or a numpy array.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if _is_p4p_value(obj):
        return _jsonify(_struct_to_dict(obj))
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):  # a numpy array (p4p Values were already handled above)
        return _jsonify(tolist())
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    return _summarize_unknown(obj)


def _extract_nt_table(raw: object) -> dict[str, object]:
    """NTTable -> ``{labels: [...], columns: {name: [...]}}`` (numpy columns converted to lists)."""
    labels_field = getattr(raw, "labels", None)
    labels = [str(x) for x in labels_field] if labels_field is not None else []
    columns = {str(k): _jsonify(v) for k, v in _struct_to_dict(getattr(raw, "value", None)).items()}
    return {"labels": labels, "columns": columns}


def _extract_nt_ndarray(raw: object) -> dict[str, object]:
    """NTNDArray -> a shape/dtype summary; the (potentially large) pixel data is OMITTED.

    Inlining a full image as a nested list would blow the MCP payload, so this surfaces the
    structural metadata with an honest ``data_omitted`` flag + note instead of the raw array.
    ``shape`` is reported in numpy (rows-first) order: p4p stores ``dimension`` as
    ``[width, height, …]`` REVERSED from numpy's ``(rows, cols, …)``, so the sizes are reversed
    back to match the array's natural order.
    """
    dims = getattr(raw, "dimension", None)
    shape = list(reversed([int(getattr(d, "size", 0)) for d in dims])) if dims is not None else []
    out: dict[str, object] = {
        "shape": shape,
        "data_omitted": True,
        "note": "NTNDArray pixel data omitted (potentially large); shape + dtype only.",
    }
    array = getattr(raw, "value", None)
    dtype = getattr(array, "dtype", None)
    if dtype is not None:
        out["dtype"] = str(dtype)
    size = getattr(array, "size", None)
    if isinstance(size, int):
        out["element_count"] = size
    return out


def _int_or_none(column: list[object], index: int) -> int | None:
    """``column[index]`` as int, or None when absent/short/non-numeric (defensive zip)."""
    if index >= len(column):
        return None
    item = column[index]
    return int(item) if isinstance(item, (int, float)) else None


def _jsonified_list(raw: object, attr: str) -> list[object]:
    """A parallel-array field of *raw* as a JSON-safe list (``[]`` when absent/non-array)."""
    listed = _jsonify(getattr(raw, attr, None))
    return listed if isinstance(listed, list) else []


def _extract_nt_matrix(raw: object) -> dict[str, object]:
    """NTMatrix -> ``{shape: [rows, cols], rows: [[...]]}`` (row-major, per the NT spec).

    ``dim`` is ``int[2]`` in matrix order ``[rows, cols]`` (NOT reversed like NTNDArray's
    ``dimension``) and is optional: absent -> the value is a vector, reported as ONE row
    with ``shape == [len(value)]``. A ``dim`` that does not multiply to ``len(value)`` is
    NOT trusted into a wrong reshape: the values stay flat and an honest ``note`` declares
    the mismatch (the NTNDArray honesty pattern). Without this extractor the ``dim`` field
    was dropped entirely and a 2x3 serialised bit-identically to a 3x2.
    """
    values_obj = _jsonify(getattr(raw, "value", None))
    if isinstance(values_obj, list):
        values = values_obj
    elif values_obj is None:
        values = []
    else:
        values = [values_obj]  # malformed scalar value: keep it as one row, never drop it
    raw_dims = _jsonified_list(raw, "dim")
    # `dim` is int[] on the wire; a non-integral entry is only constructible via fakes and
    # must NOT be int-truncated into a note that misquotes the wire — report flat + raw.
    dims = [d for d in raw_dims if isinstance(d, int) and not isinstance(d, bool)]
    if len(dims) != len(raw_dims):
        dims = []  # a non-integral entry -> the whole dim is untrusted
    if len(dims) == 2 and dims[0] * dims[1] == len(values) and min(dims) >= 0:
        n_rows, n_cols = dims
        return {
            "shape": [n_rows, n_cols],
            "rows": [values[r * n_cols : (r + 1) * n_cols] for r in range(n_rows)],
        }
    out: dict[str, object] = {"shape": [len(values)], "rows": [values]}
    if raw_dims and dims != [len(values)]:
        out["note"] = (
            f"NTMatrix dim {raw_dims} does not match the {len(values)} values; reported flat."
        )
    return out


def _extract_nt_multi_channel(raw: object) -> dict[str, object]:
    """NTMultiChannel -> ONE record per channel: name, value, per-channel alarm/connect state.

    The wire format is parallel arrays (``value[i]`` belongs to ``channelName[i]``); zipping
    them back into per-channel records keeps the name<->value<->severity attribution intact.
    Every per-channel array except ``value``/``channelName`` is optional per the NT spec, so
    each is read defensively by index (a missing/short array simply omits that key).

    The ``note`` counters a structural trap this extractor exists for: the TOP-LEVEL alarm
    of an NTMultiChannel says nothing about the channels — it typically reads NO_ALARM right
    next to a MAJOR channel, so the per-channel severities surfaced here are the ones that
    matter. The top-level block is still reported (it IS on the wire), note included.
    """
    values = _jsonified_list(raw, "value")
    names = _jsonified_list(raw, "channelName")
    severity = _jsonified_list(raw, "severity")
    status = _jsonified_list(raw, "status")
    message = _jsonified_list(raw, "message")
    connected = _jsonified_list(raw, "isConnected")
    seconds = _jsonified_list(raw, "secondsPastEpoch")
    nanos = _jsonified_list(raw, "nanoseconds")
    user_tag = _jsonified_list(raw, "userTag")

    channels: list[dict[str, object]] = []
    for index in range(max(len(values), len(names))):
        channel: dict[str, object] = {}
        if index < len(names):
            channel["name"] = str(names[index])
        if index < len(values):
            channel["value"] = values[index]
        sev = _int_or_none(severity, index)
        if sev is not None:
            channel["severity"] = sev
            channel["severity_text"] = _SEVERITY_TEXT.get(sev, str(sev))
        stat = _int_or_none(status, index)
        if stat is not None:
            channel["status"] = stat
            channel["status_text"] = _ALARM_STATUS_TEXT.get(stat, str(stat))
        if index < len(message):
            channel["message"] = str(message[index])
        if index < len(connected):
            channel["connected"] = bool(connected[index])
        tag = _int_or_none(user_tag, index)
        if tag is not None:
            channel["user_tag"] = tag
        secs = _int_or_none(seconds, index)
        if secs is not None:
            timestamp: dict[str, object] = {"seconds": secs}
            nano = _int_or_none(nanos, index)
            if nano is not None:
                timestamp["nanoseconds"] = nano
            channel["timestamp"] = timestamp
        channels.append(channel)
    return {
        "channels": channels,
        "note": (
            "per-channel alarm state lives in channels[] (severity/severity_text); the "
            "top-level alarm block of an NTMultiChannel is structural and does not "
            "aggregate the channels."
        ),
    }


def _extract_value(raw: object) -> tuple[object, dict[str, object] | None]:
    """Return ``(value, enum_or_none)`` as JSON-serialisable data.

    Scalars pass through; arrays become lists; NTEnum keeps its numeric index plus an ``enum``
    block. DS-6: complex normative types that previously slipped through as a raw p4p wrapper
    (failing at the MCP JSON boundary) or as ``value=None`` are surfaced as real data — NTTable as
    ``{labels, columns}``, NTNDArray as a shape/dtype summary (pixel data omitted), NTMatrix as
    ``{shape, rows}``, NTMultiChannel as per-channel records ``{channels: [...]}``, and every other
    shape (nested struct, ``structure[]``, variant-union array, numpy array) via the robust
    :func:`_jsonify` converter. The value is never a raw p4p object or numpy array.
    """
    type_id = _type_id(raw)
    # Routing discipline (QA-hardened): an EXPLICIT type id is authoritative. The structural
    # markers below (choices/dimension/labels/channelName) would otherwise capture foreign
    # structs and mint them into NT shapes — a fabricated enum index, a wrong-shape NTNDArray
    # summary withholding the real values, an NTMultiChannel note on a non-NTMultiChannel.
    # They stay ONLY as a fallback for ANONYMOUS values: a fake without getID() reports "",
    # a bare p4p struct reports "structure"; anything else is routed by its id or converted
    # generically. NTMatrix has no marker at all — `dim` is far too generic a field name
    # (pinned by the real-p4p generic-struct test).
    anonymous = type_id in ("", "structure")
    val_field = getattr(raw, "value", raw)
    choices = getattr(val_field, "choices", None)
    if choices is not None and (type_id.startswith("epics:nt/NTEnum") or anonymous):
        # NTEnum: the value field is a struct {index, choices}.
        index = int(getattr(val_field, "index", 0))
        labels = [str(c) for c in choices]
        label = labels[index] if 0 <= index < len(labels) else None
        return index, {"index": index, "label": label, "choices": labels}
    # NTNDArray + NTTable get a bespoke shape BEFORE the generic converter (an NTNDArray value IS an
    # array that must be summarised, not dumped whole; an NTTable gets {labels, columns}).
    if type_id.startswith("epics:nt/NTNDArray") or (
        anonymous and getattr(raw, "dimension", None) is not None
    ):
        return _extract_nt_ndarray(raw), None
    if type_id.startswith("epics:nt/NTTable") or (
        anonymous and getattr(raw, "labels", None) is not None
    ):
        return _extract_nt_table(raw), None
    if type_id.startswith("epics:nt/NTMatrix"):
        return _extract_nt_matrix(raw), None
    if type_id.startswith("epics:nt/NTMultiChannel") or (
        anonymous and getattr(raw, "channelName", None) is not None
    ):
        return _extract_nt_multi_channel(raw), None
    # Everything else (scalar, numpy array, nested struct, structure[], union array) — one robust,
    # recursive converter that discriminates a p4p Value (-> todict) from a numpy array (-> tolist).
    return _jsonify(val_field), None


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
    """value_alarm: the ``active`` flag plus whichever limits/severities carry a real value.

    ``active`` is surfaced as honest metadata, never as a visibility gate: QSRV2/softIocPVX
    reports ``active=False`` even when HIHI/HIGH/LOW/LOLO thresholds ARE configured, so gating
    on it (the old behaviour) hid every real limit. Per-field filtering replaces the gate:

    * ``NaN`` limits are ALWAYS dropped — that is the QSRV2 "unset" marker, never a real value.
    * when ``active`` is False/absent, ``0``-valued limits/severities are ALSO dropped as unset
      — a 0.0-sending producer (e.g. QSRV1/CA) would otherwise look like a real ``[0,0]``
      threshold (preserves the original unconfigured-limit suppression).
    * when ``active`` is True every non-NaN field is kept, so a legitimately-zero configured
      limit stays visible (no regression of the prior active=True behaviour).

    Note: over QSRV2 the per-level ``*Severity`` fields arrive structurally ``0`` (the record
    HSV/HHSV are not mapped into valueAlarm) and are thus dropped here; the live alarm severity
    is reported separately via the ``alarm`` block.
    """
    va = getattr(raw, "valueAlarm", None)
    if va is None:
        return None
    active = bool(getattr(va, "active", False))
    out: dict[str, object] = {"active": active}
    for key, value in _collect(va, _VALUE_ALARM_SPEC).items():
        if isinstance(value, float) and math.isnan(value):
            continue  # NaN = QSRV2 unset marker, never a real limit.
        if not active and value == 0:
            continue  # ambiguous 0/0.0 in a non-active struct = unconfigured.
        out[key] = value
    return out


def _extract_descriptor(raw: object) -> str | None:
    """The optional NT ``descriptor`` — the free-text description every NT type may carry.

    Previously dropped for ALL NT types (no block extractor claimed it). An unset
    descriptor arrives as ``""`` on the wire — absent and unset look the same there, and
    an empty description carries no information, so it is omitted rather than reported.
    Only a genuine ``str`` is accepted: ``str()`` on a malformed non-string field would
    serialise p4p repr garbage (the same trap :func:`_format_value` documents for values).
    """
    descriptor = getattr(raw, "descriptor", None)
    if not isinstance(descriptor, str):
        return None
    return descriptor or None


# Metadata blocks, each extracted independently so a malformed one cannot corrupt the
# value or the other blocks (per-block robustness). A falsy block (None, "", {}) is
# omitted from the result.
_BLOCK_EXTRACTORS: list[tuple[str, Callable[[object], dict[str, object] | str | None]]] = [
    ("alarm", _extract_alarm),
    ("timestamp", _extract_timestamp),
    ("display", _extract_display),
    ("control", _extract_control),
    ("value_alarm", _extract_value_alarm),
    ("descriptor", _extract_descriptor),
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
    (``active`` flag plus the configured HIHI/HIGH/LOW/LOLO limits; NaN/unset limits and the
    per-PVA-unmapped per-level severities are omitted), ``descriptor`` (the NT free-text
    description; empty = omitted). Display/control limit pairs that are equal (zero-width =
    unset) are omitted.

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
        # prepends a ctime() and would emit garbage like "Thu Jan  1 1970 4.2") — but
        # DECLARED: a bare {"value": None} is indistinguishable from a genuinely-None
        # reading for every consumer that condenses this dict (validate/discover/
        # write-readback/monitor). Same honesty pattern as data_omitted.
        logger.debug("value extraction failed for PV %s", pv_name, exc_info=True)
        result["note"] = "value extraction failed; value withheld (None is NOT a live reading)"

    for key, extractor in _BLOCK_EXTRACTORS:
        try:
            block = extractor(raw)
        except Exception:  # noqa: BLE001
            logger.debug("%s extraction failed for PV %s", key, pv_name, exc_info=True)
            continue
        if block:
            result[key] = block

    return result
