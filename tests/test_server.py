"""Tests for server-level tool wrappers (EpicsError → ToolError conversion)."""

import ast
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


def test_main_validates_write_config_before_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
        # A durable audit path is set so the NEW boot audit-sink check does not fire first; this
        # test pins the empty-PATTERN refuse specifically (writes on + empty allowlist pattern).
        config_module._config = EpicsConfig(
            allow_pv_write=True, pv_write_pattern="", audit_log_file=str(tmp_path / "audit.log")
        )
        safety_module._safety = None
        with pytest.raises(SafetyConfigError):
            main()
        # The boot validation must fire BEFORE mcp.run() — the server never starts serving.
        assert not run_called
    finally:
        config_module._config = saved_config
        safety_module._safety = saved_safety


def test_main_refuses_write_enabled_without_durable_audit_sink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loopback_write_env: None
) -> None:
    """A write-enabled instance with no EPICS_MCP_AUDIT_LOG_FILE must REFUSE to start: the audit
    would flow to ephemeral stderr and vanish on restart, so a wrong write leaves no durable trace
    (the top-level review's rank-1 write-safety gap). Symmetric with the empty-pattern / reach
    refusals; covers BOTH the PV gate and the Olog write gate (they share the audit sink).

    Rot-Beweis: remove the audit-sink check in main() and the writes-on/no-audit config below boots
    (get_safety falls back to a stderr StreamHandler, no raise). ``loopback_write_env`` pins the E8
    reach so the positive (boot) case builds the safety layer rather than tripping the reach guard.
    """
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
        # PV writes ON, a valid pattern, but NO audit path → the boot check must refuse.
        config_module._config = EpicsConfig(allow_pv_write=True, pv_write_pattern=".*")
        safety_module._safety = None
        with pytest.raises(SafetyConfigError):
            main()
        assert not run_called

        # The Olog write gate alone (PV write OFF) must trip the SAME check — shared sink.
        config_module._config = EpicsConfig(allow_olog_write=True)
        safety_module._safety = None
        with pytest.raises(SafetyConfigError):
            main()
        assert not run_called

        # With a durable audit path set, the write-enabled instance boots.
        config_module._config = EpicsConfig(
            allow_pv_write=True, pv_write_pattern=".*", audit_log_file=str(tmp_path / "audit.log")
        )
        safety_module._safety = None
        main()
        assert run_called == [True]
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
    e.g. reply_to_log must share OlogCreateResult; (2) every advertised property is present and
    mapped — completeness (mypy --strict guards the inverse, an undeclared key in a return literal);
    (3) every field carries the right JSON BASE type, read via :func:`_base_type` from the bare
    ``type`` (a non-nullable field) OR the non-null branch of the ``anyOf`` (an ``X | None`` field).
    Checking the base type of the NULLABLE fields too closes a widening that hides behind
    nullability: ``found: bool | None`` -> ``int | None`` passes mypy (bool ⊆ int) and stays
    an anyOf, so a bare-``type`` check skips it — but it flips the base type boolean -> integer."""
    from epics_pv_mcp.server import mcp

    titles = {
        "search_logbook": "OlogSearchResult",
        "get_log_entry": "OlogEntryResult",
        "list_logbooks": "OlogLogbooksResult",
        "list_tags": "OlogTagsResult",
        "list_log_levels": "OlogLevelsResult",
        "create_log_entry": "OlogCreateResult",
        "reply_to_log": "OlogCreateResult",  # shares OlogCreateResult — must NOT be its own type
        "add_log_attachment": "OlogAddAttachmentResult",
        "update_log_entry": "OlogUpdateResult",
        "download_log_attachment": "OlogDownloadResult",
        "list_log_attachments": "OlogListAttachmentsResult",
    }
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name, title in titles.items():
        schema = tools[name].outputSchema or {}
        properties = schema.get("properties", {})
        assert properties, f"{name}: outputSchema carries no typed properties"
        assert schema.get("title") == title, (
            f"{name}: wrong TypedDict wired ({schema.get('title')!r} != {title!r})"
        )
        for field, prop in properties.items():
            assert field in _OLOG_FIELD_BASE_TYPE, (
                f"{name}.{field}: unmapped Olog field — add its expected base type to "
                "_OLOG_FIELD_BASE_TYPE"
            )
            actual = _base_type(prop)
            assert actual == _OLOG_FIELD_BASE_TYPE[field], (
                f"{name}.{field}: schema base type {actual!r} != {_OLOG_FIELD_BASE_TYPE[field]!r}"
            )


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


def _base_type(prop: dict[str, Any]) -> str | None:
    """The non-null JSON base type of a property — the bare ``type`` of a non-nullable field, or the
    non-null branch of the ``anyOf`` FastMCP emits for an ``X | None`` field. ``None`` if absent."""
    declared = prop.get("type")
    if isinstance(declared, str) and declared != "null":
        return declared
    for branch in prop.get("anyOf", []) + prop.get("oneOf", []):
        if isinstance(branch, dict):
            inner = branch.get("type")
            if isinstance(inner, str) and inner != "null":
                return inner
    return None


# The JSON base type every Olog outputSchema property must advertise (the non-null type for an
# X | None field, the bare type otherwise). An INDEPENDENT source of truth — hardcoded, NOT
# reflected from the TypedDicts — so a base-type WIDENING of an annotation is caught even when
# nullability hides it from a bare-``type`` check (found bool | None -> int | None: mypy-legal since
# bool ⊆ int, anyOf-shaped so a presence check skips it, but boolean -> integer here).
_OLOG_FIELD_BASE_TYPE: dict[str, str] = {
    "enabled": "boolean",
    "entries": "array",
    "total": "integer",
    "total_matches": "integer",
    "capped": "boolean",
    "note": "string",
    "id": "string",
    "found": "boolean",
    "entry": "object",
    "logbooks": "array",
    "tags": "array",
    "levels": "array",
    "default_level": "string",
    "created": "boolean",
    "added": "boolean",
    "updated": "boolean",
    "attachments_uploaded": "array",
    "warnings": "array",
    "downloaded": "boolean",
    "withheld": "boolean",
    "size_bytes": "integer",
    "content_type": "string",
    "filename": "string",
    "content_base64": "string",
    "output_path": "string",
    "attachments": "array",
    "attachment_count": "integer",
}


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


# --- S29: get_pv_history typed output schema (ArchiverHistoryResult) --------------------------

# The JSON base type get_pv_history's outputSchema advertises for each of ArchiverHistoryResult's 11
# fields (the non-null type for an X | None field, the bare type otherwise). Independent source of
# truth — hardcoded, NOT reflected from the TypedDict — so a base-type WIDENING of an annotation is
# caught even where nullability hides it from a bare-``type`` check (the _OLOG_FIELD_BASE_TYPE
# rationale, applied to the archiver result). status is a Literal["ok","empty","withheld"] | None,
# which pydantic renders as anyOf[{enum:[...], type: string}, {type: null}] — the non-null branch
# carries type "string", so a Literal that lost its members (widened to str) still trips this.
_ARCHIVER_HISTORY_BASE_TYPE: dict[str, str] = {
    "enabled": "boolean",
    "pv": "string",
    "samples": "array",
    "total": "integer",
    "from": "string",
    "to": "string",
    "capped": "boolean",
    "meta": "object",
    "status": "string",
    "withheld_reason": "string",
    "note": "string",
}

# The envelope keys get_pv_history emits on EVERY return path (disabled + enabled) — FastMCP never
# serializes them as a spurious null. Every OTHER advertised property is sometimes-absent and MUST
# permit null (see test_archiver_history_structured_output_conforms_to_its_schema).
_ARCHIVER_HISTORY_ALWAYS_PRESENT = frozenset({"enabled", "pv", "samples", "total"})


@pytest.mark.asyncio
async def test_archiver_history_exposes_typed_output_schema() -> None:
    """S29: get_pv_history advertises a STRUCTURED outputSchema — its ArchiverHistoryResult
    TypedDict's typed ``properties`` — not the bare ``{additionalProperties: true}`` a plain
    ``dict[str, object]`` return yields. Red before the retype (no ``properties`` at all).

    Mirrors test_typed_tools_expose_typed_output_schema's directions for the single archiver tool:
    (1) the schema title is ArchiverHistoryResult; (2) the advertised properties are EXACTLY the 11
    mapped fields — completeness both ways (mypy --strict guards an undeclared key in a return
    literal; this guards a dropped one); (3) every field carries the right JSON BASE type, read via
    :func:`_base_type` from the bare ``type`` (non-nullable) OR the non-null ``anyOf`` branch
    (``X | None``). Checking the NULLABLE fields' base type too closes a widening that hides behind
    nullability (e.g. status's Literal collapsing to a bare string, or capped: bool | None widened
    to int | None — mypy-legal since bool ⊆ int, anyOf-shaped so a presence check skips it)."""
    from epics_pv_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["get_pv_history"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "get_pv_history: outputSchema carries no typed properties"
    assert schema.get("title") == "ArchiverHistoryResult", (
        f"get_pv_history: wrong TypedDict wired ({schema.get('title')!r})"
    )
    assert set(properties) == set(_ARCHIVER_HISTORY_BASE_TYPE), (
        f"get_pv_history: advertised properties {sorted(properties)} != "
        f"expected {sorted(_ARCHIVER_HISTORY_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _ARCHIVER_HISTORY_BASE_TYPE[field], (
            f"get_pv_history.{field}: schema base type {actual!r} != "
            f"{_ARCHIVER_HISTORY_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_archiver_history_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance (the MA-1 null trap for get_pv_history): the EMITTED structuredContent
    must conform to its own outputSchema. FastMCP serializes an omitted ``total=False`` key as JSON
    ``null`` (its ``convert_result`` dumps the model WITHOUT ``exclude_none``), so a field absent on
    some return path — from/to/capped/meta/status/withheld_reason on the disabled path, plus note on
    a success path with no note — MUST permit null in the schema. A non-nullable ``status:
    Literal[...]`` would make the disabled-path output violate the tool's own advertised schema.

    Part A (runtime, real serialization): drive get_pv_history on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits null
    — the direct bug repro. Part B (static): every advertised property outside the always-present
    envelope must permit null, extending the check to note (absent only on a success path, which the
    disabled path does not exercise)."""
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import mcp
    from epics_pv_mcp.tools import archiver

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_ARCHIVER_URL).
    # get_pv_history resolves get_config in the archiver module's OWN namespace (a plain
    # ``from ... import get_config``), so patch it THERE — NOT checkers_olog.get_config, which the
    # Olog conformance test patches (a different module for a different tool cluster).
    monkeypatch.setattr(archiver, "get_config", lambda: EpicsConfig(archiver_url=""))
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    properties = (tools["get_pv_history"].outputSchema or {}).get("properties", {})

    # Part A — the emitted structuredContent conforms on the disabled path. FastMCP's call_tool
    # returns a (content, structuredContent) tuple; we assert on the structured dict.
    _content, structured = cast(
        tuple[object, dict[str, Any]],
        await mcp.call_tool(
            "get_pv_history",
            {
                "pv": "SIM:PS-01:Cur-RB",
                "start": "2026-01-01T00:00:00.000Z",
                "end": "2026-01-02T00:00:00.000Z",
            },
        ),
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"get_pv_history.{key}: emitted null but its outputSchema property forbids null "
                f"({properties[key]})"
            )

    # Part B — every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _ARCHIVER_HISTORY_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"get_pv_history.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (FastMCP dumps an omitted key as null), got {prop_schema}"
        )


