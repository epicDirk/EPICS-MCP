"""Tests for the epics_client helpers (no EPICS connection except the real-p4p test)."""

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

import epics_mcp.services.epics_client as epics_client
from epics_mcp.errors import EpicsConnectionError, EpicsError, PVNotFoundError, PVTimeoutError
from epics_mcp.services.epics_client import _classify_p4p_error, _format_value
from epics_mcp.tools.info import _get_pv_info


def test_classify_not_found() -> None:
    err = _classify_p4p_error("X:Y", Exception("PV not found"), action="accessing")
    assert isinstance(err, PVNotFoundError)


def test_classify_search_is_not_found() -> None:
    err = _classify_p4p_error("X:Y", Exception("search failed for channel"), action="accessing")
    assert isinstance(err, PVNotFoundError)


def test_classify_other_is_connection() -> None:
    err = _classify_p4p_error("X:Y", Exception("broken pipe"), action="writing")
    assert isinstance(err, EpicsConnectionError)
    assert "X:Y" in str(err)
    assert "writing" in str(err)


# ---------------------------------------------------------------------------
# _format_value, p4p unwrapped wrappers expose meta-data via ``.raw``.
# Fakes mirror that shape: a wrapper with ``.raw`` whose attributes are the
# NTScalar / NTEnum sub-structures (no live EPICS needed for the fake-based tests).
# ---------------------------------------------------------------------------


def _wrap(raw: SimpleNamespace) -> SimpleNamespace:
    """Mimic a p4p unwrapped value: the raw ``Value`` lives under ``.raw``."""
    return SimpleNamespace(raw=raw)


class _FakeArray:
    """Stands in for a numpy array, exposes ``tolist`` (real scalars are plain Python)."""

    def __init__(self, data: list[Any]) -> None:
        self._data = data

    def tolist(self) -> list[Any]:
        return list(self._data)


class _FakeStruct:
    """Stands in for a nested p4p ``Value`` struct, exposes BOTH ``todict()`` and ``tolist()``.

    Faithful to the real p4p contract that broke the first DS-6 attempt: a real ``p4p.Value`` has
    ``tolist()`` too (not numpy-exclusive), and its ``tolist()`` returns raw ``(name, value)``
    tuples WITHOUT converting nested array leaves. The code must check ``todict``/``getID`` and
    prefer it; if it regressed to the ``tolist`` branch this fake would surface the
    raw ``_FakeArray`` tuples and the test would fail. ``getID()`` mirrors the type identifier.
    """

    def __init__(self, data: dict[str, Any], type_id: str = "structure") -> None:
        self._data = data
        self._type_id = type_id

    def getID(self) -> str:
        return self._type_id

    def todict(self) -> dict[str, Any]:
        return dict(self._data)

    def tolist(self) -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self._data.items()]


class _FakeNDArrayData:
    """Stands in for the numpy image array inside an NTNDArray, dtype/size, tolist must NOT run."""

    def __init__(self, dtype: str, size: int) -> None:
        self.dtype = dtype
        self.size = size

    def tolist(self) -> list[int]:  # pragma: no cover - asserts the bulk data is never inlined
        raise AssertionError("NTNDArray pixel data must not be dumped as a list")


def test_format_value_full_scalar() -> None:
    raw = SimpleNamespace(
        value=4.2,
        alarm=SimpleNamespace(severity=1, status=5, message="HIGH alarm"),
        timeStamp=SimpleNamespace(secondsPastEpoch=1000, nanoseconds=500),
        display=SimpleNamespace(
            units="mbar", limitLow=0.0, limitHigh=10.0, precision=2, description="Gauge"
        ),
        control=SimpleNamespace(limitLow=0.0, limitHigh=8.0, minStep=0.1),
        valueAlarm=SimpleNamespace(
            active=True,
            lowAlarmLimit=0.5,
            lowWarningLimit=1.0,
            highWarningLimit=7.0,
            highAlarmLimit=9.0,
        ),
    )

    result = _format_value("VAC:PV", _wrap(raw))

    assert result["pv_name"] == "VAC:PV"
    assert result["value"] == 4.2
    assert result["alarm"] == {
        "severity": 1,
        "severity_text": "MINOR",
        "status": 5,
        "status_text": "CONF",
        "message": "HIGH alarm",
    }
    assert result["timestamp"] == {"seconds": 1000, "nanoseconds": 500}
    assert result["display"] == {
        "units": "mbar",
        "limit_low": 0.0,
        "limit_high": 10.0,
        "precision": 2,
        "description": "Gauge",
    }
    assert result["control"] == {"limit_low": 0.0, "limit_high": 8.0, "min_step": 0.1}
    assert result["value_alarm"] == {
        "active": True,
        "low_alarm": 0.5,
        "low_warning": 1.0,
        "high_warning": 7.0,
        "high_alarm": 9.0,
    }


def test_format_value_enum() -> None:
    raw = SimpleNamespace(
        value=SimpleNamespace(index=1, choices=["OFF", "ON"]),
        alarm=SimpleNamespace(severity=0, status=0, message=""),
        timeStamp=SimpleNamespace(secondsPastEpoch=1, nanoseconds=2),
    )

    result = _format_value("DEV:State", _wrap(raw))

    assert result["value"] == 1  # back-compat: value stays the numeric index
    assert result["enum"] == {"index": 1, "label": "ON", "choices": ["OFF", "ON"]}
    alarm = result["alarm"]
    assert isinstance(alarm, dict)
    assert alarm["severity_text"] == "NO_ALARM"
    assert alarm["status_text"] == "NONE"
    assert alarm["message"] == ""  # real alarm always carries message (even empty)


def test_format_value_enum_index_out_of_range() -> None:
    raw = SimpleNamespace(value=SimpleNamespace(index=5, choices=["OFF", "ON"]))

    result = _format_value("DEV:State", _wrap(raw))

    assert result["value"] == 5
    enum = result["enum"]
    assert isinstance(enum, dict)
    assert enum["label"] is None  # guarded against out-of-range index


