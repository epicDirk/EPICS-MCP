"""Tests for server-level tool wrappers (EpicsError → ToolError conversion)."""

import importlib.util
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from epics_pv_mcp.errors import PVNotFoundError, PVTimeoutError

_Fn = Callable[..., object]


class _ToolSpy:
    """A minimal stand-in for a FastMCP instance that records which functions get registered via
    ``.tool(...)``. Version-independent (no coupling to FastMCP's internal tool registry) — used to
    assert the display-tool registration seam behaviour (S26/N06)."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, **kwargs: object) -> Callable[[_Fn], _Fn]:
        """Mirror FastMCP's ``tool(...)`` decorator factory, recording each registered name."""

        def _decorator(fn: _Fn) -> _Fn:
            self.registered.append(getattr(fn, "__name__", repr(fn)))
            return fn

        return _decorator


def _named(name: str) -> _Fn:
    """A no-op callable carrying *name* as its ``__name__`` (so _ToolSpy records that name)."""

    def _f() -> None: ...

    _f.__name__ = name
    return _f


@pytest.mark.asyncio
async def test_get_pv_value_converts_epics_error_to_tool_error() -> None:
    """Server wrapper must convert EpicsError to ToolError."""
    from epics_pv_mcp.server import get_pv_value

    with (
        patch(
            "epics_pv_mcp.tools.read.pv_get",
            new_callable=AsyncMock,
            side_effect=PVNotFoundError("PV 'MISSING:PV' not found"),
        ),
        pytest.raises(ToolError, match="PV_NOT_FOUND"),
    ):
        await get_pv_value("MISSING:PV")