# The JSON base type every is_alarm_configured (AlarmConfiguredResult) property must advertise (the
# non-null branch for an X | None field, the bare type otherwise). An INDEPENDENT source of truth —
# hardcoded, NOT reflected from the TypedDict — so a base-type WIDENING of an annotation is caught
# even where nullability hides it from a bare-``type`` check (the _ARCHIVER_HISTORY_BASE_TYPE /
# _OLOG_FIELD_BASE_TYPE rationale, applied to the alarm-configured result). configured's non-null
# branch is "boolean" — the client returns bool | None (not str), so a widen-to-str trips this.
_ALARM_CONFIGURED_BASE_TYPE: dict[str, str] = {
    "enabled": "boolean",
    "pv": "string",
    "configured": "boolean",
    "config": "string",
    "withheld": "boolean",
    "detail": "object",
    "note": "string",
}

# The keys is_alarm_configured emits on EVERY return path (disabled + no-tree + _run) and never as a
# spurious null — enabled and pv. Every OTHER advertised property is sometimes-absent (configured is
# present on every path but is None on the disabled/withheld/tree-unknown paths, so it too must
# permit null) and MUST permit null (see the conformance test below).
_ALARM_CONFIGURED_ALWAYS_PRESENT = frozenset({"enabled", "pv"})