def test_format_value_array_uses_tolist() -> None:
    raw = SimpleNamespace(value=_FakeArray([1.0, 2.0, 3.0]))

    result = _format_value("WF:PV", _wrap(raw))

    assert result["value"] == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# DS-6, complex PVA types (NTTable / NTNDArray / nested struct) are surfaced as real,
# JSON-serialisable data instead of value=None or a raw p4p passthrough that fails at the MCP
# JSON boundary. Every test json.dumps the COMPLETE tool result (the real failure surface).
# ---------------------------------------------------------------------------


def test_format_value_nttable_columns() -> None:
    """NTTable -> {labels, columns:{name:list}} with numpy columns converted to lists."""
    columns = _FakeStruct(
        {"device": _FakeArray(["A", "B"]), "value": _FakeArray([1.0, 2.0])},
        type_id="structure",
    )
    raw = SimpleNamespace(
        getID=lambda: "epics:nt/NTTable:1.0",
        labels=["Device", "Value"],
        value=columns,
        alarm=SimpleNamespace(severity=0, status=0, message=""),
    )

    result = _format_value("TBL:PV", _wrap(raw))

    assert result["value"] == {
        "labels": ["Device", "Value"],
        "columns": {"device": ["A", "B"], "value": [1.0, 2.0]},
    }
    assert "alarm" in result  # block extractors still run alongside the table value
    json.dumps(result)  # gate: the whole result serialises


def test_format_value_ntndarray_shape_dtype_only() -> None:
    """NTNDArray -> shape/dtype summary with the pixel data OMITTED (never inlined as a list).

    p4p stores ``dimension`` as ``[width, height]``, REVERSED from numpy's ``(rows, cols)``,
    so a 2-row×3-col image has wire dimension sizes ``[3, 2]``; the reported shape is reversed
    back to numpy order ``[2, 3]``. (A real-p4p test below pins this against an actual NTNDArray.)
    """
    raw = SimpleNamespace(
        getID=lambda: "epics:nt/NTNDArray:1.0",
        dimension=[SimpleNamespace(size=3), SimpleNamespace(size=2)],  # wire order (width, height)
        value=_FakeNDArrayData(dtype="uint8", size=6),
    )

    result = _format_value("IMG:PV", _wrap(raw))

    value = result["value"]
    assert isinstance(value, dict)
    assert value["shape"] == [2, 3]  # numpy (rows, cols) order after reversal
    assert value["dtype"] == "uint8"
    assert value["element_count"] == 6
    assert value["data_omitted"] is True
    assert "note" in value
    json.dumps(result)  # gate: serialises, and _FakeNDArrayData.tolist was never called


def test_format_value_nested_struct_not_raw_passthrough() -> None:
    """A generic nested struct is serialised via todict(), NOT returned as the raw p4p wrapper
    (which used to slip through and fail at the MCP JSON boundary)."""
    nested = _FakeStruct({"x": 1.0, "y": _FakeArray([2.0, 3.0])}, type_id="structure")
    raw = SimpleNamespace(value=nested)

    result = _format_value("STRUCT:PV", _wrap(raw))

    assert result["value"] == {"x": 1.0, "y": [2.0, 3.0]}
    json.dumps(result)  # gate: no raw p4p object leaks into the result


def test_format_value_unsupported_struct_without_todict_is_summarised() -> None:
    """A structured value that cannot be converted (no todict) is surfaced as an honest summary,
    never the raw object and never a crash, the last-resort fallback."""

    class _Opaque:
        def getID(self) -> str:
            return "epics:nt/NTUnion:1.0"

    raw = SimpleNamespace(value=_Opaque())

    result = _format_value("OPAQUE:PV", _wrap(raw))

    value = result["value"]
    assert isinstance(value, dict)
    assert value["unsupported_type"] == "epics:nt/NTUnion:1.0"
    assert "note" in value
    json.dumps(result)


def test_format_value_broken_complex_type_falls_back_to_none() -> None:
    """The None-path: if extraction raises, value stays None (honest 'could not read'), never a
    crash and never garbage, same contract as before, now covering complex types."""

    class _Boom:
        def getID(self) -> str:
            raise RuntimeError("boom")

    raw = SimpleNamespace(value=_Boom(), alarm=SimpleNamespace(severity=1, status=0, message="x"))

    result = _format_value("BOOM:PV", _wrap(raw))

    assert result["value"] is None
    assert "alarm" in result  # blocks still extracted; only the value failed
    json.dumps(result)


def test_format_value_string_scalar_carries_alarm() -> None:
    # A real string NTScalar DOES carry an alarm struct, the fake must too.
    raw = SimpleNamespace(value="hello", alarm=SimpleNamespace(severity=0, status=0, message=""))

    result = _format_value("STR:PV", _wrap(raw))

    assert result["value"] == "hello"
    assert "alarm" in result


def test_format_value_unknown_alarm_codes_fall_back_to_str() -> None:
    raw = SimpleNamespace(value=0.0, alarm=SimpleNamespace(severity=9, status=42, message="?"))

    result = _format_value("X:Y", _wrap(raw))

    alarm = result["alarm"]
    assert isinstance(alarm, dict)
    assert alarm["severity_text"] == "9"
    assert alarm["status_text"] == "42"
    assert alarm["message"] == "?"


def test_format_value_without_raw_uses_object_directly() -> None:
    # A raw p4p.Value (Context built with nt=False) has no ``.raw`` and is used directly.
    raw_like = SimpleNamespace(
        value=7.0,
        alarm=SimpleNamespace(severity=2, status=3, message="MAJOR"),
    )

    result = _format_value("X:Y", raw_like)

    assert result["value"] == 7.0
    alarm = result["alarm"]
    assert isinstance(alarm, dict)
    assert alarm["severity_text"] == "MAJOR"
    assert alarm["status_text"] == "RECORD"


