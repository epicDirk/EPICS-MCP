"""Tests für epics_client-Hilfsfunktionen (ohne EPICS-Verbindung)."""

from types import SimpleNamespace

from epics_pv_mcp.errors import EpicsConnectionError, PVNotFoundError
from epics_pv_mcp.services.epics_client import _classify_p4p_error, _format_value


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
# Fakes mirror that shape (no live EPICS needed): a wrapper with ``.raw`` whose
# attributes are the NTScalar / NTEnum sub-structures.
# ---------------------------------------------------------------------------


def _wrap(raw: SimpleNamespace) -> SimpleNamespace:
    """Mimic a p4p unwrapped value: the raw ``Value`` lives under ``.raw``."""
    return SimpleNamespace(raw=raw)


class _FakeArray:
    """Stands in for a numpy array — exposes ``tolist`` but not a usable ``item``."""

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
            lowAlarmLimit=0.5, lowWarningLimit=1.0, highWarningLimit=7.0, highAlarmLimit=9.0
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


def test_format_value_string_scalar() -> None:
    raw = SimpleNamespace(value="hello")

    result = _format_value("STR:PV", _wrap(raw))

    assert result["value"] == "hello"
    assert "alarm" not in result


def test_format_value_unknown_alarm_codes_fall_back_to_str() -> None:
    raw = SimpleNamespace(value=0.0, alarm=SimpleNamespace(severity=9, status=42))

    result = _format_value("X:Y", _wrap(raw))

    alarm = result["alarm"]
    assert isinstance(alarm, dict)
    assert alarm["severity_text"] == "9"
    assert alarm["status_text"] == "42"
    assert "message" not in alarm  # absent on the fake -> not surfaced


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