@pytest.mark.asyncio
async def test_alarm_configured_exposes_typed_output_schema() -> None:
    """S29 (2nd target): is_alarm_configured advertises a STRUCTURED outputSchema — its
    AlarmConfiguredResult TypedDict's typed ``properties`` — not the bare
    ``{additionalProperties: true}`` a plain ``dict[str, object]`` return yields. Red before the
    retype (no ``properties`` at all).

    Mirrors test_archiver_history_exposes_typed_output_schema for the single alarm tool: (1) the
    schema title is AlarmConfiguredResult; (2) the advertised properties are EXACTLY the 7 mapped
    fields — completeness both ways (mypy --strict guards an undeclared key in a return literal;
    this guards a dropped one); (3) every field carries the right JSON BASE type, read via
    :func:`_base_type` from the bare ``type`` (non-nullable) OR the non-null ``anyOf`` branch
    (``X | None``). Checking the NULLABLE fields' base type too closes a widening that hides behind
    nullability (e.g. configured: bool | None widened to int | None — mypy-legal since bool ⊆ int,
    anyOf-shaped so a presence check skips it, but boolean -> integer here)."""
    from epics_pv_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["is_alarm_configured"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "is_alarm_configured: outputSchema carries no typed properties"
    assert schema.get("title") == "AlarmConfiguredResult", (
        f"is_alarm_configured: wrong TypedDict wired ({schema.get('title')!r})"
    )
    assert set(properties) == set(_ALARM_CONFIGURED_BASE_TYPE), (
        f"is_alarm_configured: advertised properties {sorted(properties)} != "
        f"expected {sorted(_ALARM_CONFIGURED_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _ALARM_CONFIGURED_BASE_TYPE[field], (
            f"is_alarm_configured.{field}: schema base type {actual!r} != "
            f"{_ALARM_CONFIGURED_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_alarm_configured_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance (the MA-1 null trap for is_alarm_configured): the EMITTED structuredContent
    must conform to its own outputSchema. FastMCP serializes an omitted ``total=False`` key as JSON
    ``null`` (its ``convert_result`` dumps the model WITHOUT ``exclude_none``), so a field absent on
    some return path — config/withheld/detail on the disabled path, plus note on a success path with
    no note — MUST permit null in the schema. And ``configured`` is explicitly None on the disabled
    path (the tri-state), so a non-nullable ``configured: bool`` would make the disabled-path output
    violate the tool's own advertised schema.

    Part A (runtime, real serialization): drive is_alarm_configured on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits null
    — the direct bug repro. Part B (static): every advertised property outside the always-present
    envelope must permit null, extending the check to note (absent only on a success path, which the
    disabled path does not exercise)."""
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import mcp
    from epics_pv_mcp.services import checkers

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_ALARM_URL).
    # query_alarm_configured resolves get_config in the checkers module's OWN namespace (a plain
    # ``from ... import get_config``), so patch it THERE — NOT tools.alarm.get_config (the thin
    # adapter has none) and NOT checkers_olog.get_config (a different module for the Olog cluster).
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    properties = (tools["is_alarm_configured"].outputSchema or {}).get("properties", {})

    # Part A — the emitted structuredContent conforms on the disabled path. FastMCP's call_tool
    # returns a (content, structuredContent) tuple; we assert on the structured dict. config_name is
    # required by the tool but the disabled path returns before it is read (checkers.py:369-370).
    _content, structured = cast(
        tuple[object, dict[str, Any]],
        await mcp.call_tool(
            "is_alarm_configured",
            {"pv": "SIM:PS-01:Cur-RB", "config_name": "SomeTree"},
        ),
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"is_alarm_configured.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B — every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _ALARM_CONFIGURED_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"is_alarm_configured.{prop_name}: sometimes-absent property must permit null in its "
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


# --- MA-Q1: tools/list schema hygiene (_prune_tool_schemas post-pass) -------------------------

# The tools whose TypedDict return yields a TYPED outputSchema (properties present); every OTHER
# tool returns dict[str, object] -> its information-empty outputSchema is dropped to None by A2.
# Named here as an INDEPENDENT source of truth (not reflected from the code). The 11 Olog tools were
# typed first (MA-1 Commit C); get_pv_history followed (S29 — its result-level status ok/empty/
# withheld is exactly the kind of discriminant S29 surfaces on the wire). These are core tools, so
# the set is identical on a core-only ([displays] absent, 28 tools) and a full (32) install -> the
# assertions are RELATIONAL, never a COUNT (which would break the core-only lane).
_TYPED_OUTPUT_TOOLS = frozenset(
    {
        "search_logbook",
        "get_log_entry",
        "list_logbooks",
        "list_tags",
        "list_log_levels",
        "create_log_entry",
        "reply_to_log",
        "add_log_attachment",
        "update_log_entry",
        "download_log_attachment",
        "list_log_attachments",
        "get_pv_history",
        "is_alarm_configured",
    }
)

# The four tools that carry a parameter literally NAMED ``title`` (the schema-aware-strip trap).
_TITLE_PARAMETER_TOOLS = ("create_log_entry", "reply_to_log", "update_log_entry", "search_logbook")


def _count_title_keys(node: object) -> int:
    """Naive, traversal-independent count of EVERY ``title`` key in a schema structure — annotation
    OR real property name. Deliberately NOT schema-aware, so it cross-checks the production strip
    without sharing its logic."""
    total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "title":
                total += 1
            total += _count_title_keys(value)
    elif isinstance(node, list):
        for item in node:
            total += _count_title_keys(item)
    return total


# JSON-Schema keyword vocabulary, restated locally so this walker stays independent of production.
_JSCHEMA_MAP_KW = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)
_JSCHEMA_SUB_KW = frozenset(
    {
        "items",
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "contains",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_JSCHEMA_LIST_KW = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})


def _schema_nodes_with_title(node: object, path: str = "root") -> list[str]:
    """Schema-aware walk: a label for every SCHEMA NODE that still carries a ``title`` ANNOTATION,
    walking a properties/$defs map by its VALUES so a property NAMED ``title`` is not mistaken for
    an annotation. An independent re-statement of the spec the production strip enforces."""
    hits: list[str] = []
    if isinstance(node, dict):
        if "title" in node:
            hits.append(path)
        for key, value in node.items():
            if key in _JSCHEMA_MAP_KW:
                if isinstance(value, dict):
                    for name, sub in value.items():
                        hits += _schema_nodes_with_title(sub, f"{path}.{key}[{name}]")
            elif key in _JSCHEMA_SUB_KW:
                hits += _schema_nodes_with_title(value, f"{path}.{key}")
            elif key in _JSCHEMA_LIST_KW and isinstance(value, list):
                for i, sub in enumerate(value):
                    hits += _schema_nodes_with_title(sub, f"{path}.{key}[{i}]")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            hits += _schema_nodes_with_title(item, f"{path}[{i}]")
    return hits


def test_strip_schema_title_annotations_preserves_title_property() -> None:
    """MA-Q1 unit guard (provably red against a NAIVE strip): the schema-aware strip removes every
    ``title`` ANNOTATION but keeps a property literally NAMED ``title`` — with its description — and
    never touches ``required``. A naive 'pop every title key' deletes ``properties['title']`` and
    fails this test; that is the falsification the critical algorithm exists to prevent."""
    from epics_pv_mcp.server import _strip_schema_title_annotations

    schema: dict[str, Any] = {
        "type": "object",
        "title": "SomeArguments",  # a schema-node annotation -> must go
        "required": ["title"],
        "properties": {
            "title": {"type": "string", "title": "Title", "description": "the entry title"},
            "count": {"type": "integer", "title": "Count"},
            "nested": {
                "type": "object",
                "title": "Nested",
                "properties": {"title": {"type": "string", "title": "Title"}},
            },
        },
    }
    _strip_schema_title_annotations(schema)

    assert "title" not in schema  # top-level annotation gone
    assert schema["required"] == ["title"]  # required untouched
    assert "title" in schema["properties"]  # the real title PARAMETER survives
    assert schema["properties"]["title"]["description"] == "the entry title"  # + its description
    assert "title" not in schema["properties"]["title"]  # its annotation gone
    assert "title" not in schema["properties"]["count"]  # sibling annotation gone
    # nested object: its annotation gone, its own ``title`` property (+ that property's annotation)
    nested = schema["properties"]["nested"]
    assert "title" not in nested
    assert "title" in nested["properties"]
    assert "title" not in nested["properties"]["title"]


def test_strip_covers_2020_12_subschema_keywords() -> None:
    """MA-Q1a (a): the strip must descend into the JSON-Schema 2020-12 single-subschema keywords
    ``contains`` / ``unevaluatedItems`` / ``unevaluatedProperties`` too, or a ``title`` annotation
    under them survives on the wire. Asserted STRUCTURALLY (direct dict access), NOT via the
    independent walker ``_schema_nodes_with_title``, which reads the mirrored ``_JSCHEMA_SUB_KW`` —
    so a red-proof reverting BOTH sets would blind the walker and pass falsely; the structural check
    stays valid when only the production ``_SCHEMA_SUBSCHEMA_KEYWORDS`` is reverted. Red before the
    three keywords join the production set (the recursion never descends into them)."""
    from epics_pv_mcp.server import _strip_schema_title_annotations

    schema: dict[str, Any] = {
        "type": "object",
        "contains": {"type": "string", "title": "Contained"},
        "unevaluatedItems": {"type": "number", "title": "UnevalItems"},
        "unevaluatedProperties": {"type": "boolean", "title": "UnevalProps"},
    }
    _strip_schema_title_annotations(schema)

    assert "title" not in schema["contains"]
    assert "title" not in schema["unevaluatedItems"]
    assert "title" not in schema["unevaluatedProperties"]


@pytest.mark.asyncio
async def test_input_schemas_carry_no_title_annotation() -> None:
    """MA-Q1 A1: after the post-pass, NO inputSchema node advertises a ``title`` annotation
    (schema-aware walk). Red on the pre-strip code (pydantic emits titles on every node)."""
    from epics_pv_mcp.server import mcp

    for tool in await mcp.list_tools():
        residual = _schema_nodes_with_title(tool.inputSchema)
        assert not residual, f"{tool.name}: title annotation(s) survived at {residual}"


@pytest.mark.asyncio
async def test_only_real_title_parameters_remain() -> None:
    """MA-Q1 A1 (traversal-independent cross-check): the ONLY ``title`` keys left across all
    inputSchemas are the real ``title`` PARAMETER names — one per tool that declares one at the top
    level. Naive count == count of tools with a top-level ``title`` property. Red pre-strip
    (annotations inflate the naive count)."""
    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    residual = sum(_count_title_keys(t.inputSchema) for t in tools)
    expected = sum(1 for t in tools if "title" in (t.inputSchema.get("properties") or {}))
    assert residual == expected, (
        f"{residual} residual 'title' keys != {expected} real title parameters — "
        "an annotation survived or a real title parameter was deleted"
    )


@pytest.mark.asyncio
async def test_title_parameter_tools_keep_their_title_property() -> None:
    """MA-Q1 critical trap: the four tools with a ``title`` PARAMETER still expose
    ``properties['title']`` WITH its description. A naive 'pop every title key' deletes it. Red on a
    naive strip."""
    from epics_pv_mcp.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    for name in _TITLE_PARAMETER_TOOLS:
        properties = tools[name].inputSchema.get("properties") or {}
        assert "title" in properties, f"{name}: title parameter lost from properties"
        assert properties["title"].get("description"), (
            f"{name}: title parameter kept but its description was stripped"
        )


@pytest.mark.asyncio
async def test_every_required_arg_exists_in_properties() -> None:
    """MA-Q1 critical trap (the falsifiable core): for every tool, each ``required`` entry exists
    in ``properties``. A naive title-strip leaves ``title`` in create_log_entry/reply_to_log's
    ``required`` but deletes it from ``properties`` — a required arg hidden from the wire while
    pydantic still enforces it. Red on a naive strip."""
    from epics_pv_mcp.server import mcp

    for tool in await mcp.list_tools():
        schema = tool.inputSchema
        properties = schema.get("properties") or {}
        for req in schema.get("required", []):
            assert req in properties, (
                f"{tool.name}: required arg {req!r} missing from properties "
                "(schema-breaking title strip?)"
            )


def test_is_information_empty_output_schema_precise() -> None:
    """MA-Q1a (b): A2 drops ONLY the accept-all object form (a ``dict[str, object]`` return yields
    ``{type: object, additionalProperties: true}`` with no ``properties``); a typed schema, an
    array / ``RootModel`` return, and a ``$ref`` are all KEPT. Provably red against the old
    ``not schema.get('properties')`` predicate: mutate the helper body back to it and the array /
    ``$ref`` / bare-object cases (which also lack ``properties``) flip to True."""
    from epics_pv_mcp.server import _is_information_empty_output_schema as empty

    assert empty({"type": "object", "additionalProperties": True}) is True  # accept-all -> drop
    assert empty({"type": "object", "properties": {"x": {}}}) is False  # typed -> keep
    assert empty({"type": "array", "items": {"type": "string"}}) is False  # array return -> keep
    assert empty({"$ref": "#/$defs/Foo"}) is False  # $ref -> keep
    assert empty({"type": "object"}) is False  # not accept-all (no additionalProperties) -> keep


@pytest.mark.asyncio
async def test_output_schema_typed_only_for_typed_tools() -> None:
    """MA-Q1 A2 (RELATIONAL — not a count, so core-only [28] and full [32] both pass): a tool
    advertises an outputSchema WITH properties iff it is one of the typed tools (the 11 Olog tools +
    get_pv_history); every other present tool advertises NONE. Proves the empty schemas were dropped
    AND the typed ones kept. Red on a blanket drop (typed schemas gone) or no drop (empty schemas
    remain). Also the built-in S29 red-proof: put get_pv_history in the set before its TypedDict
    lands and this assertion goes red (its schema is still pruned to None)."""
    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    for tool in tools:
        schema = tool.outputSchema
        if tool.name in _TYPED_OUTPUT_TOOLS:
            assert schema is not None and schema.get("properties"), (
                f"{tool.name}: typed outputSchema was dropped"
            )
        else:
            assert schema is None, (
                f"{tool.name}: expected no outputSchema, got {schema!r} (empty one not dropped)"
            )
    # every named typed tool is actually present (they are core tools, not display-gated)
    present = {t.name for t in tools}
    assert present >= _TYPED_OUTPUT_TOOLS, (
        f"missing typed tools: {sorted(_TYPED_OUTPUT_TOOLS - present)}"
    )


@pytest.mark.asyncio
async def test_field_descriptions_survive_the_strip() -> None:
    """MA-Q1: stripping the ``title`` ANNOTATIONS must not touch field ``description``s — the
    point-of-need semantics the repo DoD requires. Spot-check a distinctive, anchored one."""
    from epics_pv_mcp.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    name_pattern = tools["find_channels"].inputSchema["properties"]["name_pattern"]
    assert "ANCHORED" in name_pattern["description"]


@pytest.mark.asyncio
async def test_stripped_tool_still_returns_structured_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA-Q1 A2 is ADVERTISE-ONLY: dropping the wire outputSchema leaves the RUNTIME
    ``fn_metadata.output_schema`` intact, so a stripped tool still returns structuredContent at call
    time. Drive ``is_archived`` (a stripped tool) on its deterministic disabled path (no network)
    and assert it still yields a structured dict."""
    from epics_pv_mcp.config import EpicsConfig
    from epics_pv_mcp.server import mcp
    from epics_pv_mcp.services import checkers

    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url=""))
    _content, structured = cast(
        tuple[object, dict[str, Any]],
        await mcp.call_tool("is_archived", {"pv": "SIM:PS-01:Cur-RB"}),
    )
    # Reached the disabled path AND produced structuredContent despite the dropped wire schema.
    assert structured.get("enabled") is False