def test_format_value_minimal_omits_absent_blocks() -> None:
    raw = SimpleNamespace(value=1.0)

    result = _format_value("X:Y", _wrap(raw))

    assert result["value"] == 1.0
    for key in ("alarm", "timestamp", "display", "control", "value_alarm", "enum"):
        assert key not in result


# --- value_alarm active-gating -------------------------------------------------


def test_value_alarm_inactive_hides_limits() -> None:
    # Unconfigured valueAlarm: active=False with 0.0 limits -> limits suppressed.
    raw = SimpleNamespace(
        value=4.2,
        valueAlarm=SimpleNamespace(active=False, lowAlarmLimit=0.0, highAlarmLimit=0.0),
    )

    result = _format_value("X:Y", _wrap(raw))

    assert result["value_alarm"] == {"active": False}


def test_value_alarm_active_surfaces_limits_and_severities() -> None:
    raw = SimpleNamespace(
        value=4.2,
        valueAlarm=SimpleNamespace(
            active=True,
            lowAlarmLimit=-5.0,
            highAlarmLimit=5.0,
            lowAlarmSeverity=2,
            highAlarmSeverity=2,
        ),
    )

    result = _format_value("X:Y", _wrap(raw))

    assert result["value_alarm"] == {
        "active": True,
        "low_alarm": -5.0,
        "high_alarm": 5.0,
        "low_alarm_severity": 2,
        "high_alarm_severity": 2,
    }


def test_value_alarm_without_active_field_surfaces_real_limits() -> None:
    # No ``active`` field (defaults False): real (non-zero, non-NaN) limits are STILL surfaced:
    # the active flag is metadata, not a visibility gate.
    raw = SimpleNamespace(
        value=4.2,
        valueAlarm=SimpleNamespace(lowAlarmLimit=-5.0, highAlarmLimit=5.0),
    )

    result = _format_value("X:Y", _wrap(raw))

    assert result["value_alarm"] == {"active": False, "low_alarm": -5.0, "high_alarm": 5.0}


def test_value_alarm_qsrv2_active_false_surfaces_configured_limits() -> None:
    # The real QSRV2/softIocPVX wire profile: active is present-but-False, configured limits are
    # non-NaN, unset limits arrive as NaN, and the per-level severities arrive structurally 0.
    # Expectation: the configured limits ARE surfaced; NaN limits + 0-severities are dropped.
    raw = SimpleNamespace(
        value=42.5,
        valueAlarm=SimpleNamespace(
            active=False,
            lowAlarmLimit=float("nan"),
            lowWarningLimit=float("nan"),
            highWarningLimit=70.0,
            highAlarmLimit=80.0,
            lowAlarmSeverity=0,
            lowWarningSeverity=0,
            highWarningSeverity=0,
            highAlarmSeverity=0,
        ),
    )

    result = _format_value("X:Y", _wrap(raw))

    assert result["value_alarm"] == {"active": False, "high_warning": 70.0, "high_alarm": 80.0}


def test_value_alarm_all_nan_limits_yields_only_active() -> None:
    # A record with no alarm thresholds (e.g. 3V3): QSRV2 still emits a full valueAlarm
    # struct with all-NaN limits + 0-severities -> only the honest ``active`` flag survives.
    raw = SimpleNamespace(
        value=3.3,
        valueAlarm=SimpleNamespace(
            active=False,
            lowAlarmLimit=float("nan"),
            lowWarningLimit=float("nan"),
            highWarningLimit=float("nan"),
            highAlarmLimit=float("nan"),
            lowAlarmSeverity=0,
            highAlarmSeverity=0,
        ),
    )

    result = _format_value("X:Y", _wrap(raw))

    assert result["value_alarm"] == {"active": False}


# --- degenerate limits + display.format + robustness --------------------------


def test_degenerate_limit_pairs_dropped() -> None:
    raw = SimpleNamespace(
        value=4.2,
        display=SimpleNamespace(units="V", limitLow=0.0, limitHigh=0.0),
        control=SimpleNamespace(limitLow=0.0, limitHigh=0.0, minStep=0.0),
    )

    result = _format_value("X:Y", _wrap(raw))

    # Zero-width ranges are unset -> the limit pairs are dropped; other fields stay.
    assert result["display"] == {"units": "V"}
    assert result["control"] == {"min_step": 0.0}


def test_display_format_surfaced_when_no_precision() -> None:
    raw = SimpleNamespace(value=4.2, display=SimpleNamespace(format="%.3f"))

    result = _format_value("X:Y", _wrap(raw))

    display = result["display"]
    assert isinstance(display, dict)
    assert display["format"] == "%.3f"


def test_malformed_field_skipped_value_survives() -> None:
    # float(None) raises -> only that field is skipped; value + other fields survive.
    raw = SimpleNamespace(
        value=4.2,
        alarm=SimpleNamespace(severity=0, status=0, message=""),
        display=SimpleNamespace(units="V", limitLow=None, limitHigh=10.0),
    )

    result = _format_value("X:Y", _wrap(raw))

    assert result["value"] == 4.2  # NOT corrupted to a wrapper string
    assert "alarm" in result
    display = result["display"]
    assert isinstance(display, dict)
    assert "limit_low" not in display  # the malformed field was skipped
    assert display["limit_high"] == 10.0
    assert display["units"] == "V"


def test_present_but_empty_strings_passed_through() -> None:
    raw = SimpleNamespace(value=4.2, display=SimpleNamespace(units="", description=""))

    result = _format_value("X:Y", _wrap(raw))

    # Contract: present string fields are surfaced as-is (incl. empty).
    assert result["display"] == {"units": "", "description": ""}


def test_partial_alarm_and_timestamp_defaults() -> None:
    raw = SimpleNamespace(
        value=4.2,
        alarm=SimpleNamespace(severity=2),  # no status, no message
        timeStamp=SimpleNamespace(secondsPastEpoch=10),  # no nanoseconds
    )

    result = _format_value("X:Y", _wrap(raw))

    alarm = result["alarm"]
    assert isinstance(alarm, dict)
    assert alarm["severity_text"] == "MAJOR"
    assert alarm["status"] == 0
    assert alarm["status_text"] == "NONE"
    assert "message" not in alarm
    assert result["timestamp"] == {"seconds": 10, "nanoseconds": 0}


