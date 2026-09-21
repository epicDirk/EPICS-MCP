"""Offline tests for the Phoebus Alarm Logger client + tools (no network)."""

import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from epics_mcp.config import EpicsConfig
from epics_mcp.services import alarm_client
from epics_mcp.services._time_window import TimeWindowFormatError
from epics_mcp.services.alarm_client import AlarmClient
from epics_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmResponseError
from epics_mcp.tools.alarm import _get_alarm_history, _is_alarm_configured
from tests import test_alarm_live
from tests.wire_tools import wire_tools


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


def test_is_alarm_configured_detail_drops_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is STRUCTURE: known AlarmLogMessage fields (incl. user/host) ride through
    with their values, an unknown field a future logger version adds is dropped."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/DEV-TEST01/X",
        "enabled": True,
        "delay": 5,
        "guidance": [{"title": "check", "details": "..."}],
        "user": "jdoe",
        "host": "console-host-3.example.org",
        "some_future_field": "unknown",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    configured, detail = client.is_alarm_configured("X", config_name="Accelerator")
    assert configured is True
    assert detail["config"] == "config:/Accelerator/DEV-TEST01/X"
    assert detail["enabled"] is True
    assert detail["delay"] == 5
    assert detail["guidance"] == [{"title": "check", "details": "..."}]
    assert detail["user"] == "jdoe"
    assert detail["host"] == "console-host-3.example.org"
    assert "some_future_field" not in detail


def test_is_alarm_configured_surfaces_authored_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSITIVE PIN (decision PI, 2026-08-01): the authored fields carry their VALUES. The former
    free-text withholding made exactly the actionable half, the handling GUIDANCE, unreadable;
    re-hooking it turns this red. All tokens synthetic."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R",
        "enabled": True,
        "latching": True,
        "description": "Valve position alarm",
        "guidance": [{"title": "On-call", "details": "Call the vacuum group, +46 46 888"}],
        "displays": [{"title": "Vac overview", "details": "vac.bob"}],
        "commands": [{"title": "notify", "details": "run notify script"}],
        "actions": [{"title": "Notify", "details": "mailto:vacuum-oncall@example.org"}],
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    _, detail = client.is_alarm_configured("SIM:Vac-Vlv-01:Pos-R", config_name="Accelerator")
    for field in ("description", "guidance", "displays", "commands", "actions"):
        assert detail[field] == raw[field], field
    assert detail["enabled"] is True
    assert detail["latching"] is True
    assert detail["config"] == "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R"