def test_prune_tool_schemas_never_crashes_core(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MA-Q1 trap #2 (crash-guard): the post-pass runs at MODULE IMPORT and mutates a
    cached_property — an unguarded raise while pruning would take down the whole core PV server.
    Force the strip to raise on every tool and assert _prune_tool_schemas swallows it (logs ERROR,
    no propagation). Since MA-Q1a the strip raise is caught PER-TOOL by the inner guard, so full red
    needs BOTH try/excepts removed; each guard is red-proven on its own path elsewhere — the inner
    by test_prune_isolates_a_single_failing_tool, the outer by
    test_prune_outer_guard_swallows_a_broken_manager."""
    import epics_pv_mcp.server as server

    def _boom(_node: object) -> None:
        raise RuntimeError("frozen model")

    monkeypatch.setattr(server, "_strip_schema_title_annotations", _boom)
    with caplog.at_level(logging.ERROR):
        server._prune_tool_schemas(server.mcp)  # must NOT raise
    assert any("_prune_tool_schemas" in record.message for record in caplog.records)


def test_prune_outer_guard_swallows_a_broken_manager(caplog: pytest.LogCaptureFixture) -> None:
    """MA-Q1a: the OUTER guard covers a failure the per-tool inner try/except cannot reach —
    ``manager.list_tools()`` itself raising, before the loop is ever entered. Drive it and assert
    _prune_tool_schemas swallows it (logs the outer 'core PV tools remain available' message, no
    propagation). Red if the OUTER try/except is removed: the RuntimeError propagates out of
    _prune_tool_schemas. The inner guard is red-proven separately by
    test_prune_isolates_a_single_failing_tool."""
    import epics_pv_mcp.server as server

    class _BoomManager:
        def list_tools(self) -> list[object]:
            raise RuntimeError("manager exploded")

    class _FakeMcp:
        _tool_manager = _BoomManager()

    with caplog.at_level(logging.ERROR):
        server._prune_tool_schemas(cast(Any, _FakeMcp()))  # must NOT raise

    assert any("core PV tools remain available" in record.message for record in caplog.records), (
        "outer guard did not log its message — its own failure path is unexercised"
    )


def test_prune_isolates_a_single_failing_tool(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MA-Q1a (f): a raise while pruning ONE tool is isolated — that tool is logged and skipped, the
    OTHERS are still pruned (no half-pruned, inconsistent state). The load-bearing assertion is
    ``good.output_schema is None``: good was reached and pruned DESPITE bad raising first. Red on
    the pre-fix single outer try/except — the first raise (bad) aborts the whole loop, so good is
    never reached and keeps its schema. ``bad`` is enumerated BEFORE ``good`` on purpose: reverse
    the order and the pre-fix code prunes good first, so the red-proof would be lost.

    Driven through a minimal fake manager/tool (not the real ``mcp``): ``server.mcp`` is already
    pruned at import, so a real re-run would be idempotent and could not exhibit the isolation. The
    loop's per-tool robustness is the unit under test; ``_prune_tool_schemas`` reads only
    ``_tool_manager``, ``list_tools()``, and each tool's ``parameters`` / ``output_schema`` /
    ``name`` — so the fake is faithful."""
    import epics_pv_mcp.server as server

    class _FakeTool:
        def __init__(self, name: str) -> None:
            self.name = name
            self.parameters: dict[str, Any] = {"type": "object", "title": name}
            self.output_schema: dict[str, Any] | None = {
                "type": "object",
                "additionalProperties": True,  # accept-all form -> A2 drops it to None
            }

    bad = _FakeTool("bad")
    good = _FakeTool("good")

    class _FakeManager:
        def list_tools(self) -> list[_FakeTool]:
            return [bad, good]  # bad FIRST — the red-proof depends on this order

    class _FakeMcp:
        _tool_manager = _FakeManager()

    real_strip = server._strip_schema_title_annotations

    def _strip_maybe_boom(node: object) -> None:
        if node is bad.parameters:
            raise RuntimeError("boom in one tool")
        real_strip(node)

    monkeypatch.setattr(server, "_strip_schema_title_annotations", _strip_maybe_boom)
    with caplog.at_level(logging.ERROR):
        server._prune_tool_schemas(cast(Any, _FakeMcp()))  # must NOT raise

    assert good.output_schema is None  # LOAD-BEARING: good pruned despite bad raising first
    assert bad.output_schema is not None  # bad untouched (its body raised) — doc only
    assert any("bad" in record.message for record in caplog.records)  # the failing tool was named


# MA-Q3 tools/list size-gate ceiling (chars of the compact ListToolsResult wire payload). Set
# deliberately above the current post-MA-Q1 value (64499) with headroom, and BELOW the un-pruned
# baseline (70237) so a regression that stops the pruning also trips it. Raising it is a conscious,
# reviewed one-line change with a rationale — never a silent bump.
_TOOLS_LIST_WIRE_CEILING = 70_000


@pytest.mark.asyncio
async def test_tools_list_within_budget() -> None:
    """MA-Q3 size-gate: the wire tools/list payload must stay within the agreed ceiling. MA-Q1
    shrank it to 64499 and is framed as 'a precondition for every new tool', but nothing guarded the
    character budget — a new tool, an SDK change that inflates the wire, or a regression that stops
    the pruning could grow it back UNNOTICED with a green suite. This is that missing guard.

    RELATIONAL (a ``<=`` ceiling, not an exact count) so both the core-only lane (28 tools) and the
    full lane (32) pass — a count-pinned assert would break core-only CI. Provably red: lower the
    ceiling below the current 64499. Also catches a broken prune: the un-pruned payload is 70237,
    which exceeds the 70000 ceiling."""
    from mcp.types import ListToolsResult

    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    wire = len(ListToolsResult(tools=tools).model_dump_json(by_alias=True, exclude_none=True))
    assert wire <= _TOOLS_LIST_WIRE_CEILING, (
        f"tools/list wire payload grew to {wire} chars (> {_TOOLS_LIST_WIRE_CEILING} ceiling) — "
        "decide consciously: trim the new tool's schema/description, or raise the ceiling with a "
        "rationale (MA-Q3). Never bump it silently."
    )


# MA-Q2 client-side consent (_meta["anthropic/requiresUserInteraction"]). set_pv_value is the
# only PV-mutating tool (destructiveHint=True) and the only one that actuates physical hardware,
# so it advertises a consent hint that a honouring client turns into a per-call human approval
# prompt. The load-bearing guard stays server-side (safety.py); this is Defense-in-Depth only.
_CONSENT_META = {"anthropic/requiresUserInteraction": True}
# Match on key-PRESENCE, not whole-dict equality: a tool may legitimately gain other anthropic/*
# _meta keys later (maxResultSizeChars, alwaysLoad, ...). Whole-dict "==" would then drop the tool
# from K2's coverage (vacuously green - the dangerous direction) and misfire the invariant.
_CONSENT_KEY = next(iter(_CONSENT_META))
# The Olog-write class (update_log_entry etc.) is a SEPARATE, deliberately deferred governance
# decision (its own gate); it is EXPLICITLY exempted so the consent invariant never silently skips
# it. Goes loud-red if update_log_entry is ever renamed. Self-contained: no plan reference.
_CONSENT_DEFERRED = frozenset({"update_log_entry"})


@pytest.mark.asyncio
async def test_destructive_tools_carry_consent_meta_or_are_explicitly_deferred() -> None:
    """Every destructive tool MUST advertise the consent _meta (or be explicitly deferred).

    A class-invariant keyed on ``destructiveHint``, not a point-check on one named tool: a future
    2nd PV-write tool falls into the net the moment it is marked destructive, so it cannot ship
    with a green suite and no consent hint (the failure a point-test would miss). The Olog-write
    class is deferred via ``_CONSENT_DEFERRED`` (a separate governance decision), never skipped.

    Provably red: drop the ``meta=`` kwarg on set_pv_value -> offenders == ['set_pv_value'].
    """
    from mcp.types import ListToolsResult

    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    offenders = [
        t.name
        for t in tools
        if t.annotations
        and t.annotations.destructiveHint is True
        and t.name not in _CONSENT_DEFERRED
        and (t.meta or {}).get(_CONSENT_KEY) is not True
    ]
    assert not offenders, (
        f"destructive tool(s) missing client-side consent _meta: {offenders} - add "
        "meta={'anthropic/requiresUserInteraction': True} to the @mcp.tool decorator, or "
        "(Olog-write governance) add the tool to _CONSENT_DEFERRED with a rationale."
    )
    # ...and the consent reaches the wire under the _meta alias (what the client receives):
    set_pv = {t.name: t for t in tools}["set_pv_value"]
    wire = ListToolsResult(tools=[set_pv]).model_dump_json(by_alias=True, exclude_none=True)
    assert '"_meta":{"anthropic/requiresUserInteraction":true}' in wire


@pytest.mark.asyncio
async def test_consent_meta_tools_document_the_client_scope() -> None:
    """A tool carrying the consent _meta MUST document the key in its description.

    Honesty drift-guard, encoded as a test (Tier 1) rather than a prose convention (which rots):
    the _meta hint is client- and version-conditional and fails OPEN for a non-honouring client,
    so the point-of-need docstring must name it. Stable marker = the key name itself (it does not
    drift like a version number). Honest limit: it proves a caveat EXISTS, not that it is good.

    Provably red: remove ``requiresUserInteraction`` from set_pv_value's docstring -> goes red.
    """
    from epics_pv_mcp.server import mcp

    undocumented = [
        t.name
        for t in await mcp.list_tools()
        if (t.meta or {}).get(_CONSENT_KEY) is True
        and not (t.description and "requiresUserInteraction" in t.description)
    ]
    assert not undocumented, (
        "consent-_meta tool(s) not documenting the client/version scope in their description: "
        f"{undocumented}"
    )


# ── Annotation consistency / drift guards ──────────────────────────────────────────────────────
# Tool annotations (readOnlyHint/destructiveHint/idempotentHint/openWorldHint) drive REAL client
# behaviour since MA-Q2: K1 keys the consent invariant on destructiveHint, and a honouring client
# (Claude Code) derives permission prompts from them, so a mislabelled or silently drifted field is
# security-relevant. These guards freeze a human-reviewed snapshot plus two structural laws so such
# a change cannot pass unnoticed. Honest limit: they check STRUCTURE + DRIFT, not SEMANTICS (whether
# a hint matches the tool's true behaviour -- that stays a review). Self-contained: no plan ref.

# The four boolean hint fields, in the fixed order used by the golden map below.
_ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# Golden map {tool_name: (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)}, measured
# 2026-07-23 from the ToolAnnotations in server.py + display_tools.py -- a human-reviewed snapshot
# of every tool's client-facing safety labels. Any live annotation change, or a new/renamed tool,
# turns test_tool_annotations_match_golden_map RED until this map is CONSCIOUSLY updated: that edit
# is the review checkpoint. Covers all 32 full-lane tools; the 4 display-extra tools are absent in
# the core-only lane (tolerated -- see _annotation_drift). Regenerate by re-measuring, not by guess.
_ANNOTATION_GOLDEN: dict[str, tuple[bool, bool, bool, bool]] = {
    "add_log_attachment": (False, False, False, True),
    "coverage_audit": (True, False, True, True),
    "create_log_entry": (False, False, False, True),
    "crossplane_check": (True, False, True, True),
    "diagnose_connection": (True, False, True, True),
    "discover_pvs": (True, False, True, True),
    "download_log_attachment": (False, False, False, True),
    "find_channels": (True, False, True, True),
    "find_device": (True, False, True, True),
    "get_alarm_history": (True, False, True, True),
    "get_appliance_info": (True, False, True, True),
    "get_archive_info": (True, False, True, True),
    "get_log_entry": (True, False, True, True),
    "get_pv_history": (True, False, True, True),
    "get_pv_info": (True, False, True, True),
    "get_pv_value": (True, False, True, True),
    "get_pvs": (True, False, True, True),
    "is_alarm_configured": (True, False, True, True),
    "is_archived": (True, False, True, True),
    "list_archived_pvs": (True, False, True, True),
    "list_channel_vocabulary": (True, False, True, True),
    "list_log_attachments": (True, False, True, True),
    "list_log_levels": (True, False, True, True),
    "list_logbooks": (True, False, True, True),
    "list_tags": (True, False, True, True),
    "lookup_device_name": (True, False, True, True),
    "monitor_pv": (True, False, False, True),
    "reply_to_log": (False, False, False, True),
    "search_logbook": (True, False, True, True),
    "set_pv_value": (False, True, False, True),
    "update_log_entry": (False, True, False, True),
    "validate_pvs": (True, False, True, True),
}

# The display-extra tools live in display_tools.py and register only with the [displays] extra, so
# they are ABSENT in core-only CI (28 tools) and PRESENT in the full lane (32). AST-scanned, NEVER
# imported: importing display_tools.py pulls opi_navigation (absent in core-only), which would break
# collection there -- the same reason test_guide_matches_code.py AST-scans it.
_DISPLAY_TOOLS_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "epics_pv_mcp" / "display_tools.py"
)


