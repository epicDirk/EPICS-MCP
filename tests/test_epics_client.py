"""Tests für epics_client-Hilfsfunktionen (ohne EPICS-Verbindung außer dem echten-p4p-Test)."""

from types import SimpleNamespace
from typing import Any

import epics_pv_mcp.services.epics_client as epics_client
from epics_pv_mcp.errors import EpicsConnectionError, PVNotFoundError
from epics_pv_mcp.services.epics_client import _classify_p4p_error, _format_value
from epics_pv_mcp.tools.info import _get_pv_info


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
# _format_value — p4p unwrapped wrappers expose meta-data via ``.raw``.
# Fakes mirror that shape: a wrapper with ``.raw`` whose attributes are the
# NTScalar / NTEnum sub-structures (no live EPICS needed for the fake-based tests).
# ---------------------------------------------------------------------------


def _wrap(raw: SimpleNamespace) -> SimpleNamespace:
    """Mimic a p4p unwrapped value: the raw ``Value`` lives under ``.raw``."""
    return SimpleNamespace(raw=raw)


class _FakeArray:
    """Stands in for a numpy array — exposes ``tolist`` (real scalars are plain Python)."""

    def __init__(self, data: list[float]) -> None:
        self._data = data

    def tolist(self) -> list[float]:
        return list(self._data)


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


def test_format_value_string_scalar_carries_alarm() -> None:
    # A real string NTScalar DOES carry an alarm struct — the fake must too.
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


def test_value_alarm_without_active_field_treated_inactive() -> None:
    # Non-NT-conformant producer: limits present, no ``active`` field -> conservatively hidden.
    raw = SimpleNamespace(
        value=4.2,
        valueAlarm=SimpleNamespace(lowAlarmLimit=-5.0, highAlarmLimit=5.0),
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

    events = await epics_client.pv_monitor("X:Y", duration=0.2, max_events=1)

    assert events == [{"pv_name": "X:Y", "value": None}]
