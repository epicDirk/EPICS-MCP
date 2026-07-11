"""Offline tests for the Phoebus Alarm Logger client + tools (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services.alarm_client import AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmConnectionError
from epics_pv_mcp.services.redact import FREETEXT_WITHHELD
from epics_pv_mcp.tools.alarm import _get_alarm_history, _is_alarm_configured


def _resp(payload: object, *, ok: bool = True) -> Mock:
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- client ---


def test_is_alarm_configured_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # Realistic config-index doc: NO `pv` field (the config index never emits one); identity comes
    # from the leaf segment of the `config` path.
    client = AlarmClient("http://alarm:8081")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"config": "config:/Accelerator/DEV-TEST01/X", "enabled": True}])),
    )
    configured, detail = client.is_alarm_configured("X")
    assert configured is True
    assert detail["config"] == "config:/Accelerator/DEV-TEST01/X"


def test_is_alarm_configured_detail_strips_person_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY: a real config-change doc carries user/host (who changed it) — the returned detail
    must drop them (and any unknown field) while keeping the technical config."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/DEV-TEST01/X",
        "enabled": True,
        "delay": 5,
        "guidance": [{"title": "check", "details": "..."}],
        "user": "jdoe",
        "host": "console-host-3.example.org",
        "some_future_field": "leak?",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    configured, detail = client.is_alarm_configured("X")
    assert configured is True
    assert detail["config"] == "config:/Accelerator/DEV-TEST01/X"
    assert detail["enabled"] is True
    assert detail["delay"] == 5
    assert detail["guidance"] == FREETEXT_WITHHELD  # key kept, authored value withheld
    # person-bearing + unknown fields are gone
    assert "user" not in detail
    assert "host" not in detail
    assert "some_future_field" not in detail


def test_is_alarm_configured_withholds_authored_freetext(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY (defense-in-depth): if an alarm-logger ever surfaces the authored free-text
    fields FLAT at top level, each VALUE must be withheld (Olog treatment) — key kept, value gone.
    NOTE: the CURRENT upstream nests these inside ``config_msg`` (dropped by the allowlist — see
    ``..._drops_config_msg_person_data``); this only guards the hypothetical flat shape."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/Vacuum/Vac-VVMC-01:Pos-R",
        "enabled": True,
        "latching": True,
        "description": "Valve position alarm",
        "guidance": [{"title": "On-call", "details": "Call Jane Doe (vacuum group), +46 46 888"}],
        "displays": [{"title": "Vac overview", "details": "vac.bob"}],
        "commands": [{"title": "notify", "details": "email jane"}],
        "actions": [{"title": "Notify", "details": "mailto:jane.doe@example.org"}],
        "user": "eng.smith",
        "host": "ws-ctrl-042",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    _, detail = client.is_alarm_configured("Vac-VVMC-01:Pos-R")
    # authored free-text values are withheld — no person can leak inside the prose / a mailto action
    for field in ("description", "guidance", "displays", "commands", "actions"):
        assert detail[field] == FREETEXT_WITHHELD, field
    # technical fields pass through; audit metadata is gone
    assert detail["enabled"] is True
    assert detail["latching"] is True
    assert detail["config"] == "config:/Accelerator/Vacuum/Vac-VVMC-01:Pos-R"
    assert "user" not in detail
    assert "host" not in detail


def test_is_alarm_configured_drops_config_msg_person_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY (real upstream shape): a ``/search/alarm/config`` doc deserializes to the
    Phoebus ``AlarmLogMessage`` shape {config, user, host, enabled, config_msg, message_time}. The
    person data — who changed it (``user``/``host``) and the serialized ``AlarmConfigMessage``
    (``config_msg``, which embeds guidance prose / ``mailto:`` actions) — rides in fields that are
    NONE of them on the allowlist. The load-bearing drop is the allowlist projection: assert the
    three person-bearing fields are absent and only the technical fields remain."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/Vacuum/Vac-VVMC-01:Pos-R",
        "user": "eng.smith",
        "host": "ws-ctrl-042",
        "enabled": True,
        "config_msg": '{"guidance":[{"details":"Call Jane Doe (mailto:jane.doe@x.org)"}]}',
        "message_time": 1746093720000,
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    _, detail = client.is_alarm_configured("Vac-VVMC-01:Pos-R")
    assert detail == {
        "config": "config:/Accelerator/Vacuum/Vac-VVMC-01:Pos-R",
        "enabled": True,
        "message_time": 1746093720000,
    }
    for leaked in ("user", "host", "config_msg"):
        assert leaked not in detail


def test_is_alarm_configured_false_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    configured, detail = client.is_alarm_configured("X")
    assert configured is False
    assert detail == {}


def test_is_alarm_configured_false_on_leaf_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Substring over-match guard: a returned record whose config-leaf is a DIFFERENT PV (e.g. the
    # trailing-`*` query matched a sibling "XY") must NOT count as configured for "X".
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"config": "config:/Accelerator/C/XY"}])),
    )
    configured, _ = client.is_alarm_configured("X")
    assert configured is False


def test_is_alarm_configured_query_format(monkeypatch: pytest.MonkeyPatch) -> None:
    # Load-bearing: the config param MUST carry a leading slash + config name (the server does
    # config.split("/")[1] to pick the ES index) and span component nesting with "*".
    client = AlarmClient("http://alarm")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.is_alarm_configured("DEV-TEST01:Ctrl-EVR-01:Temp1Value", config_name="Accelerator")
    _, kwargs = getter.call_args
    assert kwargs["params"] == {"config": "/Accelerator/*DEV-TEST01:Ctrl-EVR-01:Temp1Value"}


def test_is_alarm_configured_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(AlarmConnectionError):
        client.is_alarm_configured("X")


# --- tools ---


@pytest.mark.asyncio
async def test_is_alarm_configured_tool_disabled_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gating + client construction moved to services/checkers.query_alarm_configured (M9);
    # _is_alarm_configured is a thin delegator, so config/client are patched there.
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="")
    )

    def _boom(*args: object, **kwargs: object) -> AlarmClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.AlarmClient", _boom)
    result = await _is_alarm_configured("X")
    assert result["enabled"] is False
    assert result["configured"] is None


@pytest.mark.asyncio
async def test_is_alarm_configured_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            return True, {"config": f"config:/{config_name}/C/{pv}"}

    monkeypatch.setattr("epics_pv_mcp.services.checkers.AlarmClient", _Fake)
    result = await _is_alarm_configured("X")
    assert result["enabled"] is True
    assert result["configured"] is True
    assert result["config"] == "Accelerator"


# --- get_alarm_history (DS-3): projection · capped · query params · privacy ---


def test_get_alarm_history_projects_technical_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A state/history event surfaces only the technical alarm data (newest-first as the server
    sorts message_time DESC)."""
    client = AlarmClient("http://alarm:8081")
    raw = [
        {
            "pv": "DEV-TEST01:X",
            "severity": "MAJOR",
            "message": "HIHI_ALARM",
            "value": "9.9",
            "time": "2026-06-01 10:00:00.000",
            "current_severity": "MAJOR",
            "current_message": "HIHI_ALARM",
            "enabled": True,
            "mode": "normal",
            "config": "state:/Accelerator/DEV-TEST01/X",
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, capped = client.get_alarm_history("DEV-TEST01:X", "2026-06-01", "2026-06-02")
    assert capped is False
    assert events[0]["severity"] == "MAJOR"
    assert events[0]["value"] == "9.9"
    assert events[0]["pv"] == "DEV-TEST01:X"


def test_get_alarm_history_strips_person_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY: an alarm state doc can carry user/host (WHO acknowledged/enabled/disabled) plus a
    command and a config_msg — the returned events must drop them (and any unknown field) while
    keeping the technical alarm data. Mirrors the is_alarm_configured allowlist guard."""
    client = AlarmClient("http://alarm:8081")
    raw = [
        {
            "pv": "DEV-TEST01:X",
            "severity": "MINOR",
            "message": "LOW_ALARM",
            "value": "1.0",
            "time": "2026-06-01 09:00:00.000",
            "current_severity": "OK",
            "current_message": "OK",
            "enabled": True,
            "user": "jdoe",
            "host": "console-host-3",
            "command": "Disabled",
            "config_msg": "possibly authored text",
            "some_future_field": "leak?",
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, _ = client.get_alarm_history("DEV-TEST01:X", "2026-06-01", "2026-06-02")
    event = events[0]
    assert event["severity"] == "MINOR"
    assert event["enabled"] is True
    for leaked in ("user", "host", "command", "config_msg", "some_future_field"):
        assert leaked not in event


def test_get_alarm_history_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """capped=True when the server returns MORE than max_events — the client fetches max_events+1
    (honest off-by-one) and keeps the newest max_events."""
    client = AlarmClient("http://alarm:8081")
    raw = [{"pv": "X", "severity": "MAJOR"} for _ in range(4)]  # max_events=3 → request 4, got 4
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, capped = client.get_alarm_history("X", "2026-06-01", "2026-06-02", max_events=3)
    assert capped is True
    assert len(events) == 3


def test_get_alarm_history_exactly_max_events_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: the server returns EXACTLY max_events records (fewer than the max_events+1 we
    requested) → the window is complete, capped=False. Pins the honest strict ``>`` — a ``>=``
    regression would false-flag exactly max_events real events as truncated (which the
    size=max_events+1 idiom exists to avoid), and every OTHER capped test survives that mutation."""
    client = AlarmClient("http://alarm:8081")
    raw = [{"pv": "X", "severity": "MAJOR"} for _ in range(3)]  # exactly max_events=3 (requested 4)
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, capped = client.get_alarm_history("X", "2026-06-01", "2026-06-02", max_events=3)
    assert capped is False
    assert len(events) == 3


def test_get_alarm_history_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """pv/start/end pass through; the endpoint is /search/alarm; size = max_events+1 so capped is an
    honest fetched>max_events."""
    client = AlarmClient("http://alarm:8081")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.get_alarm_history("DEV:X", "2026-06-01", "2026-06-02", max_events=50)
    args, kwargs = getter.call_args
    assert args[0] == "http://alarm:8081/search/alarm"  # the state/history endpoint, not /config
    assert kwargs["params"] == {
        "pv": "DEV:X",
        "start": "2026-06-01",
        "end": "2026-06-02",
        "size": "51",
    }


def test_get_alarm_history_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(AlarmConnectionError):
        client.get_alarm_history("X", "2026-06-01", "2026-06-02")


@pytest.mark.asyncio
async def test_get_alarm_history_tool_disabled_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="")
    )

    def _boom(*args: object, **kwargs: object) -> AlarmClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.AlarmClient", _boom)
    result = await _get_alarm_history("X", "2026-06-01", "2026-06-02")
    assert result["enabled"] is False
    assert result["events"] == []


@pytest.mark.asyncio
async def test_get_alarm_history_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_alarm_history(
            self, pv: str, start: str, end: str, max_events: int = 100
        ) -> tuple[list[dict[str, object]], bool]:
            return [{"severity": "MAJOR", "pv": pv}], True

    monkeypatch.setattr("epics_pv_mcp.services.checkers.AlarmClient", _Fake)
    result = await _get_alarm_history("X", "2026-06-01", "2026-06-02")
    assert result["enabled"] is True
    assert result["total"] == 1
    assert result["capped"] is True
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["severity"] == "MAJOR"


# --- check_connectivity (E2 doctor probe) ---


def test_check_connectivity_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP response to a HEAD on the root = reachable (transport + CA proven)."""
    client = AlarmClient("http://alarm:8081")
    monkeypatch.setattr(client.session, "head", Mock(return_value=Mock()))
    assert client.check_connectivity() is True


def test_check_connectivity_raises_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AlarmClient("http://alarm:8081")
    monkeypatch.setattr(
        client.session, "head", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(AlarmConnectionError):
        client.check_connectivity()