# --- against REAL p4p Values (deterministic, offline; p4p is a core dependency) ---


def test_format_value_real_p4p() -> None:
    from p4p.nt import NTEnum, NTScalar

    ntype = NTScalar("d", display=True, control=True, valueAlarm=True, form=True)
    v = ntype.wrap(4.2)
    v["alarm.severity"] = 1
    v["alarm.status"] = 5
    v["alarm.message"] = "HIGH"
    v["display.units"] = "mbar"
    v["display.limitLow"] = 0.0
    v["display.limitHigh"] = 10.0
    v["display.precision"] = 2
    v["control.limitLow"] = 0.0
    v["control.limitHigh"] = 8.0
    v["valueAlarm.active"] = True
    v["valueAlarm.lowAlarmLimit"] = 0.5
    v["valueAlarm.highAlarmLimit"] = 9.0

    result = _format_value("AI", ntype.unwrap(v))

    assert result["value"] == 4.2
    alarm = result["alarm"]
    assert isinstance(alarm, dict)
    assert alarm["severity_text"] == "MINOR"
    display = result["display"]
    assert isinstance(display, dict)
    assert display["precision"] == 2
    assert display["units"] == "mbar"
    value_alarm = result["value_alarm"]
    assert isinstance(value_alarm, dict)
    assert value_alarm["active"] is True
    assert value_alarm["low_alarm"] == 0.5

    nte = NTEnum()
    ve = nte.wrap({"index": 1, "choices": ["OFF", "ON"]})
    eresult = _format_value("BI", nte.unwrap(ve))
    assert eresult["value"] == 1
    enum = eresult["enum"]
    assert isinstance(enum, dict)
    assert enum["label"] == "ON"


# --- DS-6 against REAL p4p complex types (the shapes SimpleNamespace fakes cannot reproduce:
#     a real p4p Value has BOTH tolist() and todict(), and structure[]/union-array come back as
#     plain Python lists of Value/ndarray). Each asserts json.dumps(result) succeeds, the real
#     MCP-boundary failure surface. These are the tests that would have caught the first attempt.


def test_format_value_real_p4p_struct_with_array_serialises() -> None:
    """A generic (non-NT) struct whose value holds an array leaf. p4p does NOT unwrap it, so
    _format_value gets a raw Value that ALSO has .tolist(); the value must be serialised via
    todict() and json.dumps(result) must succeed (the exact bug the fakes missed)."""
    from p4p import Type, Value

    t = Type([("value", ("S", None, [("name", "s"), ("samples", "ad"), ("n", "i")]))])
    v = Value(t, {"value": {"name": "roi", "samples": [1.0, 2.0, 3.0], "n": 3}})

    result = _format_value("STRUCT:PV", v)

    assert result["value"] == {"name": "roi", "samples": [1.0, 2.0, 3.0], "n": 3}
    json.dumps(result)


def test_format_value_real_p4p_structure_array_serialises() -> None:
    """A top-level structure[] value -> p4p returns raw.value as a plain list of Value objects;
    each element must be converted and json.dumps(result) must succeed."""
    from p4p import Type, Value

    t = Type([("value", ("aS", None, [("x", "d"), ("y", "d")]))])
    v = Value(t, {"value": [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]})

    result = _format_value("SARR:PV", v)

    assert result["value"] == [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]
    json.dumps(result)


def test_format_value_real_p4p_union_array_serialises() -> None:
    """A variant-union array (av) -> raw.value is a plain list of numpy arrays (the NTMultiChannel
    top-level shape); each must convert to a list and json.dumps(result) must succeed."""
    import numpy as np
    from p4p import Type, Value

    t = Type([("value", "av")])
    v = Value(t, {"value": [np.array([1, 2, 3], dtype=np.int32), np.array([4.0, 5.0])]})

    result = _format_value("UARR:PV", v)

    assert result["value"] == [[1, 2, 3], [4.0, 5.0]]
    json.dumps(result)


def test_format_value_real_p4p_ntndarray_shape_numpy_order() -> None:
    """A real NTNDArray reports shape in numpy (rows, cols) order (NOT the reversed wire dimension)
    and omits the pixel data; json.dumps(result) must succeed."""
    import numpy as np
    from p4p.nt import NTNDArray

    img = np.arange(6, dtype=np.uint8).reshape(2, 3)  # 2 rows x 3 cols
    nt = NTNDArray()

    result = _format_value("IMG:PV", nt.unwrap(nt.wrap(img)))

    value = result["value"]
    assert isinstance(value, dict)
    assert value["shape"] == [2, 3]  # numpy order, not the reversed wire dimension [3, 2]
    assert value["data_omitted"] is True
    assert value["dtype"] == "uint8"
    json.dumps(result)


def test_format_value_real_p4p_nttable_serialises() -> None:
    """A real NTTable -> {labels, columns:{name:list}}; json.dumps(result) must succeed.

    p4p's default Context does NOT unwrap NTTable (its unwrap set is NTScalar/NTScalarArray/
    NTEnum/NTNDArray), so production passes the RAW NTTable Value here, routed by its top-level
    getID/labels, not the unwrapped list-of-rows that ``NTTable.unwrap`` would produce.
    """
    from p4p.nt import NTTable

    nt = NTTable(columns=[("device", "s"), ("value", "d")])
    v = nt.wrap([{"device": "A", "value": 1.0}, {"device": "B", "value": 2.0}])

    result = _format_value(
        "TBL:PV", v
    )  # raw Value, as ctxt.get delivers it (NTTable not unwrapped)

    value = result["value"]
    assert isinstance(value, dict)
    assert value["columns"]["device"] == ["A", "B"]
    assert value["columns"]["value"] == [1.0, 2.0]
    assert value["labels"] == ["device", "value"]
    json.dumps(result)