def test_is_alarm_configured_surfaces_config_msg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REAL upstream shape: a ``/search/alarm/config`` doc deserializes to the Phoebus
    ``AlarmLogMessage`` shape {config, user, host, enabled, config_msg, message_time}, and the
    guidance rides serialized inside ``config_msg``. That field must come back readable, it is
    the handling instruction (decision PI)."""
    client = AlarmClient("http://alarm:8081")
    raw = {
        "config": "config:/Accelerator/Vacuum/SIM:Vac-Vlv-01:Pos-R",
        "user": "eng.smith",
        "host": "ws-ctrl-042",
        "enabled": True,
        "config_msg": '{"guidance":[{"details":"Call the vacuum on-call phone"}]}',
        "message_time": 1746093720000,
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([raw])))
    _, detail = client.is_alarm_configured("SIM:Vac-Vlv-01:Pos-R", config_name="Accelerator")
    assert detail == raw


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


# --- GQ-459: a False is a MISS in a change-log, and the answer has to say so ----------------------
# The tree probe proves only that the tree NAME was read, never that its change-log is complete:
# the server wraps every pattern in stars, so "/{tree}/*" matches on a single surviving document
# and every other PV then falls to a definitive False. Measured 2026-09-20 in tree ACCP: 0 of 8 PVs
# with alarm activity in the last seven days were reported configured, while the whole tree's most
# recent change document was from 2026-07-27. The verdict stays False (callers and coverage_audit
# depend on a provable "no"), but it no longer travels without the caveat.


def _fake_alarm_client(verdict: bool | None) -> type:
    """An AlarmClient stand-in whose is_alarm_configured returns *verdict* for any PV."""

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool | None, dict[str, object]]:
            return verdict, {}

    return _Fake


@pytest.mark.asyncio
async def test_is_alarm_configured_false_carries_the_change_log_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A False must say what it is worth: the index searched is a change-log, not the config."""
    monkeypatch.setattr(
        "epics_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )
    monkeypatch.setattr("epics_mcp.services.checkers.AlarmClient", _fake_alarm_client(False))

    result = await _is_alarm_configured("X", "Accelerator")

    assert result["configured"] is False
    note = result.get("note")
    assert note is not None, (
        "a proven-looking False must carry a note saying what the No is worth "
        "(the config index is a change-log; a missing document is not a missing configuration)"
    )
    # Both failure directions, because the endpoint errs both ways: a change document can be
    # absent for a configured PV, and it SURVIVES for a deleted one (the logger drops Kafka
    # tombstones), so the caveat may not present itself as a one-directional caveat.
    assert "change-log" in note
    assert "deleted" in note


@pytest.mark.asyncio
async def test_is_alarm_configured_true_carries_no_miss_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: a tool that staples the caveat onto EVERY answer would pass above."""
    monkeypatch.setattr(
        "epics_mcp.services.checkers.get_config", lambda: EpicsConfig(alarm_url="http://alarm")
    )
    monkeypatch.setattr("epics_mcp.services.checkers.AlarmClient", _fake_alarm_client(True))

    result = await _is_alarm_configured("X", "Accelerator")

    assert result["configured"] is True
    assert result.get("note") is None, "a hit proves the PV is configured; it needs no caveat"


# --- GQ-459: a miss has several causes, and every page that qualifies one must name them ---------
# The repo used to carry one precondition, "the Alarm Logger was running at config-import time",
# and to present it as the whole of it. Measured at the source: a change document is equally
# absent for a config that was never CHANGED since import, and the purge component deletes whole
# indices when it is switched on. Naming one cause as the only one certifies the No it means to
# qualify, which is why this is a guard and not a one-off edit.
#
# TWO guards, because one of them can only do half the job.
#
# The POSITIVE one is the load-bearing half: each page that explains what a miss is worth must
# name at least two causes. That cannot be dodged by rewording, because it asks for content.
# The NEGATIVE one catches the two historical wordings coming back. ⚠ It is a WORDING guard and
# nothing more, and this comment says so rather than letting the test name imply otherwise: the
# claim survives a swapped clause order ("only if the tree was imported while the logger was
# running"), a synonym for "running" outside its small word field, and a full stop between the
# two halves. A regex cannot hold "names one cause as the only one"; the positive guard is what
# holds that, from the other side.

_LOGGER_UP = r"(?:running|up|live|active|started)"
# Both clause orders, because either reads naturally: only ... running ... import, and
# only ... import ... running. Bounded by the sentence (``[^.]``) so a match cannot span one.
# ⚠ The sentence must also mention the LOGGER. Without that conjunct, measured on this tree, the
# widened word field fired on cli_common.py, on a sentence about registering display tools "only
# when the ENGINE imports, keep the core server UP": three words of the pattern, nothing to do
# with alarms. "import" is overloaded in a Python tree, so the alarm context is what makes the
# guard about its subject rather than about three common words.
_GAP = r"[^.]{0,140}"
_ONLY_AT_IMPORT = re.compile(
    "|".join(
        rf"only\b{_GAP}" + rf"\b{_GAP}".join(order)
        for order in (
            (r"logger", _LOGGER_UP, r"import"),
            (r"logger", r"import(?:ed)?", _LOGGER_UP),
            (r"import(?:ed)?", r"logger", _LOGGER_UP),
        )
    ),
    re.IGNORECASE,
)
# The causes a page must name at least two of, each by a phrase that cannot be confused with the
# others. "aged out" and "purge" are one cause (the document is gone), spelled two ways.
_MISS_CAUSES: dict[str, tuple[str, ...]] = {
    "not configured": ("not configured", "unconfigured"),
    "never changed": ("never changed", "was never changed", "not changed since"),
    "document gone": ("aged out", "purge", "deleted index", "has aged"),
}
# The pages that must carry it, with the region each one explains a miss in. A page absent here
# is not read; adding a fifth explanation without adding it here is the gap this cannot close.
_PAGES_EXPLAINING_A_MISS: tuple[str, ...] = (
    "src/epics_mcp/server.py",
    "src/epics_mcp/services/alarm_client.py",
    "src/epics_mcp/services/checkers.py",
    "src/epics_mcp/services/coverage.py",
    "src/epics_mcp/operator_guide.md",
    # Added by a post-build review lens: the first list left out the one page the same commit had
    # edited. docs/safety.md is where a reader looks up which negatives are definitive, so it is
    # exactly where a single-cause explanation does the most damage.
    "docs/safety.md",
)
_SHIPPED_ROOT = Path(__file__).resolve().parents[1]


def _pages_that_ship() -> list[Path]:
    """Every page a caller can read: the package sources, the guide, docs/ and the README.

    CHANGELOG.md is deliberately absent. It is a record of what was said when, and correcting a
    past entry would be the drift this guard exists to prevent.
    """
    src = _SHIPPED_ROOT / "src"
    pages = [p for p in src.rglob("*.py") if "__pycache__" not in p.parts]
    pages += sorted(src.rglob("*.md"))
    pages += sorted((_SHIPPED_ROOT / "docs").glob("*.md"))
    pages.append(_SHIPPED_ROOT / "README.md")
    return [p for p in pages if p.is_file()]


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """Split into paragraphs, each as (1-based start line, text with newlines folded to spaces).

    Folding is needed because every occurrence of the claim wraps across lines. Folding the WHOLE
    file instead would let a match run from the end of one paragraph into the start of the next,
    which is a false positive nobody would be able to explain from the reported line.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    for number, line in enumerate(lines, start=1):
        if line.strip():
            if not buf:
                start = number
            buf.append(line.strip())
        elif buf:
            out.append((start, " ".join(buf)))
            buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _import_only_claims() -> list[str]:
    hits: list[str] = []
    for page in _pages_that_ship():
        text = page.read_text(encoding="utf-8")
        for start, paragraph in _paragraphs(text):
            for match in _ONLY_AT_IMPORT.finditer(paragraph):
                where = page.relative_to(_SHIPPED_ROOT).as_posix()
                hits.append(f"{where}: paragraph at line {start}: {match.group(0)}")
    return hits


def test_no_shipped_page_claims_the_import_is_the_only_precondition() -> None:
    """The negative half: the two historical wordings, and near neighbours of them, stay out."""
    hits = _import_only_claims()
    assert not hits, (
        "these places still claim the config-import precondition as the only one:\n  "
        + "\n  ".join(hits)
    )


@pytest.mark.parametrize("page", _PAGES_EXPLAINING_A_MISS)
def test_every_page_explaining_a_miss_names_more_than_one_cause(page: str) -> None:
    """The positive half, and the one a rewording cannot get around.

    Each of these pages tells a caller what a ``false`` is worth. A page that names a single
    cause is how the defect looked before GQ-459, whatever words it used, so this asks for the
    content rather than forbidding a phrase.
    """
    text = (_SHIPPED_ROOT / page).read_text(encoding="utf-8").lower()
    named = sorted(
        cause for cause, phrases in _MISS_CAUSES.items() if any(p in text for p in phrases)
    )
    assert len(named) >= 2, (
        f"{page} explains a miss but names {len(named)} cause(s) ({named or 'none'}); "
        f"at least two of {sorted(_MISS_CAUSES)} must be named, or the page certifies a No it "
        "cannot prove"
    )


def test_the_import_only_pattern_sees_the_wording_it_is_meant_to_catch() -> None:
    """Negative control on the pattern. Without it, one matching nothing would pass forever.

    Both historical wordings, because they differ: one says "at config-import time", the other
    "when the tree was imported". Plus the swapped clause order and one synonym, which the first
    version of this pattern let through, found by a post-build review lens.
    """
    was = "a miss is a real negative only when the Alarm Logger was running at config-import time"
    other = "a MISS is only trustworthy if the Alarm Logger was running when the tree was imported"
    swapped = "a real negative only if the tree was imported while the Alarm Logger was running"
    synonym = "a real negative only if the Alarm Logger was up at config-import time"
    allowed = (
        "A miss can mean the config was never changed since the tree was imported, or that the "
        "logger was not running then, or that the document has aged out."
    )
    # A MEASURED false positive, not an invented one: the first version of the widened pattern
    # fired on this sentence in cli_common.py, which is about the display-tool engine and not
    # about alarms at all. It is kept as a control so the alarm-context conjunct cannot be
    # dropped again without something going red.
    unrelated = (
        "register the display tools only when the engine imports, keep the core server up, and "
        "degrade LOUD when an installed engine fails to import"
    )
    for wording in (was, other, swapped, synonym):
        assert _ONLY_AT_IMPORT.search(wording), wording
    assert not _ONLY_AT_IMPORT.search(allowed), (
        "naming the import as ONE cause among several must stay allowed, or the guard would "
        "forbid the very sentence it asks for"
    )
    assert not _ONLY_AT_IMPORT.search(unrelated), (
        "a sentence about a MODULE import and an unrelated 'up' must not match; the guard is "
        "about the alarm logger, not about three common words"
    )


def test_a_paragraph_boundary_stops_a_match() -> None:
    """The folding control: a whole-file fold made two unrelated paragraphs match as one.

    Measured shape of the false positive, a bullet list where neither line ends in a full stop.
    """
    text = "- Run only the live suite against a running Alarm Logger\n\n- import the tree first\n"
    assert not _import_only_claims_in(text), "a match must not span a blank line"
    same_paragraph = "- a real negative only if the logger was running at config-import time\n"
    assert _import_only_claims_in(same_paragraph), "within one paragraph it must still match"


def _import_only_claims_in(text: str) -> list[str]:
    """The paragraph scan over a literal, for the control above."""
    return [
        match.group(0)
        for _, paragraph in _paragraphs(text)
        for match in _ONLY_AT_IMPORT.finditer(paragraph)
    ]


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


def test_get_alarm_history_surfaces_who_and_what(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSITIVE PIN (decision PI, 2026-08-01): user/host/command/config_msg come back readable,
    WHO acknowledged or disabled an alarm and what was done is exactly what a history reader
    needs. The allowlist stays as STRUCTURE: an unknown future field is still dropped."""
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
            "config_msg": "authored note",
            "some_future_field": "unknown",
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    events, _ = client.get_alarm_history("DEV-TEST01:X", "2026-06-01", "2026-06-02")
    event = events[0]
    assert event["severity"] == "MINOR"
    assert event["enabled"] is True
    assert event["user"] == "jdoe"
    assert event["host"] == "console-host-3"
    assert event["command"] == "Disabled"
    assert event["config_msg"] == "authored note"
    assert "some_future_field" not in event


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


# --- MA-2b(d): the alarm tree is required (no silent 'Accelerator' default that matches nothing)


async def test_is_alarm_configured_tool_requires_config_name() -> None:
    """MA-2b(d): the alarm tree is a REQUIRED tool parameter, no silent 'Accelerator' default that
    matches nothing at a real facility (is_alarm_configured would else always withhold). Mutant
    (a default restored) -> config_name drops out of the schema's 'required' -> this fails."""

    tools = await wire_tools()
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

    tools = await wire_tools()
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


# --- GQ-288: the naive-ISO live probe, driven offline in BOTH directions ---
#
# The live module cannot prove its own red case, and that is a property of the code rather than an
# omission: `normalize_alarm_time` turns a zone-less ISO into the same wire string as the zoned
# one, so a fake server BELOW the normalisation sees one value twice and could never answer
# differently, while a fake client ABOVE it would not exercise the normalisation at all. So the
# regression is reproduced where it lived: with the normalisation taken out of the path the naive
# form reaches the server raw, and the fake server below does what a real Alarm Logger was measured
# doing on 2026-07-15, reading anything its parser cannot take as *now* and answering 200 with an
# empty list rather than an error.

#: Newest-first, as the server answers. Three events one second apart, so the probe's page has a
#: distinct newest and a distinct oldest to anchor its window on.
_ALARM_PAGE: list[dict[str, object]] = [
    {
        "config": f"state:/Accelerator/DEV/A{index}",
        "pv": f"A{index}",
        "message_time": f"2026-08-25T09:44:{2 - index:02d}.000Z",
    }
    for index in range(3)
]


class _ZoneSensitiveLogger:
    """A stand-in Alarm Logger: an ABSOLUTE start without a zone is not read, it becomes *now*.

    A relative amount ("7 days") is read fine, which is what the real parser does and what the
    probe's anchor query depends on. Every QUERY it was asked is kept, not only the start: the
    second half of the [GQ-288] repair is the closed window, and a fake that forgets ``end``
    cannot hold it. A test asserts what went on the WIRE instead of what the caller believed.
    """

    def __init__(self, page: list[dict[str, object]]) -> None:
        self.page = page
        self.calls: list[dict[str, str]] = []

    def __call__(self, url: str, **kwargs: object) -> Mock:
        params = kwargs["params"]
        assert isinstance(params, dict)
        self.calls.append({str(key): str(value) for key, value in params.items()})
        start = str(params["start"])
        zone_less = "T" in start and not (start.endswith("Z") or "+" in start)
        return _resp([] if zone_less else self.page)

    @property
    def starts(self) -> list[str]:
        return [call["start"] for call in self.calls]


def test_naive_iso_probe_is_green_while_the_client_normalises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direction one: with the normalisation in place both spellings reach the server IDENTICALLY.

    That identity is the whole reason the live probe is green today, and it is asserted here
    rather than assumed, because it is what makes the red direction below the only way this probe
    can catch the regression.
    """
    client = AlarmClient("http://alarm")
    logger = _ZoneSensitiveLogger(_ALARM_PAGE)
    monkeypatch.setattr(client.session, "get", logger)

    test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    anchor, zoned, naive = logger.starts
    assert anchor == "7 days", "the anchor query must stay relative, that is what stops it ageing"
    assert zoned == naive, "the client is expected to normalise both spellings to one wire value"
    assert zoned.endswith("Z")


def test_the_compared_window_is_closed_and_carries_its_own_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OTHER half of the [GQ-288] repair, which the two direction tests do not touch.

    Both comparison queries must ask the SAME absolute end. With ``end="now"`` the second query
    asks a wider window than the first, and an event arriving between them lands on one side
    only: red without a defect. And the comparison must ask for its own cap rather than the five
    the other probes use, or the window it just widened would be truncated again.
    """
    client = AlarmClient("http://alarm")
    logger = _ZoneSensitiveLogger(_ALARM_PAGE)
    monkeypatch.setattr(client.session, "get", logger)

    test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    anchor_call, first, second = logger.calls
    assert first["end"] == second["end"], "the two compared queries must span ONE window"
    assert first["end"].endswith("Z"), f"the end must be absolute, got {first['end']!r}"
    # The client over-fetches by one so `capped` can be an honest `fetched > max_events`.
    assert anchor_call["size"] == str(test_alarm_live._ANCHOR_PAGE + 1)
    expected_size = str(test_alarm_live._COMPARISON_MAX_EVENTS + 1)
    assert first["size"] == second["size"] == expected_size


def test_naive_iso_probe_goes_red_when_the_normalisation_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direction two: THE regression, reproduced.

    Without the normalisation the naive form reaches the server raw, the server takes it as *now*,
    and its answer is empty while the zoned side is not. The probe has to notice, and it has to
    notice AT THE COMPARISON: a failure at the reference or cap guard would mean the fixture was
    wrong rather than the code, so both are ruled out by name.
    """
    client = AlarmClient("http://alarm")
    logger = _ZoneSensitiveLogger(_ALARM_PAGE)
    monkeypatch.setattr(client.session, "get", logger)
    monkeypatch.setattr(alarm_client, "normalize_alarm_time", lambda value, *, param: value)

    with pytest.raises(AssertionError) as raised:
        test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    message = str(raised.value)
    assert "positive control not met" not in message, (
        "the reference guard fired, not the comparison"
    )
    assert "the cap truncated" not in message, "the cap guard fired, not the comparison"
    _anchor, zoned, naive = logger.starts
    assert zoned.endswith("Z")
    assert not naive.endswith("Z"), "the naive form was expected to reach the server unrepaired"


def test_instant_of_reads_the_shape_the_logger_answers_with() -> None:
    """Measured 2026-09-04 against a real ESS alarm logger: message_time is zone-explicit ISO."""
    moment = test_alarm_live.instant_of("2026-08-25T09:44:16.935Z")
    assert moment == datetime(2026, 8, 25, 9, 44, 16, 935000, tzinfo=UTC)


def test_instant_of_reads_an_offset_form_as_the_same_moment() -> None:
    """Why the naive spelling is cut from the NORMALISED rendering and never from the raw string:
    an offset form names the same instant, and truncating it would name a different one."""
    offset = test_alarm_live.instant_of("2026-08-25T11:44:16.935+02:00")
    assert offset == test_alarm_live.instant_of("2026-08-25T09:44:16.935Z")


def test_instant_of_refuses_epoch_milliseconds_loudly() -> None:
    """One fixture in this module carries that shape, on a config document rather than on a
    history event instant_of ever sees; the measured logger answers zone-explicit ISO. It is
    refused rather than parsed, so the probe never grows a second way of reading a timestamp."""
    with pytest.raises(TimeWindowFormatError, match="not a time this service can read"):
        test_alarm_live.instant_of(1746093720000)


def test_the_two_spellings_name_one_instant() -> None:
    """The property the whole comparison rests on."""
    moment = test_alarm_live.instant_of("2026-08-25T09:44:16.935Z")
    zoned, naive = test_alarm_live.spellings_of(moment)
    assert (zoned, naive) == ("2026-08-25T09:44:16.935Z", "2026-08-25T09:44:16.935")
    assert test_alarm_live.instant_of(naive) == test_alarm_live.instant_of(zoned)


class _IndexedLogger:
    """A stand-in Alarm Logger that answers by CALL INDEX, not by the start value.

    ``_ZoneSensitiveLogger`` decides on the zone, and that key cannot separate the two compared
    queries from each other: with the normalisation in the path both carry the same zoned ISO
    (its own control test above asserts ``zoned == naive``). The probe's order of queries is a
    property of its code, so this fake pins it: the anchor is call 1, the two compared windows
    are calls 2 and 3. Every requested URL is kept as well, so a test can assert the ADDRESS and
    not only the answer (CLAUDE.md, evidence discipline 8).
    """

    def __init__(self, answers: list[list[dict[str, object]]]) -> None:
        self.answers = answers
        self.urls: list[str] = []
        self.calls: list[dict[str, str]] = []

    def __call__(self, url: str, **kwargs: object) -> Mock:
        params = kwargs["params"]
        assert isinstance(params, dict)
        self.urls.append(url)
        self.calls.append({str(key): str(value) for key, value in params.items()})
        return _resp(self.answers[len(self.calls) - 1])


def test_naive_iso_probe_refuses_an_event_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GQ-395 (S15 row 1): two DIFFERENT lists of events that lack ``message_time`` used to
    compare EQUAL, because the identity read ``str(e.get(...))`` and both sides collapsed to a
    ``'None'`` placeholder. The comparison must refuse such an event by the name of the missing
    field, and it must refuse at the identity, not at the reference or cap guard: those two
    would mean the fixture was wrong rather than the payload.
    """
    client = AlarmClient("http://alarm")
    logger = _IndexedLogger(
        [
            _ALARM_PAGE,  # the anchor page: complete events, so the window derives normally
            [{"config": "state:/Accelerator/DEV/A0"}],  # the zoned window: no message_time
            [{"config": "state:/Accelerator/DEV/A1"}],  # the naive window: a DIFFERENT event
        ]
    )
    monkeypatch.setattr(client.session, "get", logger)

    with pytest.raises(AssertionError, match="message_time") as raised:
        test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    message = str(raised.value)
    assert "positive control not met" not in message, "the reference guard fired, not the identity"
    assert "the cap truncated" not in message, "the cap guard fired, not the identity"
    assert logger.urls == ["http://alarm/search/alarm"] * 3


def _config_event(config: str, message_time: str = "2026-08-25T09:44:01.000Z") -> dict[str, object]:
    """One event as a ``config:`` document can come back from ``/search/alarm``: no ``pv``."""
    return {"config": config, "message_time": message_time}


def test_naive_iso_probe_goes_red_on_equal_times_with_different_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GQ-395 (S15 row 1), the ``config`` half of the identity: one event per window at the SAME
    time but for a DIFFERENT config must be red at the comparison. An identity reduced to the time,
    or one that checks ``config`` without comparing it, would let the same number of wrong events
    pass again, which is the class the row was opened for.
    """
    client = AlarmClient("http://alarm")
    logger = _IndexedLogger(
        [
            _ALARM_PAGE,
            [_config_event("config:/Accelerator/DEV/A0")],
            [_config_event("config:/Accelerator/DEV/A1")],
        ]
    )
    monkeypatch.setattr(client.session, "get", logger)

    with pytest.raises(AssertionError) as raised:
        test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    message = str(raised.value)
    for guard in ("lacks a usable", "positive control not met", "the cap truncated"):
        assert guard not in message, f"the {guard!r} guard fired, not the comparison"
    assert logger.urls == ["http://alarm/search/alarm"] * 3


def test_naive_iso_probe_is_green_on_config_documents_without_pv_in_any_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The green direction of the same identity. ``/search/alarm`` answers config: documents too,
    and those can come without ``pv`` (Phoebus' ``AlarmConfigMessage`` has no such field, read in
    the source), so an identity built on ``pv`` would be red here without a defect. The server's
    order is not part of the claim either: the same two events in the opposite order must compare
    equal.
    """
    client = AlarmClient("http://alarm")
    first = _config_event("config:/Accelerator/DEV/A0", "2026-08-25T09:44:01.000Z")
    second = _config_event("config:/Accelerator/DEV/A1", "2026-08-25T09:44:02.000Z")
    logger = _IndexedLogger([_ALARM_PAGE, [first, second], [second, first]])
    monkeypatch.setattr(client.session, "get", logger)

    test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    assert logger.urls == ["http://alarm/search/alarm"] * 3


def test_naive_iso_probe_refuses_an_empty_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_require_alarm_records`` admits an empty ``config`` string, and two such events at the
    same time would compare equal whatever they stood for. The identity refuses the empty string by
    the field's name, and not at the reference or the cap guard.
    """
    client = AlarmClient("http://alarm")
    empty = _config_event("")
    logger = _IndexedLogger([_ALARM_PAGE, [empty], [empty]])
    monkeypatch.setattr(client.session, "get", logger)

    with pytest.raises(AssertionError, match="usable 'config'") as raised:
        test_alarm_live.test_naive_iso_window_is_honoured(client, "A")

    message = str(raised.value)
    assert "positive control not met" not in message, "the reference guard fired, not the identity"
    assert "the cap truncated" not in message, "the cap guard fired, not the identity"
    assert logger.urls == ["http://alarm/search/alarm"] * 3
