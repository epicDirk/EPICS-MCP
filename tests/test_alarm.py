"""Offline tests for the Phoebus Alarm Logger client + tools (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_mcp.config import EpicsConfig
from epics_mcp.services._time_window import TimeWindowFormatError
from epics_mcp.services.alarm_client import AlarmClient
from epics_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmResponseError
from epics_mcp.services.redact import FREETEXT_WITHHELD
from epics_mcp.tools.alarm import _get_alarm_history, _is_alarm_configured


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
    configured, detail = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is True
    assert detail["config"] == "config:/Accelerator/DEV-TEST01/X"


def test_is_alarm_configured_detail_strips_person_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY: a real config-change doc carries user/host (who changed it), the returned detail
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
    configured, detail = client.is_alarm_configured("X", config_name="Accelerator")
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
    fields FLAT at top level, each VALUE must be withheld (Olog treatment), key kept, value gone.
    NOTE: the CURRENT upstream nests these inside ``config_msg`` (dropped by the allowlist, see
    ``..._drops_config_msg_person_data``); this only guards the hypothetical flat shape."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R",
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
    _, detail = client.is_alarm_configured("SIM:Vac-Vlv-01:Pos-R", config_name="Accelerator")
    # authored free-text values are withheld, no person can leak inside the prose / a mailto action
    for field in ("description", "guidance", "displays", "commands", "actions"):
        assert detail[field] == FREETEXT_WITHHELD, field
    # technical fields pass through; audit metadata is gone
    assert detail["enabled"] is True
    assert detail["latching"] is True
    assert detail["config"] == "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R"
    assert "user" not in detail
    assert "host" not in detail


def test_is_alarm_configured_drops_config_msg_person_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY (real upstream shape): a ``/search/alarm/config`` doc deserializes to the
    Phoebus ``AlarmLogMessage`` shape {config, user, host, enabled, config_msg, message_time}. The
    person data, who changed it (``user``/``host``) and the serialized ``AlarmConfigMessage``
    (``config_msg``, which embeds guidance prose / ``mailto:`` actions), rides in fields that are
    NONE of them on the allowlist. The load-bearing drop is the allowlist projection: assert the
    three person-bearing fields are absent and only the technical fields remain."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R",
        "user": "eng.smith",
        "host": "ws-ctrl-042",
        "enabled": True,
        "config_msg": '{"guidance":[{"details":"Call Jane Doe (mailto:jane.doe@x.org)"}]}',
        "message_time": 1746093720000,
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    _, detail = client.is_alarm_configured("SIM:Vac-Vlv-01:Pos-R", config_name="Accelerator")
    assert detail == {
        "config": "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R",
        "enabled": True,
        "message_time": 1746093720000,
    }
    for leaked in ("user", "host", "config_msg"):
        assert leaked not in detail


def test_is_alarm_configured_false_when_tree_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real negative: the PV query is empty, but the tree itself HAS configuration → the PV is
    # genuinely not configured. This is the only shape that may still be reported as False.
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(side_effect=[_resp([]), _resp([{"config": "config:/Accelerator/C/Other"}])]),
    )
    configured, detail = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is False
    assert detail == {}


def test_is_alarm_configured_withheld_when_tree_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bug this replaces: an empty answer used to be reported as False. Measured live, a
    # mis-cased or unknown config_name is answered EXACTLY like a genuinely unconfigured PV
    # (200 + []), so False was a guess dressed as a fact. Both queries empty → withheld (None).
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    configured, detail = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is None
    assert detail == {}


def test_is_alarm_configured_hit_does_not_probe_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # The extra request is on the MISS path only, a hit already proves the tree was read as
    # intended, so the common case still costs exactly one round trip.
    client = AlarmClient("http://alarm")
    getter = Mock(return_value=_resp([{"config": "config:/Accelerator/C/X"}]))
    monkeypatch.setattr(client.session, "get", getter)
    configured, _ = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is True
    assert getter.call_count == 1


# --- client: strict response schema (S11), unreadable 2xx is NEVER a definitive answer ---
#
# Measured payload shapes (local Alarm Logger 5.0.052, live 2026-07-16): /search/alarm returns a
# list whose docs ALL carry a string `config` (state: docs additionally pv/severity/..., config:
# docs config_msg/...); /search/alarm/config likewise. `config` is the identity field the client
# reads, it is the schema anchor.


@pytest.mark.parametrize(
    "payload",
    [{}, "nope", 123, {"unexpected": "shape"}],
    ids=["dict", "string", "number", "unrelated-dict"],
)
def test_is_alarm_configured_unreadable_payload_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: a non-list 2xx main payload must RAISE, it used to be read as ``[]`` (a miss) and
    fall through to the tree probe, where an answering tree turned it into a DEFINITIVE
    ``False``. Unreadable must never reach the tree probe."""
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(side_effect=[_resp(payload), _resp([{"config": "config:/Accelerator/C/Other"}])]),
    )
    with pytest.raises(AlarmResponseError):
        client.is_alarm_configured("X", config_name="Accelerator")