# --- seam: the metadata actually reaches the tool layer -----------------------


class _FakeGetContext:
    """Stands in for the p4p Context: ``.get`` returns a pre-built unwrapped value."""

    def __init__(self, value: object) -> None:
        self._value = value

    def get(self, name: str, timeout: float | None = None) -> object:
        return self._value


async def test_metadata_reaches_get_pv_info_tool(monkeypatch: Any) -> None:
    raw = SimpleNamespace(
        value=4.2,
        valueAlarm=SimpleNamespace(active=True, lowAlarmLimit=1.0),
    )
    monkeypatch.setattr(epics_client, "get_context", lambda: _FakeGetContext(_wrap(raw)))

    result = await _get_pv_info("X:Y")

    assert result["status"] == "success"
    assert result["value"] == 4.2
    assert result["value_alarm"] == {"active": True, "low_alarm": 1.0}


class _RecordingGetContext:
    """Like _FakeGetContext, but records the timeout ``ctxt.get`` is called with."""

    def __init__(self, value: object, sink: dict[str, object]) -> None:
        self._value = value
        self._sink = sink

    def get(self, name: str, timeout: float | None = None) -> object:
        self._sink["timeout"] = timeout
        return self._value


async def test_config_default_timeout_reaches_context(monkeypatch: Any) -> None:
    """M1/C1: with no explicit timeout, EPICS_MCP_DEFAULT_TIMEOUT reaches the p4p context.

    Proves the whole chain end-to-end: the tool wrapper passes ``None`` → ``pv_get``
    resolves ``cfg.default_timeout`` → ``ctxt.get`` sees it. Previously a hardcoded 5.0
    in the wrapper shadowed the operator's configured default.
    """
    import epics_mcp.config as config_module
    from epics_mcp.config import EpicsConfig
    from epics_mcp.tools.read import _get_pv_value

    config_module._config = EpicsConfig(default_timeout=0.1)
    try:
        sink: dict[str, object] = {}
        monkeypatch.setattr(
            epics_client,
            "get_context",
            lambda: _RecordingGetContext(_wrap(SimpleNamespace(value=1.0)), sink),
        )

        await _get_pv_value("X:Y")

        assert sink["timeout"] == 0.1
    finally:
        config_module._config = None


# --- monitor fallback: a failing _format_value must NOT leak a ctime wrapper string ---


class _FakeSub:
    def close(self) -> None:
        pass


class _FakeMonitorContext:
    def __init__(self, value: object) -> None:
        self._value = value

    def monitor(self, name: str, cb: Any) -> _FakeSub:
        cb(self._value)  # deliver one event synchronously
        return _FakeSub()


async def test_monitor_format_failure_yields_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        epics_client, "get_context", lambda: _FakeMonitorContext(_wrap(SimpleNamespace(value=4.2)))
    )

    def _boom(name: str, value: object) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(epics_client, "_format_value", _boom)

    events, truncated = await epics_client.pv_monitor("X:Y", duration=0.2, max_events=1)

    # QA: the fallback event must DECLARE itself, a bare {"value": None} was
    # indistinguishable from a genuinely-None reading in the event count.
    assert len(events) == 1
    assert truncated is False  # one event delivered, cap 1 → complete, not truncated
    assert events[0]["pv_name"] == "X:Y"
    assert events[0]["value"] is None
    assert "extraction failed" in str(events[0]["note"])


class _MultiEventContext:
    """Deliver *n* synthetic events synchronously on subscribe, drives the monitor's
    over-fetch / truncation logic deterministically (F27)."""

    def __init__(self, n: int) -> None:
        self._n = n

    def monitor(self, name: str, cb: Any) -> _FakeSub:
        for i in range(self._n):
            cb(float(i))  # _format_value is stubbed in the tests below
        return _FakeSub()


async def test_pv_monitor_flags_overflow_and_trims(monkeypatch: Any) -> None:
    """F27 over-fetch-by-one: when MORE than max_events arrive, pv_monitor returns EXACTLY
    max_events events and truncated=True, a dropped event is never reported as a complete
    read. RED on the pre-fix cap (== max_events): it cannot tell overflow from an exactly-
    full stream, and a naive `>` swap makes truncated permanently False (silent loss)."""
    monkeypatch.setattr(
        epics_client, "_format_value", lambda name, value: {"pv_name": name, "value": value}
    )
    monkeypatch.setattr(epics_client, "get_context", lambda: _MultiEventContext(5))

    events, truncated = await epics_client.pv_monitor("X:Y", duration=0.2, max_events=3)

    assert len(events) == 3
    assert truncated is True


async def test_pv_monitor_exact_fill_is_not_truncated(monkeypatch: Any) -> None:
    """The complementary control: exactly max_events arrive, then the stream goes quiet →
    truncated=False. This is the case the pre-fix `>=` wrongly flagged as truncated."""
    monkeypatch.setattr(
        epics_client, "_format_value", lambda name, value: {"pv_name": name, "value": value}
    )
    monkeypatch.setattr(epics_client, "get_context", lambda: _MultiEventContext(3))

    events, truncated = await epics_client.pv_monitor("X:Y", duration=0.2, max_events=3)

    assert len(events) == 3
    assert truncated is False


# --- K4 bulkhead: pv_monitor runs on a dedicated executor, not the shared default pool ---


async def test_pv_monitor_runs_on_dedicated_executor(monkeypatch: Any) -> None:
    """K4 bulkhead: pv_monitor must run its blocking p4p subscription on the DEDICATED monitor
    executor, not the shared asyncio default executor. Otherwise >= monitor_max_concurrency
    concurrent monitors (each blocking up to max_monitor_duration = 60 s) occupy the whole default
    pool, and every other ``to_thread`` call (REST plane checks, PV reads/writes, the Olog write
    path) queues behind them, the server appears hung though nothing crashed. The worker thread's
    name proves which executor ran it. RED before K4 (``asyncio.to_thread`` → the default pool,
    whose threads are NOT named ``epics-monitor``)."""
    captured: dict[str, str] = {}

    class _ThreadNameContext:
        def monitor(self, name: str, cb: Any) -> _FakeSub:
            captured["thread"] = threading.current_thread().name
            return _FakeSub()

    monkeypatch.setattr(epics_client, "get_context", lambda: _ThreadNameContext())
    await epics_client.pv_monitor("X:Y", duration=0.0, max_events=1)

    assert captured["thread"].startswith("epics-monitor")