def _display_tool_names() -> set[str]:
    """Top-level ``async def`` names in display_tools.py (the [displays]-extra tools)."""
    source = _DISPLAY_TOOLS_SRC.read_text(encoding="utf-8")
    return {
        node.name
        for node in ast.iter_child_nodes(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _annotation_drift(
    live_names: set[str], golden_names: set[str], display_extra: set[str]
) -> tuple[set[str], set[str]]:
    """The two-lane set logic of Guard C, as a PURE function so both lanes are unit-testable.

    Returns ``(unclassified, unexpected_missing)`` -- both must be empty for Guard C to pass:
      * ``unclassified`` = live tools absent from the golden map: a new tool must be consciously
        classified.
      * ``unexpected_missing`` = golden tools absent live that are NOT display-extra: a removed or
        renamed core tool. A golden tool missing in the core-only lane is fine ONLY when it is a
        display-extra tool; deriving the tolerance from ``display_extra`` (not a hard set-equality)
        is what makes Guard C pass in BOTH the 28-tool core-only and the 32-tool full lane.

    Load-bearing twice: this backward check also anchors Guard C against an empty ``live_names``.
    On a registration break unexpected_missing becomes all non-display golden tools (non-empty =
    red), so C is never vacuously green. A forward-only ``set(live) - golden`` check would drop that
    anchor; keep both directions.
    """
    unclassified = live_names - golden_names
    unexpected_missing = (golden_names - live_names) - display_extra
    return unclassified, unexpected_missing


def test_annotation_drift_set_logic_is_lane_robust() -> None:
    """Guard C's pure set logic, proven for BOTH lanes without a core-only environment.

    The local gate runs the full lane (32==32), so Guard C's tolerance branch (golden - live) is
    never exercised locally: a mistaken ``set(live) == golden_keys`` would pass here yet break the
    28-tool core-only CI, and would ship under the autonomous "green gates only" push policy. This
    exercises the logic on synthetic 28- and 32-tool live sets so both lanes are red-provable.
    """
    golden = set(_ANNOTATION_GOLDEN)
    display_extra = _display_tool_names()
    assert display_extra <= golden, "every display-extra tool must be classified in the golden map"

    # Full lane (32): every golden tool present → clean.
    assert _annotation_drift(set(golden), golden, display_extra) == (set(), set())

    # Core-only lane (28): the display-extra tools are absent → tolerated, still clean.
    core_only = golden - display_extra
    assert _annotation_drift(core_only, golden, display_extra) == (set(), set())

    # A removed/renamed CORE tool (absent, NOT display-extra) → unexpected_missing ≠ ∅ (red).
    a_core_tool = next(iter(golden - display_extra))
    _, missing = _annotation_drift(core_only - {a_core_tool}, golden, display_extra)
    assert missing == {a_core_tool}

    # A new, unclassified tool → unclassified ≠ ∅ (red).
    unclassified, _ = _annotation_drift(golden | {"brand_new_tool"}, golden, display_extra)
    assert unclassified == {"brand_new_tool"}

    # Empty live (registration break) → unexpected_missing = all non-display golden tools (red), so
    # Guard C is anchored, never vacuously green.
    _, missing_empty = _annotation_drift(set(), golden, display_extra)
    assert missing_empty == golden - display_extra


@pytest.mark.asyncio
async def test_every_tool_carries_complete_annotations() -> None:
    """Every tool MUST carry annotations with all four hint fields explicitly set (not None).

    The four hints drive real client behaviour since MA-Q2, and the MCP client-side defaults are
    surprising (destructiveHint defaults TRUE, readOnlyHint false): a partially-filled
    ToolAnnotations silently mislabels a tool. Catches a new tool shipped without (complete)
    annotations. NOT SDK-default-vacuous: ``ToolAnnotations()`` leaves all four fields None
    (measured), so a real omission surfaces as None here, not as a benign bool default.

    Provably red: drop a hint kwarg (or annotations=) on one tool in server.py -> offender.
    """
    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    assert len(tools) >= 28, "list_tools() returned < core-lane count — tool registration broke"
    offenders = [
        t.name
        for t in tools
        if t.annotations is None
        or any(getattr(t.annotations, hint) is None for hint in _ANNOTATION_HINTS)
    ]
    assert not offenders, (
        f"tool(s) with missing/incomplete annotations: {offenders} — every tool must set all four "
        "hints (readOnlyHint/destructiveHint/idempotentHint/openWorldHint) explicitly."
    )


@pytest.mark.asyncio
async def test_destructive_tools_are_not_marked_read_only() -> None:
    """``destructiveHint is True`` MUST imply ``readOnlyHint is False``, for every tool.

    The MCP spec makes destructiveHint meaningful only when readOnlyHint is false: a tool marked
    both is internally contradictory. A class-invariant keyed on the property (like K1), not a
    point-check.

    Non-vacuity note: this law's antecedent holds for exactly two tools today; were both ever set
    destructive=False it would go vacuous. Its teeth against that live in Guard C's golden map
    (which pins destructive=True for set_pv_value/update_log_entry): B and C are COUPLED, do not
    drop C without revisiting B.

    Provably red: flip set_pv_value's readOnlyHint to True (destructive stays True) -> offender.
    """
    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    assert len(tools) >= 28, "list_tools() returned < core-lane count — tool registration broke"
    offenders = [
        t.name
        for t in tools
        if t.annotations
        and t.annotations.destructiveHint is True
        and t.annotations.readOnlyHint is not False
    ]
    assert not offenders, (
        f"destructive tool(s) also marked read-only (contradictory): {offenders} — a tool with "
        "destructiveHint=True must have readOnlyHint=False."
    )


@pytest.mark.asyncio
async def test_tool_annotations_match_golden_map() -> None:
    """Every tool's four annotation hints MUST match the frozen golden map — the drift guard.

    The load-bearing guard: the ONLY one that catches a silent change of an EXISTING tool's
    annotations -- e.g. flipping set_pv_value to destructive=False/read-only, which Guards A and
    B AND the MA-Q2 consent guards K1/K2 all pass (A: annotations still present; B: vacuous once not
    destructive; K1: drops out of the destructive net, consent _meta untouched; K2: consent _meta
    still documented). A new/renamed tool or any hint change goes red until _ANNOTATION_GOLDEN is
    consciously updated.

    Two-lane robust (core-only CI = 28 tools, full = 32) via _annotation_drift: a golden tool absent
    live is tolerated ONLY if it is display-extra. Honest limit: STRUCTURE + DRIFT, not SEMANTICS.

    Provably red: change a golden tuple, flip a live annotation, or rename a tool.
    """
    from epics_pv_mcp.server import mcp

    tools = await mcp.list_tools()
    assert len(tools) >= 28, "list_tools() returned < core-lane count — tool registration broke"

    golden_names = set(_ANNOTATION_GOLDEN)
    unclassified, unexpected_missing = _annotation_drift(
        {t.name for t in tools}, golden_names, _display_tool_names()
    )
    assert not unclassified, (
        f"tool(s) not in the annotation golden map: {sorted(unclassified)} — classify their safety "
        "hints in _ANNOTATION_GOLDEN (the review checkpoint for a new tool's client-facing labels)."
    )
    assert not unexpected_missing, (
        f"golden tool(s) missing live and not display-extra: {sorted(unexpected_missing)} -- a "
        "removed or renamed tool; update _ANNOTATION_GOLDEN."
    )

    mismatched: dict[str, tuple[object, ...]] = {}
    for t in tools:
        a = t.annotations
        live = (
            a.readOnlyHint if a else None,
            a.destructiveHint if a else None,
            a.idempotentHint if a else None,
            a.openWorldHint if a else None,
        )
        if live != _ANNOTATION_GOLDEN[t.name]:
            mismatched[t.name] = live
    detail = ", ".join(
        f"{name}: {live} vs {_ANNOTATION_GOLDEN[name]}" for name, live in sorted(mismatched.items())
    )
    assert not mismatched, (
        f"tool annotation(s) drifted from the golden map (name: live vs golden): {detail} — update "
        "_ANNOTATION_GOLDEN only after a conscious review of the client-facing hint."
    )
