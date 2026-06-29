"""Offline tests for the Phoebus Alarm Logger client + tools (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services.alarm_client import AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmConnectionError
from epics_pv_mcp.tools.alarm import _is_alarm_configured


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
    monkeypatch.setattr("epics_pv_mcp.tools.alarm.get_config", lambda: EpicsConfig(alarm_url=""))

    def _boom(*args: object, **kwargs: object) -> AlarmClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.alarm.AlarmClient", _boom)
    result = await _is_alarm_configured("X")
    assert result["enabled"] is False
    assert result["configured"] is None


@pytest.mark.asyncio
async def test_is_alarm_configured_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.alarm.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            return True, {"config": f"config:/{config_name}/C/{pv}"}

    monkeypatch.setattr("epics_pv_mcp.tools.alarm.AlarmClient", _Fake)
    result = await _is_alarm_configured("X")
    assert result["enabled"] is True
    assert result["configured"] is True
    assert result["config"] == "Accelerator"