# ---------------------------------------------------------------------------
# pv_get_batch fallback (M5/C5): native batch fails → CONCURRENT individual gets,
# each outcome classified per element (a bad PV never crashes or serialises the batch).
# ---------------------------------------------------------------------------


class _FailBatchContext:
    """A p4p Context stand-in whose batch get always fails, forcing the individual fallback."""

    def get(self, names: object, timeout: object = None) -> object:
        raise RuntimeError("native batch get unsupported")


async def test_pv_get_batch_fallback_classifies_each_pv(monkeypatch: Any) -> None:
    """After the native batch fails, each PV is read individually and sorted good→results,
    disconnected→errors, one bad PV does not sink the healthy ones."""
    monkeypatch.setattr(epics_client, "get_context", _FailBatchContext)

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        if name == "BAD":
            raise PVTimeoutError(f"Timeout getting PV '{name}' after {timeout}s", details={})
        return {"pv_name": name, "value": 1}

    monkeypatch.setattr(epics_client, "pv_get", fake_pv_get)

    out = await epics_client.pv_get_batch(["GOOD", "BAD"])
    results, errors = out["results"], out["errors"]
    assert isinstance(results, list)
    assert isinstance(errors, list)
    assert [r["pv_name"] for r in results] == ["GOOD"]
    assert [e["pv_name"] for e in errors] == ["BAD"]
    assert "Timeout" in str(errors[0]["error"])


async def test_pv_get_batch_fallback_runs_concurrently(monkeypatch: Any) -> None:
    """M5: the individual fallback reads run CONCURRENTLY (asyncio.gather), not serially.

    Proven deterministically with a rendezvous (no wall-clock): the FIRST read blocks on an event
    that only the SECOND read sets. A serial for-loop would deadlock (FIRST never returns, so SECOND
    never starts), asyncio.wait_for turns that into a clean failure instead of a hang."""
    monkeypatch.setattr(epics_client, "get_context", _FailBatchContext)
    second_started = asyncio.Event()

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        if name == "FIRST":
            await second_started.wait()  # only satisfiable if SECOND runs concurrently
            return {"pv_name": name, "value": 1}
        second_started.set()
        return {"pv_name": name, "value": 2}

    monkeypatch.setattr(epics_client, "pv_get", fake_pv_get)

    out = await asyncio.wait_for(epics_client.pv_get_batch(["FIRST", "SECOND"]), timeout=2.0)
    results = out["results"]
    assert isinstance(results, list)
    assert {r["pv_name"] for r in results} == {"FIRST", "SECOND"}
    assert out["errors"] == []


class _NativeBatchContext:
    """A p4p Context stand-in whose batch get SUCCEEDS, returning one wrapped value per name, so
    the NATIVE happy path (ctxt.get(list) → zip → _format_value) is exercised, not the fallback."""

    def get(self, names: list[str], timeout: object = None) -> list[object]:
        return [_wrap(SimpleNamespace(value=float(i), alarm=None)) for i, _ in enumerate(names)]


async def test_pv_get_batch_native_success_path(monkeypatch: Any) -> None:
    """C5 coverage gap: the native-batch SUCCESS path (epics_client.py:104-108) was never run:
    both existing batch tests force the fallback via _FailBatchContext. Here the native get returns
    a list, so every name is formatted and lands in results with NO fallback and NO errors."""
    monkeypatch.setattr(epics_client, "get_context", _NativeBatchContext)

    out = await epics_client.pv_get_batch(["A:1", "B:2", "C:3"])
    results, errors = out["results"], out["errors"]
    assert isinstance(results, list)
    assert [r["pv_name"] for r in results] == ["A:1", "B:2", "C:3"]
    assert [r["value"] for r in results] == [0.0, 1.0, 2.0]
    assert errors == []


async def test_pv_get_batch_native_bad_value_isolated(monkeypatch: Any) -> None:
    """C5 coverage gap: in the native path, a value that _format_value cannot render must be routed
    to errors per-element (epics_client.py:109-111), NOT crash the whole batch. One PV booms; the
    others still land in results."""
    monkeypatch.setattr(epics_client, "get_context", _NativeBatchContext)

    def _boom_on_b(name: str, value: object) -> dict[str, object]:
        if name == "B:2":
            raise RuntimeError("malformed value")
        return {"pv_name": name, "value": "ok"}

    monkeypatch.setattr(epics_client, "_format_value", _boom_on_b)

    out = await epics_client.pv_get_batch(["A:1", "B:2", "C:3"])
    results, errors = out["results"], out["errors"]
    assert isinstance(results, list)
    assert isinstance(errors, list)
    assert [r["pv_name"] for r in results] == ["A:1", "C:3"]
    assert [e["pv_name"] for e in errors] == ["B:2"]
    assert "malformed value" in str(errors[0]["error"])


class _ShortBatchContext:
    """A p4p Context stand-in whose batch get returns FEWER values than requested names, a broken
    provider violating the length contract. Returns one wrapped value for any number of names."""

    def get(self, names: list[str], timeout: object = None) -> list[object]:
        return [_wrap(SimpleNamespace(value=0.0, alarm=None))]


async def test_pv_get_batch_native_length_mismatch_raises(monkeypatch: Any) -> None:
    """S27/F11: a provider that returns fewer values than requested names must fail LOUDLY with
    UPSTREAM_CONTRACT_ERROR, not silently drop the surplus PVs. Goes RED (no raise) against the
    pre-S27 code (native path, short list, zip strict=False -> 1 result / 0 errors / no raise)."""
    monkeypatch.setattr(epics_client, "get_context", _ShortBatchContext)
    with pytest.raises(EpicsError) as ei:
        await epics_client.pv_get_batch(["A:1", "B:2", "C:3"])
    assert ei.value.error_code == "UPSTREAM_CONTRACT_ERROR"
    assert ei.value.details == {"requested": 3, "received": 1}