@pytest.mark.parametrize(
    "payload",
    [[123], [{"unexpected": "shape"}], [{"config": 7}]],
    ids=["non-dict-record", "record-without-config", "non-str-config"],
)
def test_is_alarm_configured_unreadable_record_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11 (plan-review finding A1): unreadable records INSIDE a list were silently dropped →
    miss → answering tree → DEFINITIVE ``False`` from junk. Every record must be a dict carrying
    a string ``config``; junk raises, it never silently shrinks the answer."""
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(side_effect=[_resp(payload), _resp([{"config": "config:/Accelerator/C/Other"}])]),
    )
    with pytest.raises(AlarmResponseError):
        client.is_alarm_configured("X", config_name="Accelerator")


def test_is_alarm_configured_junk_tree_probe_withholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: a MISS whose tree probe returns junk must stay withheld (None), never a definitive
    ``False``: junk is no proof the tree name was read as intended."""
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(side_effect=[_resp([]), _resp([123])]),
    )
    configured, detail = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is None
    assert detail == {}


@pytest.mark.parametrize(
    "payload",
    [{}, "nope", 123, {"unexpected": "shape"}],
    ids=["dict", "string", "number", "unrelated-dict"],
)
def test_get_alarm_history_unreadable_payload_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: an unreadable 2xx history payload must RAISE, it used to read as ``([], False)``,
    indistinguishable from "no alarms in the window" (auditor probe ALARM_HISTORY_BAD_2XX)."""
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(AlarmResponseError):
        client.get_alarm_history("X", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")


@pytest.mark.parametrize(
    "payload",
    [[123], [{"unexpected": "shape"}], [{"config": 7}]],
    ids=["non-dict-record", "record-without-config", "non-str-config"],
)
def test_get_alarm_history_unreadable_record_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: junk records in the history list were silently dropped (a fabricated, smaller
    history). Every record must be a dict carrying a string ``config`` (measured: BOTH doc
    types, state: and config:, always carry it)."""
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(AlarmResponseError):
        client.get_alarm_history("X", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")


def test_get_alarm_history_empty_list_is_a_real_empty_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: a measured ``[]`` stays a genuinely empty window, not an error (the
    S14 false-red lesson)."""
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    events, capped = client.get_alarm_history("X", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert events == []
    assert capped is False


def test_is_alarm_configured_false_on_leaf_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Substring over-match guard: a returned record whose config-leaf is a DIFFERENT PV (e.g. the
    # trailing-`*` query matched a sibling "XY") must NOT count as configured for "X".
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"config": "config:/Accelerator/C/XY"}])),
    )
    configured, _ = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is False


def test_is_alarm_configured_query_format(monkeypatch: pytest.MonkeyPatch) -> None:
    # Load-bearing: the config param MUST carry a leading slash + config name (the server does
    # config.split("/")[1] to pick the ES index) and span component nesting with "*". The tree
    # probe on the miss path asks the same shape WITHOUT the PV, it must select the same index,
    # or it would answer for a different tree than the one being judged.
    client = AlarmClient("http://alarm")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.is_alarm_configured("DEV-TEST01:Ctrl-EVR-01:Temp1Value", config_name="Accelerator")
    sent = [call.kwargs["params"] for call in getter.call_args_list]
    assert sent == [
        {"config": "/Accelerator/*DEV-TEST01:Ctrl-EVR-01:Temp1Value"},
        {"config": "/Accelerator/*"},
    ]


def test_is_alarm_configured_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AlarmClient("http://alarm")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(AlarmConnectionError):
        client.is_alarm_configured("X", config_name="Accelerator")


# --- tools ---


@pytest.mark.asyncio
async def test_is_alarm_configured_tool_disabled_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gating + client construction moved to services/checkers.query_alarm_configured (M9);
    # _is_alarm_configured is a thin delegator, so config/client are patched there.
    monkeypatch.setattr("epics_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url=""))

    def _boom(*args: object, **kwargs: object) -> AlarmClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_mcp.services.checkers.AlarmClient", _boom)
    result = await _is_alarm_configured("X", "Accelerator")
    assert result["enabled"] is False
    assert result["configured"] is None


@pytest.mark.asyncio
async def test_is_alarm_configured_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            return True, {"config": f"config:/{config_name}/C/{pv}"}

    monkeypatch.setattr("epics_mcp.services.checkers.AlarmClient", _Fake)
    result = await _is_alarm_configured("X", "Accelerator")
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
    command and a config_msg, the returned events must drop them (and any unknown field) while
    keeping the technical alarm data. Mirrors the is_alarm_configured allowlist guard."""
    client = AlarmClient("http://alarm:8081")
    raw = [
        {
            "config": "state:/Accelerator/DEV-TEST01/X",  # measured: every doc carries `config`
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
    """capped=True when the server returns MORE than max_events, the client fetches max_events+1
    (honest off-by-one) and keeps the newest max_events."""
    client = AlarmClient("http://alarm:8081")
    # max_events=3 → request 4, got 4; `config` = the measured always-present anchor (S11)
    raw = [{"config": "state:/Accelerator/C/X", "pv": "X", "severity": "MAJOR"} for _ in range(4)]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, capped = client.get_alarm_history("X", "2026-06-01", "2026-06-02", max_events=3)
    assert capped is True
    assert len(events) == 3


def test_get_alarm_history_exactly_max_events_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: the server returns EXACTLY max_events records (fewer than the max_events+1 we
    requested) → the window is complete, capped=False. Pins the honest strict ``>``, a ``>=``
    regression would false-flag exactly max_events real events as truncated (which the
    size=max_events+1 idiom exists to avoid), and every OTHER capped test survives that mutation."""
    client = AlarmClient("http://alarm:8081")
    # exactly max_events=3 (requested 4); `config` = the measured always-present anchor (S11)
    raw = [{"config": "state:/Accelerator/C/X", "pv": "X", "severity": "MAJOR"} for _ in range(3)]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, capped = client.get_alarm_history("X", "2026-06-01", "2026-06-02", max_events=3)
    assert capped is False
    assert len(events) == 3


def test_get_alarm_history_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """pv passes through, start/end are NORMALIZED to zone-explicit ISO; the endpoint is
    /search/alarm; size = max_events+1 so capped is an honest fetched>max_events.

    This test previously asserted that start/end 'pass through' unchanged, pinning the very
    behaviour that was broken. The Alarm Logger reads a bare wall clock in ITS OWN zone and reads
    a zone-less ISO not at all (silently as 'now' -> 200 + empty). Sending the zone removes both
    ambiguities; see services/alarm_time. Do not restore the pass-through.
    """
    client = AlarmClient("http://alarm:8081")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.get_alarm_history("DEV:X", "2026-06-01", "2026-06-02", max_events=50)
    args, kwargs = getter.call_args
    assert args[0] == "http://alarm:8081/search/alarm"  # the state/history endpoint, not /config
    assert kwargs["params"] == {
        "pv": "DEV:X",
        "start": "2026-06-01T00:00:00.000Z",
        "end": "2026-06-02T00:00:00.000Z",
        "size": "51",
    }


def test_get_alarm_history_naive_iso_gains_the_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE alarm regression, at the wire level.

    Measured live against a real Alarm Logger: 'start=2026-07-08T12:45:58Z' returned events while
    the identical 'start=2026-07-08T12:45:58' returned 0, the zone-less form matches none of the
    server's parsers and degrades to 'now'. A 7-day window is far too wide for a mere zone shift to
    empty, so this is the collapse, not an offset. It is also the most likely wrong value there is:
    datetime.now().isoformat() emits exactly this.
    """
    client = AlarmClient("http://alarm:8081")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.get_alarm_history("DEV:X", "2026-07-08T12:45:58", "2026-07-15T12:45:58")
    params = getter.call_args.kwargs["params"]
    assert params["start"] == "2026-07-08T12:45:58.000Z"
    assert params["end"] == "2026-07-15T12:45:58.000Z"


def test_get_alarm_history_relative_amount_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative amount is the server's to resolve, its clock owns its data."""
    client = AlarmClient("http://alarm:8081")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.get_alarm_history("DEV:X", "8 hours", "now")
    params = getter.call_args.kwargs["params"]
    assert params["start"] == "8 hours"
    assert params["end"] == "now"


def test_get_alarm_history_bad_time_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value the server would misread is refused BEFORE any I/O.

    '500 millis' is the sharpest case: measured live it RETURNS DATA, for a 500-MINUTE window,
    because the unit dispatch tests startsWith("mi") before equals("ms"). Wrong data beats no data
    only in the sense that it is harder to notice.
    """
    client = AlarmClient("http://alarm:8081")

    def _fail(*_a: object, **_k: object) -> Mock:
        raise AssertionError("a request was made with an unusable time value")

    monkeypatch.setattr(client.session, "get", _fail)
    for bad in ("500 millis", "5 m", "garbage", "1 year"):
        with pytest.raises(TimeWindowFormatError):
            client.get_alarm_history("DEV:X", bad, "now")


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
    monkeypatch.setattr("epics_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url=""))

    def _boom(*args: object, **kwargs: object) -> AlarmClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_mcp.services.checkers.AlarmClient", _boom)
    result = await _get_alarm_history("X", "2026-06-01", "2026-06-02")
    assert result["enabled"] is False
    assert result["events"] == []


@pytest.mark.asyncio
async def test_get_alarm_history_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_alarm_history(
            self, pv: str, start: str, end: str, max_events: int = 100, **kwargs: object
        ) -> tuple[list[dict[str, object]], bool]:
            return [{"severity": "MAJOR", "pv": pv}], True

    monkeypatch.setattr("epics_mcp.services.checkers.AlarmClient", _Fake)
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


# --- MA-2b(d): the alarm tree is required (no silent 'Accelerator' default that matches nothing) --


async def test_is_alarm_configured_tool_requires_config_name() -> None:
    """MA-2b(d): the alarm tree is a REQUIRED tool parameter, no silent 'Accelerator' default that
    matches nothing at a real facility (is_alarm_configured would else always withhold). Mutant
    (a default restored) -> config_name drops out of the schema's 'required' -> this fails."""
    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    tool = next(t for t in tools if t.name == "is_alarm_configured")
    required = tool.inputSchema.get("required", [])
    assert "pv_name" in required
    assert "config_name" in required


async def test_query_alarm_configured_without_tree_withholds_no_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA-2b(d): with no tree named, query_alarm_configured withholds honestly instead of probing a
    guessed default tree, the AlarmClient must NOT even be constructed (no network for a guess)."""
    from epics_mcp.services import checkers

    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("no client / no guessed-tree probe when the tree is None")

    monkeypatch.setattr(checkers, "AlarmClient", _boom)
    result = await checkers.query_alarm_configured("SIM:PV-NoTree")
    assert result["configured"] is None
    assert result.get("withheld") is True


# --- MA-2b(a/b/c): server-side alarm-history filters root / command / severity -----------------
# Source-verified (AlarmLogSearchUtil.java): root -> config-field OR over state:/config: + index
# narrowing; command Enabled/Disabled -> the `enabled` keyword field (true/false) on BOTH doc types;
# severity/current_severity -> wildcard on the respective keyword field. An UNSUPPORTED param is
# silently ignored server-side (broadens), so the tool boundary Literal-restricts what it can.


def test_get_alarm_history_forwards_server_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """MA-2b(a/c): root/severity/current_severity are forwarded as server-side query params (a
    single GET, no tree-probe). Mutant (a filter not added to params) -> missing key -> fails."""
    client = AlarmClient("http://alarm")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.get_alarm_history(
        "X", "2026-06-01", "2026-06-02", root="DTL", severity="MAJOR", current_severity="OK"
    )
    params = getter.call_args.kwargs["params"]
    assert params["root"] == "DTL"
    assert params["severity"] == "MAJOR"
    assert params["current_severity"] == "OK"


def test_get_alarm_history_omits_unset_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """No filter set -> the param is absent, preserving today's all-trees/all-severity search."""
    client = AlarmClient("http://alarm")
    getter = Mock(return_value=_resp([]))
    monkeypatch.setattr(client.session, "get", getter)
    client.get_alarm_history("X", "2026-06-01", "2026-06-02")
    params = getter.call_args.kwargs["params"]
    assert "root" not in params
    assert "severity" not in params
    assert "current_severity" not in params
    assert "command" not in params


def test_get_alarm_history_command_restricts_to_config_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA-2b(b): command= filters the `enabled` field on BOTH doc types; a state doc carries
    enabled=false intrinsically, so the client restricts results to config: docs so 'which configs
    are disabled' is not swamped by state-change events. Mutant (no config restriction) -> the state
    doc leaks -> this fails. The command value is also forwarded to the server."""
    client = AlarmClient("http://alarm")
    raw = [
        {"config": "state:/DTL/DEV/X", "pv": "X", "severity": "MAJOR", "enabled": False},
        {"config": "config:/DTL/DEV/X", "pv": "X", "enabled": False},
    ]
    getter = Mock(return_value=_resp(raw))
    monkeypatch.setattr(client.session, "get", getter)
    events, _ = client.get_alarm_history("X", "2026-06-01", "2026-06-02", command="Disabled")
    assert getter.call_args.kwargs["params"]["command"] == "Disabled"
    assert [event["config"] for event in events] == ["config:/DTL/DEV/X"]


async def test_get_alarm_history_tool_severity_and_command_are_enums() -> None:
    """MA-2b(b/c): command/severity/current_severity are Literal-restricted at the tool boundary
    (structural typo-rejection, an unsupported value would otherwise be silently ignored by the
    server and broaden). Mutant (free str) -> the enum vanishes from the schema -> this fails."""
    import json

    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    tool = next(t for t in tools if t.name == "get_alarm_history")
    props = tool.inputSchema["properties"]
    command_schema = json.dumps(props["command"])
    assert "Enabled" in command_schema and "Disabled" in command_schema
    severity_schema = json.dumps(props["severity"])
    assert (
        "MAJOR" in severity_schema
        and "MINOR_ACK" in severity_schema
        and "UNDEFINED" in severity_schema
    )


def test_get_alarm_history_command_capped_survives_config_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA(2026-07-22): the client-side config: filter must NOT consume the size=max_events+1
    over-fetch sentinel, a truncated window whose newest page holds a dropped state: doc must
    still report capped=True. Mutant (capped computed AFTER the filter) -> capped=False on a
    truncated window -> this fails (the silent false-completeness the QA found)."""
    client = AlarmClient("http://alarm")
    # max_events=2 -> the client requests size=3; the server returns 3 newest docs (the window IS
    # truncated), one of them a state: doc the config: filter drops.
    raw = [
        {"config": "config:/Accelerator/DEV/A", "pv": "A", "enabled": False},
        {"config": "state:/Accelerator/DEV/B", "pv": "B", "enabled": False},
        {"config": "config:/Accelerator/DEV/C", "pv": "C", "enabled": False},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, capped = client.get_alarm_history(
        "A", "2026-06-01", "2026-06-02", max_events=2, command="Disabled"
    )
    assert capped is True  # window truncated (3 > 2) though the filter left exactly 2 config docs
    assert len(events) == 2
    assert all(str(event["config"]).startswith("config:") for event in events)