@pytest.mark.asyncio
async def test_get_pv_value_converts_generic_exception_to_tool_error() -> None:
    """Server wrapper must convert unexpected Exception to ToolError."""
    from epics_pv_mcp.server import get_pv_value

    with (
        patch(
            "epics_pv_mcp.tools.read.pv_get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ),
        # N6: the fallback now preserves the exception class + the original message
        # ("[INTERNAL] <ClassName>: <message>") instead of a bare str(e).
        pytest.raises(ToolError, match=r"\[INTERNAL\] RuntimeError: unexpected"),
    ):
        await get_pv_value("ANY:PV")


@pytest.mark.asyncio
async def test_set_pv_value_converts_write_denied_to_tool_error() -> None:
    """set_pv_value with writes disabled must raise ToolError."""
    from epics_pv_mcp.server import set_pv_value

    with pytest.raises(ToolError, match="PV_WRITE_DENIED"):
        await set_pv_value("TEST:PV", "42")


@pytest.mark.asyncio
async def test_monitor_pv_converts_timeout_to_tool_error() -> None:
    """monitor_pv timeout must raise ToolError."""
    from epics_pv_mcp.server import monitor_pv

    with (
        patch(
            "epics_pv_mcp.tools.monitor.pv_monitor",
            new_callable=AsyncMock,
            side_effect=PVTimeoutError("Timeout monitoring PV 'X'"),
        ),
        pytest.raises(ToolError, match="PV_TIMEOUT"),
    ):
        await monitor_pv("X", duration=1.0)


def test_server_advertises_write_gate_posture() -> None:
    """N1: the server instructions= must advertise the read-only / write-gate posture
    in the initialize handshake — the one protocol-near place a client learns it."""
    from epics_pv_mcp.server import mcp

    assert mcp.instructions and "set_pv_value" in mcp.instructions


@pytest.mark.asyncio
async def test_lookup_device_name_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DS-2 lookup_device_name tool is registered and, with EPICS_MCP_NAMING_URL unset, returns
    a structured enabled:false result and makes no ESS call (no egress by default)."""
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import lookup_device_name
    from epics_pv_mcp.services import checkers

    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url=""))
    result = await lookup_device_name("DEV-TEST01:Ctrl-EVR-01")
    assert result["enabled"] is False
    assert result["registered"] is None


@pytest.mark.asyncio
async def test_get_alarm_history_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DS-3 get_alarm_history tool is registered and, with EPICS_MCP_ALARM_URL unset, returns a
    structured enabled:false result and makes no network call (disabled by default)."""
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import get_alarm_history
    from epics_pv_mcp.services import checkers

    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    result = await get_alarm_history("DEV-TEST01:X", "2026-06-01", "2026-06-02")
    assert result["enabled"] is False
    assert result["events"] == []


def test_main_validates_write_config_before_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """S22: main() validates the write-safety config at BOOT (eager get_safety()) before mcp.run().
    A misconfig (writes enabled + empty allowlist pattern) must fail loudly at start, not silently
    run with every PV writable. Rot-Beweis against the former ``def main(): mcp.run()``."""
    import epics_pv_mcp.config as config_module
    import epics_pv_mcp.safety as safety_module
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.errors import SafetyConfigError
    from epics_pv_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    saved_config = config_module._config
    saved_safety = safety_module._safety
    try:
        config_module._config = EpicsConfig(allow_pv_write=True, pv_write_pattern="")
        safety_module._safety = None
        with pytest.raises(SafetyConfigError):
            main()
        # The boot validation must fire BEFORE mcp.run() — the server never starts serving.
        assert not run_called
    finally:
        config_module._config = saved_config
        safety_module._safety = saved_safety


def test_main_boots_read_only_despite_unusable_audit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S22 QA (F-A): the eager boot validation must fire ONLY when writes are enabled. A read-only
    deployment (writes off = the default) with a set-but-unwritable EPICS_MCP_AUDIT_LOG_FILE must
    still boot — the audit sink is a write-only concern. Regression guard for the eager get_safety()
    over-reach that promoted the audit-path guard to boot time for read-only deployments."""
    import epics_pv_mcp.config as config_module
    import epics_pv_mcp.safety as safety_module
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    # Clear the process-global audit logger so the FileHandler is actually attempted (mirrors
    # test_safety.py::test_invalid_audit_path_raises_safety_config_error).
    audit = logging.getLogger("epics_pv_mcp.audit")
    saved_handlers = audit.handlers[:]
    audit.handlers.clear()
    saved_config = config_module._config
    saved_safety = safety_module._safety
    try:
        config_module._config = EpicsConfig(
            allow_pv_write=False, audit_log_file=str(tmp_path / "nope" / "audit.log")
        )
        safety_module._safety = None
        main()  # must NOT raise — a read-only deploy boots despite the unusable (unused) audit path
        assert run_called == [True]
    finally:
        audit.handlers.clear()
        audit.handlers.extend(saved_handlers)
        config_module._config = saved_config
        safety_module._safety = saved_safety


# --- S26 / N05+N06: core-only + installed-but-broken consistency ------------------------------


def test_load_display_registrar_degrades_loud_on_broken_extra(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard A (S26/N06, degrade-loud): an INSTALLED-but-broken [displays] extra must log at ERROR
    with the correct attribution and return None (→ registrar unavailable, so NO surface over-claims
    the display tools), WITHOUT crashing — the core PV tools stay available. Rot-Beweis vs. the old
    broad ``except ImportError`` that logged INFO "not installed". Install-independent: forces the
    availability signal True and injects a broken import via sys.modules, so opi_navigation need not
    be importable (this test also runs in a core-only install)."""
    import sys

    import epics_pv_mcp.server as server

    monkeypatch.setattr(server, "_display_tools_available", lambda: True)
    # A None entry makes ``from epics_pv_mcp.display_tools import ...`` raise ImportError.
    monkeypatch.setitem(sys.modules, "epics_pv_mcp.display_tools", None)

    with caplog.at_level(logging.ERROR, logger="epics_pv_mcp.server"):
        registrar = server._load_display_registrar()  # must NOT raise

    assert registrar is None  # broken extra → no registrar → no over-claim on any surface
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a broken installed extra must be logged at ERROR"
    assert "display tools failed to load" in errors[0].getMessage()


def test_load_display_registrar_skips_when_extra_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard A positive control 1: a MISSING extra (find_spec None) is the supported core-only
    degrade — return None SILENTLY (no ERROR), symmetric to Guard A's loud degrade on a broken
    extra."""
    import epics_pv_mcp.server as server

    monkeypatch.setattr(server, "_display_tools_available", lambda: False)

    with caplog.at_level(logging.ERROR, logger="epics_pv_mcp.server"):
        assert server._load_display_registrar() is None
    assert not [r for r in caplog.records if r.levelno == logging.ERROR], (
        "an absent extra must degrade silently, without an ERROR log"
    )


def test_load_display_registrar_returns_registrar_when_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard A positive control 2 (non-vacuous happy path, install-independent): with the extra
    present and importable, the loader returns the registrar (→ _DISPLAY_TOOLS_AVAILABLE True) and
    that registrar registers exactly the four display tools. A fake display_tools module is injected
    so this pins the plumbing even in a core-only CI where opi_navigation is absent — excluding the
    mutant that returns None on the importable branch."""
    import sys
    import types

    import epics_pv_mcp.server as server

    def _fake_register(m: object) -> None:
        for name in ("validate_pvs", "crossplane_check", "coverage_audit", "find_device"):
            m.tool()(_named(name))  # type: ignore[attr-defined]

    fake = types.ModuleType("epics_pv_mcp.display_tools")
    fake.register_display_tools = _fake_register  # type: ignore[attr-defined]
    monkeypatch.setattr(server, "_display_tools_available", lambda: True)
    monkeypatch.setitem(sys.modules, "epics_pv_mcp.display_tools", fake)

    registrar = server._load_display_registrar()
    assert registrar is not None
    spy = _ToolSpy()
    registrar(cast(FastMCP[Any], spy))
    assert set(spy.registered) == {
        "validate_pvs",
        "crossplane_check",
        "coverage_audit",
        "find_device",
    }


@pytest.mark.skipif(
    importlib.util.find_spec("opi_navigation") is None,
    reason="[displays] extra (opi_navigation) not installed",
)
def test_load_display_registrar_returns_real_registrar_when_installed() -> None:
    """Guard A positive control 2b: with opi_navigation actually installed, the loader returns the
    REAL register_display_tools (identity), and driving that PRODUCTION registrar registers exactly
    the four display tools — real coverage (not the injected fake of 2a), so a dropped/renamed
    production tool fails here. Runs in the local commit gate; skipped in a core-only CI."""
    import epics_pv_mcp.server as server
    from epics_pv_mcp.display_tools import register_display_tools

    registrar = server._load_display_registrar()
    assert registrar is register_display_tools
    spy = _ToolSpy()
    registrar(cast(FastMCP[Any], spy))
    assert set(spy.registered) == {
        "validate_pvs",
        "crossplane_check",
        "coverage_audit",
        "find_device",
    }


def test_compare_machine_state_wrapper_threads_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard B2 (S26/N05, the fail-open closer): the REGISTERED server prompt wrapper must thread
    the real capability. With display tools unavailable, rendering must not instruct validate_pvs —
    proves server.py actually passes _DISPLAY_TOOLS_AVAILABLE (a forgotten pass-through would leave
    the pure-function Guard B1 green while the live prompt stayed broken)."""
    import epics_pv_mcp.server as server

    monkeypatch.setattr(server, "_DISPLAY_TOOLS_AVAILABLE", False)
    result = server.compare_machine_state("MPS:", reference_file="status.bob")
    assert "validate_pvs" not in result


def test_compare_machine_state_prompt_hides_capability_arg() -> None:
    """Guard B2 (schema): the capability flag must NOT be an LLM-settable prompt argument — the
    wrapper's exposed signature stays (pv_prefix, reference_file), else the LLM could force
    display_tools_available=True and re-instruct the missing tool."""
    import inspect

    import epics_pv_mcp.server as server

    params = inspect.signature(server.compare_machine_state).parameters
    assert "display_tools_available" not in params


def test_build_instructions_omits_display_claims_core_only() -> None:
    """Guard C (S26, Fläche 3): the instructions advertise the display-gated capabilities only when
    the extra is available — a core-only install must not over-claim them. Both branches by direct
    call of the pure builder."""
    from epics_pv_mcp.server import build_instructions

    full = build_instructions(True)
    core = build_instructions(False)
    assert "validate the PVs of a .bob display" in full
    assert "validate the PVs of a .bob display" not in core
    assert "device lookup (screens + live + source IOC)" not in core
    # the core capabilities remain in both
    assert "ChannelFinder lookups" in core
    assert "set_pv_value" in core


def test_mcp_instructions_wired_to_capability_flag() -> None:
    """Guard D (S26/N06, the instructions-wiring closer — symmetric to Guard B2 for the prompt): the
    LIVE server object's instructions must be built FROM the real capability flag, i.e.
    mcp.instructions == build_instructions(_DISPLAY_TOOLS_AVAILABLE). Pins that the initialize-
    handshake advertisement follows the flag; a hardcoded build_instructions(True) would diverge
    from this in a core-only install (flag False) — the asymmetric gap the pure-function Guard C
    alone cannot close."""
    from epics_pv_mcp.server import _DISPLAY_TOOLS_AVAILABLE, build_instructions, mcp

    assert mcp.instructions == build_instructions(_DISPLAY_TOOLS_AVAILABLE)


def test_build_instructions_under_2048_bytes() -> None:
    """MA-1 Commit B: the initialize-handshake instructions must stay a tight header — under 2048
    bytes in BOTH capability branches — so the operator guide (epics-pv://guide) carries the detail
    and MA-2/2b keep head-room for new tool lines. Red at the pre-MA-1 size (3108 / 3003 bytes)."""
    from epics_pv_mcp.server import build_instructions

    assert len(build_instructions(True).encode("utf-8")) < 2048
    assert len(build_instructions(False).encode("utf-8")) < 2048


@pytest.mark.asyncio
async def test_olog_tools_expose_typed_output_schema() -> None:
    """MA-1 Commit C: each Olog tool advertises a STRUCTURED outputSchema — its per-function
    TypedDict's typed ``properties`` — not the bare ``{additionalProperties: true}`` a plain
    ``dict[str, object]`` return yields. Red before the retype (no ``properties`` at all).

    Three independent red directions so the guard is not one-way (CLAUDE.md: a one-way guard
    under-protects): (1) the schema ``title`` is the mapped TypedDict — catches a mis-wired tool,
    e.g. reply_to_log must share OlogCreateResult; (2) the envelope keys are present — completeness
    (mypy --strict guards the inverse, an undeclared key in a return literal); (3) the non-nullable
    scalar/array fields carry the right JSON ``type`` — catches a value-type regression that
    key-presence alone misses. Only ALWAYS-PRESENT fields are type-checked here; the
    sometimes-absent fields are typed ``X | None`` (so FastMCP's null-for-an-omitted-key stays
    schema-valid — see test_olog_structured_output_conforms_to_its_schema) and render as ``anyOf``
    with no top-level ``type``, so they are checked for presence only."""
    from epics_pv_mcp.server import mcp

    # tool: (TypedDict title, {property: expected JSON type}) for the ALWAYS-PRESENT non-nullable
    # fields only. Sometimes-absent fields (capped, note, entry, attachments_uploaded, warnings,
    # withheld, size_bytes, content_base64, output_path) are typed X | None so FastMCP's
    # null-for-an-omitted-key stays schema-valid (see
    # test_olog_structured_output_conforms_to_its_schema); they render as anyOf with no top-level
    # type and are intentionally NOT type-checked here.
    specs: dict[str, tuple[str, dict[str, str]]] = {
        "search_logbook": (
            "OlogSearchResult",
            {"enabled": "boolean", "entries": "array", "total": "integer"},
        ),
        "get_log_entry": ("OlogEntryResult", {"enabled": "boolean", "id": "string"}),
        "list_logbooks": ("OlogLogbooksResult", {"enabled": "boolean", "logbooks": "array"}),
        "list_tags": ("OlogTagsResult", {"enabled": "boolean", "tags": "array"}),
        "list_log_levels": ("OlogLevelsResult", {"enabled": "boolean", "levels": "array"}),
        "create_log_entry": (
            "OlogCreateResult",
            {"enabled": "boolean", "created": "boolean"},
        ),
        "reply_to_log": ("OlogCreateResult", {"enabled": "boolean", "created": "boolean"}),
        "add_log_attachment": (
            "OlogAddAttachmentResult",
            {"enabled": "boolean", "added": "boolean"},
        ),
        "update_log_entry": (
            "OlogUpdateResult",
            {"enabled": "boolean", "updated": "boolean"},
        ),
        "download_log_attachment": (
            "OlogDownloadResult",
            {"enabled": "boolean", "downloaded": "boolean"},
        ),
        "list_log_attachments": (
            "OlogListAttachmentsResult",
            {"enabled": "boolean", "attachments": "array"},
        ),
    }
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name, (title, field_types) in specs.items():
        schema = tools[name].outputSchema or {}
        properties = schema.get("properties", {})
        assert properties, f"{name}: outputSchema carries no typed properties"
        assert schema.get("title") == title, (
            f"{name}: wrong TypedDict wired ({schema.get('title')!r} != {title!r})"
        )
        for field, json_type in field_types.items():
            assert field in properties, f"{name}: outputSchema missing {field!r}"
            actual = properties[field].get("type")
            assert actual == json_type, f"{name}.{field}: schema type {actual!r} != {json_type!r}"


def _schema_permits_null(prop: dict[str, Any]) -> bool:
    """True iff a JSON-schema property accepts a null value.

    A property permits null if it carries no type constraint at all, is itself ``type: null``, or is
    an ``anyOf``/``oneOf`` with a null branch — the shape FastMCP emits for an ``X | None`` field
    (``anyOf[{type: T}, {type: null}]``). A bare ``{"type": "boolean"}`` does NOT permit null.
    """
    if "type" not in prop and "anyOf" not in prop and "oneOf" not in prop:
        return True
    if prop.get("type") == "null":
        return True
    branches = prop.get("anyOf", []) + prop.get("oneOf", [])
    return any(
        isinstance(b, dict) and (b.get("type") == "null" or "type" not in b) for b in branches
    )


# The envelope keys each Olog tool emits on EVERY return path — so FastMCP never serializes them
# as a spurious null. Every OTHER advertised property is sometimes-absent and therefore MUST permit
# null (see test_olog_structured_output_conforms_to_its_schema).
_OLOG_ALWAYS_PRESENT: dict[str, set[str]] = {
    "search_logbook": {"enabled", "entries", "total"},
    "get_log_entry": {"enabled", "id"},
    "list_logbooks": {"enabled", "logbooks"},
    "list_tags": {"enabled", "tags"},
    "list_log_levels": {"enabled", "levels"},
    "create_log_entry": {"enabled", "created"},
    "reply_to_log": {"enabled", "created"},
    "add_log_attachment": {"enabled", "added"},
    "update_log_entry": {"enabled", "updated"},
    "download_log_attachment": {"enabled", "downloaded"},
    "list_log_attachments": {"enabled", "id", "attachments"},
}


@pytest.mark.asyncio
async def test_olog_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA-1 follow-up: every Olog tool's EMITTED structuredContent must conform to its own
    outputSchema. FastMCP serializes an omitted ``total=False`` key as JSON ``null`` (its
    ``convert_result`` dumps the model WITHOUT ``exclude_none``), so a property that is sometimes
    absent MUST permit null in the schema — otherwise the server advertises a schema its own output
    violates. Pre-fix that was 8/11 tools on the disabled path alone and all 11 once ``note`` is
    absent on a success path. Green once the sometimes-absent fields are typed ``X | None``.

    Part A (runtime, real serialization): drive each tool on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits null
    in the schema — the direct bug repro. Part B (static, all 11): every advertised property
    outside the always-present envelope must permit null, extending the check to the
    note/success-only fields of the write + download tools without mocking the write gate.
    """
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import mcp
    from epics_pv_mcp.services import checkers_olog

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_OLOG_URL).
    monkeypatch.setattr(checkers_olog, "get_config", lambda: EpicsConfig(olog_url=""))
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    # Minimal args that reach each tool's disabled return. download_log_attachment validates its
    # identity/sink BEFORE the config gate, so it needs a fully-specified pair.
    disabled_args: dict[str, dict[str, Any]] = {
        "search_logbook": {},
        "get_log_entry": {"log_id": "1"},
        "list_logbooks": {},
        "list_tags": {},
        "list_log_levels": {},
        "create_log_entry": {"title": "t", "logbooks": "Ops"},
        "reply_to_log": {"log_id": "1", "title": "t", "logbooks": "Ops"},
        "add_log_attachment": {"log_id": "1"},
        "update_log_entry": {"log_id": "1"},
        "download_log_attachment": {"attachment_id": "1", "as_base64": True},
        "list_log_attachments": {"log_id": "1"},
    }

    # Part A — the emitted structuredContent conforms on the disabled path. FastMCP's call_tool
    # returns a (content, structuredContent) tuple; we assert on the structured dict.
    for name, args in disabled_args.items():
        _content, structured = cast(tuple[object, dict[str, Any]], await mcp.call_tool(name, args))
        properties = (tools[name].outputSchema or {}).get("properties", {})
        for key, value in structured.items():
            if value is None:
                assert _schema_permits_null(properties[key]), (
                    f"{name}.{key}: emitted null but its outputSchema property forbids null "
                    f"({properties[key]})"
                )

    # Part B — every sometimes-absent advertised property permits null (uniform over all 11 tools).
    for name, always in _OLOG_ALWAYS_PRESENT.items():
        properties = (tools[name].outputSchema or {}).get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name in always:
                continue
            assert _schema_permits_null(prop_schema), (
                f"{name}.{prop_name}: sometimes-absent property must permit null in its "
                f"outputSchema (FastMCP dumps an omitted key as null), got {prop_schema}"
            )


def test_installed_but_broken_extra_keeps_all_surfaces_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard E (S26/N06, the fix's central regression guard): reproduce the INSTALLED-but-broken
    state at import time (find_spec truthy, the display_tools import fails) via importlib.reload,
    then assert the LIVE surfaces stay consistent — ``_DISPLAY_TOOLS_AVAILABLE`` False, instructions
    omit the display clause, the prompt omits validate_pvs.

    The ONLY test that fails if the single-truth derivation ``_DISPLAY_TOOLS_AVAILABLE =
    _display_registrar is not None`` regresses to the Round-1 find_spec formulation
    ``= _display_tools_available()``: the two diverge only in this state, which neither CI lane
    otherwise exercises. Install-independent (forces find_spec truthy + a broken import); restores
    the clean module afterwards so the rest of the suite is unaffected."""
    import importlib
    import sys

    import epics_pv_mcp.server as server

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: (
            object() if name == "opi_navigation" else real_find_spec(name, *a, **k)
        ),
    )
    # A None entry makes ``from epics_pv_mcp.display_tools import ...`` raise ImportError at reload.
    monkeypatch.setitem(sys.modules, "epics_pv_mcp.display_tools", None)
    try:
        importlib.reload(server)

        assert server._DISPLAY_TOOLS_AVAILABLE is False
        assert "validate the PVs of a .bob display" not in (server.mcp.instructions or "")
        prompt = server.compare_machine_state("MPS:", reference_file="status.bob")
        assert "validate_pvs" not in prompt
        assert "status.bob" in prompt  # the file is still referenced, via the core client-read path
    finally:
        monkeypatch.undo()  # restore find_spec + sys.modules BEFORE the clean reload
        importlib.reload(server)  # restore the real module for the rest of the suite