# --- NTMatrix / NTMultiChannel against REAL p4p (same rationale as the DS-6 block above).
#     Pre-fix, both fell through to the generic converter, which kept ONLY the value field:
#     two semantically different matrices came out bit-identical, and an NTMultiChannel lost
#     its channel names and per-channel severities while the structurally-zero top-level
#     alarm read NO_ALARM next to a MAJOR channel. Inequality proofs on purpose, they hit
#     the bit-identity frontally and cannot be greened by cosmetic extra fields.


def _nt_matrix(flat: list[float], dim: list[int]) -> object:
    """A real NTMatrix Value (p4p has no NTMatrix wrapper class; ~10 lines by type id)."""
    from p4p import Type, Value

    t = Type([("value", "ad"), ("dim", "ai")], id="epics:nt/NTMatrix:1.0")
    return Value(t, {"value": flat, "dim": dim})


def test_format_value_real_p4p_ntmatrix_distinguishes_transposed_shapes() -> None:
    """A 2x3 and a 3x2 NTMatrix over the same flat values must NOT serialise identically:
    pre-fix the `dim` field was dropped and the shape was unrecoverable."""
    flat = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    two_by_three = _format_value("MAT:PV", _nt_matrix(flat, [2, 3]))
    three_by_two = _format_value("MAT:PV", _nt_matrix(flat, [3, 2]))

    assert two_by_three != three_by_two  # bit-identical pre-fix
    value = two_by_three["value"]
    assert isinstance(value, dict)
    assert value["shape"] == [2, 3]
    assert value["rows"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    json.dumps(two_by_three)


def test_format_value_real_p4p_ntmatrix_dim_mismatch_stays_flat_with_note() -> None:
    """A `dim` that does not multiply to len(value) must NOT be trusted into a wrong
    reshape: the values stay flat (one row) and an honest note declares the mismatch."""
    result = _format_value("MAT:PV", _nt_matrix([1.0, 2.0, 3.0], [2, 3]))

    value = result["value"]
    assert isinstance(value, dict)
    assert value["shape"] == [3]
    assert value["rows"] == [[1.0, 2.0, 3.0]]
    assert "note" in value
    json.dumps(result)


def _nt_multi_channel(
    names: list[str],
    severities: list[int],
    connected: list[bool],
    user_tags: list[int] | None = None,
) -> object:
    """A real NTMultiChannel Value. `NTMultiChannel.wrap()` raises NotImplementedError and
    `.type()` returns a Value PROTOTYPE (not a Type), hence Value(proto.type(), ...)."""
    import numpy as np
    from p4p import Value
    from p4p.nt import NTMultiChannel

    proto = NTMultiChannel("av").type()
    fields: dict[str, object] = {
        "value": [np.float64(1.0), np.float64(2.0), np.float64(3.0)],
        "channelName": names,
        "severity": severities,
        "isConnected": connected,
    }
    if user_tags is not None:
        fields["userTag"] = user_tags
    return Value(proto.type(), fields)


def test_format_value_real_p4p_ntmultichannel_surfaces_channels() -> None:
    """An NTMultiChannel must surface channelName + per-channel severity/connected as one
    record per channel. Pre-fix only the bare numbers survived: a MAJOR channel next to a
    disconnected one serialised identically to an all-quiet, all-connected triple, and the
    structural top-level alarm read NO_ALARM."""
    loud = _format_value(
        "MC:PV",
        _nt_multi_channel(["ROOM:TEMP", "ROOM:PRESS", "ROOM:FLOW"], [0, 2, 1], [True, False, True]),
    )
    quiet = _format_value(
        "MC:PV", _nt_multi_channel(["X:1", "X:2", "X:3"], [0, 0, 0], [False, False, False])
    )

    assert loud != quiet  # bit-identical pre-fix
    dump = json.dumps(loud)
    assert "ROOM:PRESS" in dump  # channel names were dropped entirely pre-fix
    value = loud["value"]
    assert isinstance(value, dict)
    channels = value["channels"]
    assert isinstance(channels, list)
    press = channels[1]
    assert isinstance(press, dict)
    assert press["name"] == "ROOM:PRESS"
    assert press["severity"] == 2
    assert press["severity_text"] == "MAJOR"  # the MAJOR that NO_ALARM hid pre-fix
    assert press["connected"] is False
    # The top-level alarm block of an NTMultiChannel is structural (says nothing about the
    # channels), the value must carry the note that points readers at channels[].
    assert "note" in value and "channels[]" in str(value["note"])


def test_format_value_real_p4p_ntmultichannel_surfaces_user_tag() -> None:
    """QA: `userTag` is an NT-spec per-channel parallel array too, the fix that promised
    "no more silently lost sibling fields" still dropped it."""
    result = _format_value(
        "MC:PV",
        _nt_multi_channel(["A", "B", "C"], [0, 0, 0], [True, True, True], user_tags=[7, 8, 9]),
    )
    value = result["value"]
    assert isinstance(value, dict)
    channels = value["channels"]
    assert isinstance(channels, list)
    second = channels[1]
    assert isinstance(second, dict)
    assert second["user_tag"] == 8


def test_format_value_extraction_failure_is_declared() -> None:
    """QA: a crashed value extraction left a bare ``{"value": None}``, indistinguishable
    from a genuinely-None reading for every consumer that condenses this dict further
    (validate/discover/write-readback/monitor). The fallback now declares itself in-band,
    following the established data_omitted/note honesty pattern."""

    class _Boom:
        @property
        def value(self) -> object:
            raise RuntimeError("boom")

    result = _format_value("BOOM:PV", _Boom())

    assert result["value"] is None
    assert "extraction failed" in str(result.get("note", ""))


def test_format_value_real_p4p_ntmatrix_scalar_value_is_kept() -> None:
    """A malformed NTMatrix whose value is a SCALAR must keep the scalar as one row:
    pre-fix it was silently dropped to ``rows=[[]]`` (shape [0])."""
    from p4p import Type, Value

    t = Type([("value", "d"), ("dim", "ai")], id="epics:nt/NTMatrix:1.0")
    result = _format_value("MAT:PV", Value(t, {"value": 7.5}))

    value = result["value"]
    assert isinstance(value, dict)
    assert value["shape"] == [1]
    assert value["rows"] == [[7.5]]
    json.dumps(result)


def test_extract_nt_matrix_non_integral_dim_is_not_trusted() -> None:
    """A non-integral `dim` (only constructible via fakes, the real wire type is int[])
    must not be int-truncated into a note that misquotes the wire: report flat + the RAW
    dim in the note."""
    from epics_mcp.services.epics_client import _extract_nt_matrix

    out = _extract_nt_matrix(SimpleNamespace(value=[1.0, 2.0, 3.0], dim=[1.5, 2.0]))

    assert out["shape"] == [3]
    assert out["rows"] == [[1.0, 2.0, 3.0]]
    assert "1.5" in str(out.get("note", ""))  # the RAW dim, not a truncated [1, 2]


def test_format_value_non_string_descriptor_is_omitted() -> None:
    """A malformed non-string descriptor must not be str()-serialised into p4p-repr
    garbage (this file forbids str() on p4p objects for exactly that reason)."""
    from p4p import Type, Value

    t = Type([("value", "d"), ("descriptor", ("S", None, [("a", "i")]))])
    result = _format_value("DESC:PV", Value(t, {"value": 1.0, "descriptor": {"a": 5}}))

    assert "descriptor" not in result


def test_format_value_foreign_typed_structs_are_not_marker_routed() -> None:
    """QA: a value with an EXPLICIT foreign type id must go through the generic converter,
    the structural markers (choices/dimension/labels/channelName) exist for id-less
    fakes and anonymous structs only. Pre-fix each marker captured foreign structs: a
    custom OptionSet was minted into an NTEnum with a FABRICATED index=0 contradicting
    its own data, a `dimension` sibling produced a wrong-shape NTNDArray summary that
    withheld the real values, and a `channelName` sibling got the NTMultiChannel note
    stamped onto a non-NTMultiChannel."""
    from p4p import Type, Value

    option_set = _format_value(
        "OPT:PV",
        Value(
            Type(
                [("value", ("S", None, [("choices", "as"), ("active", "s"), ("n", "i")]))],
                id="my:custom/OptionSet:1.0",
            ),
            {"value": {"choices": ["A", "B", "C"], "active": "B", "n": 3}},
        ),
    )
    assert "enum" not in option_set  # pre-fix: fabricated {"index": 0, "label": "A", ...}
    assert option_set["value"] == {"choices": ["A", "B", "C"], "active": "B", "n": 3}

    grid = _format_value(
        "GRID:PV",
        Value(
            Type([("value", "ad"), ("dimension", "ai")], id="my:custom/Grid:1.0"),
            {"value": [1.0, 2.0, 3.0], "dimension": [3]},
        ),
    )
    assert grid["value"] == [1.0, 2.0, 3.0]  # pre-fix: {"shape": [1], "data_omitted": true, ...}

    axes = _format_value(
        "AXES:PV",
        Value(
            Type([("value", "ad"), ("labels", "as")], id="my:custom/Axes:1.0"),
            {"value": [1.0, 2.0], "labels": ["x", "y"]},
        ),
    )
    assert axes["value"] == [1.0, 2.0]  # pre-fix: {"labels": [...], "columns": {}}

    roster = _format_value(
        "ROSTER:PV",
        Value(
            Type([("value", "ad"), ("channelName", "as")], id="my:custom/Roster:1.0"),
            {"value": [1.0, 2.0], "channelName": ["X", "Y"]},
        ),
    )
    assert roster["value"] == [1.0, 2.0]
    assert "channels" not in json.dumps(roster)  # pre-fix: misrouted + misleading note
    for result in (option_set, grid, axes, roster):
        json.dumps(result)


def test_format_value_anonymous_marker_structs_still_fall_back() -> None:
    """Counter-control: the markers stay a FALLBACK for ANONYMOUS values (a bare p4p
    struct reports getID() == "structure"; fakes have no getID at all), NT-shaped data
    from an id-stripping provider keeps its bespoke extraction."""
    from p4p import Type, Value

    anon = _format_value(
        "ANON:PV",
        Value(
            Type([("value", "ad"), ("channelName", "as")]),  # no id -> "structure"
            {"value": [1.0, 2.0], "channelName": ["X", "Y"]},
        ),
    )
    value = anon["value"]
    assert isinstance(value, dict)
    assert "channels" in value


def test_format_value_real_p4p_descriptor_surfaces() -> None:
    """The optional NT `descriptor` (free-text description every NT type may carry) must
    reach the output, pre-fix it was dropped for ALL NT types (no block extractor)."""
    from p4p import Type, Value

    t = Type([("value", "d"), ("descriptor", "s")], id="epics:nt/NTScalar:1.0")
    v = Value(t, {"value": 4.2, "descriptor": "simulated readback"})

    result = _format_value("DESC:PV", v)

    assert result["descriptor"] == "simulated readback"
    json.dumps(result)


def test_format_value_real_p4p_empty_descriptor_is_omitted() -> None:
    """An unset descriptor arrives as "" on the wire, it carries no information and must
    be omitted, not reported as an empty string."""
    from p4p import Type, Value

    t = Type([("value", "d"), ("descriptor", "s")], id="epics:nt/NTScalar:1.0")
    v = Value(t, {"value": 4.2})  # descriptor left unset -> "" default

    result = _format_value("DESC:PV", v)

    assert "descriptor" not in result
