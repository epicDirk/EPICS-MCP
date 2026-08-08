"""Tests for server-level tool wrappers (EpicsError → ToolError conversion)."""

import ast
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from epics_mcp.errors import PVNotFoundError, PVTimeoutError
from tests.engine_gate import engine_available

_Fn = Callable[..., object]


class _ToolSpy:
    """A minimal stand-in for a FastMCP instance that records which functions get registered via
    ``.tool(...)``. Version-independent (no coupling to FastMCP's internal tool registry), used to
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
    from epics_mcp.server import get_pv_value

    with (
        patch(
            "epics_mcp.tools.read.pv_get",
            new_callable=AsyncMock,
            side_effect=PVNotFoundError("PV 'MISSING:PV' not found"),
        ),
        pytest.raises(ToolError, match="PV_NOT_FOUND"),
    ):
        await get_pv_value("MISSING:PV")


@pytest.mark.asyncio
async def test_get_pv_value_converts_generic_exception_to_tool_error() -> None:
    """Server wrapper must convert unexpected Exception to ToolError."""
    from epics_mcp.server import get_pv_value

    with (
        patch(
            "epics_mcp.tools.read.pv_get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ),
        # Hardening (2026-07-25): the INTERNAL fallback confines the wire message to the exception
        # CLASS name; the raw str(e) ("unexpected") is logged server-side, not sent to the client
        # (full leak-check in test_tool_errors.py's hardening test).
        pytest.raises(ToolError, match=r"^\[INTERNAL\] RuntimeError$"),
    ):
        await get_pv_value("ANY:PV")


@pytest.mark.asyncio
async def test_set_pv_value_converts_write_denied_to_tool_error() -> None:
    """set_pv_value with writes disabled must raise ToolError."""
    from epics_mcp.server import set_pv_value

    with pytest.raises(ToolError, match="PV_WRITE_DENIED"):
        await set_pv_value("TEST:PV", "42")


@pytest.mark.asyncio
async def test_monitor_pv_converts_timeout_to_tool_error() -> None:
    """monitor_pv timeout must raise ToolError."""
    from epics_mcp.server import monitor_pv

    with (
        patch(
            "epics_mcp.tools.monitor.pv_monitor",
            new_callable=AsyncMock,
            side_effect=PVTimeoutError("Timeout monitoring PV 'X'"),
        ),
        pytest.raises(ToolError, match="PV_TIMEOUT"),
    ):
        await monitor_pv("X", duration=1.0)


def test_server_advertises_write_gate_posture() -> None:
    """N1: the server instructions= must advertise the read-only / write-gate posture
    in the initialize handshake, the one protocol-near place a client learns it."""
    from epics_mcp.server import mcp

    assert mcp.instructions and "set_pv_value" in mcp.instructions


@pytest.mark.asyncio
async def test_lookup_device_name_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DS-2 lookup_device_name tool is registered and, with EPICS_MCP_NAMING_URL unset, returns
    a structured enabled:false result and makes no ESS call (no egress by default)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import lookup_device_name
    from epics_mcp.services import checkers

    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url=""))
    result = await lookup_device_name("DEV-TEST01:Ctrl-EVR-01")
    assert result["enabled"] is False
    assert result["registered"] is None


@pytest.mark.asyncio
async def test_get_alarm_history_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DS-3 get_alarm_history tool is registered and, with EPICS_MCP_ALARM_URL unset, returns a
    structured enabled:false result and makes no network call (disabled by default)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import get_alarm_history
    from epics_mcp.services import checkers

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
    import epics_mcp.config as config_module
    import epics_mcp.safety as safety_module
    from epics_mcp.config import EpicsConfig
    from epics_mcp.errors import SafetyConfigError
    from epics_mcp.server import main, mcp

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
            main([])
        # The boot validation must fire BEFORE mcp.run(), the server never starts serving.
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
    import epics_mcp.config as config_module
    import epics_mcp.safety as safety_module
    from epics_mcp.config import EpicsConfig
    from epics_mcp.errors import SafetyConfigError
    from epics_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    saved_config = config_module._config
    saved_safety = safety_module._safety
    try:
        # PV writes ON, a valid pattern, but NO audit path → the boot check must refuse.
        config_module._config = EpicsConfig(allow_pv_write=True, pv_write_pattern=".*")
        safety_module._safety = None
        with pytest.raises(SafetyConfigError):
            main([])
        assert not run_called

        # The Olog write gate alone (PV write OFF) must trip the SAME check, shared sink.
        config_module._config = EpicsConfig(allow_olog_write=True)
        safety_module._safety = None
        with pytest.raises(SafetyConfigError):
            main([])
        assert not run_called

        # With a durable audit path set, the write-enabled instance boots.
        config_module._config = EpicsConfig(
            allow_pv_write=True, pv_write_pattern=".*", audit_log_file=str(tmp_path / "audit.log")
        )
        safety_module._safety = None
        main([])
        assert run_called == [True]
    finally:
        config_module._config = saved_config
        safety_module._safety = saved_safety


def test_main_boots_read_only_despite_unusable_audit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S22 QA (F-A): the eager boot validation must fire ONLY when writes are enabled. A read-only
    deployment (writes off = the default) with a set-but-unwritable EPICS_MCP_AUDIT_LOG_FILE must
    still boot, the audit sink is a write-only concern. Regression guard for the eager get_safety()
    over-reach that promoted the audit-path guard to boot time for read-only deployments."""
    import epics_mcp.config as config_module
    import epics_mcp.safety as safety_module
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    # Clear the process-global audit logger so the FileHandler is actually attempted (mirrors
    # test_safety.py::test_invalid_audit_path_raises_safety_config_error).
    audit = logging.getLogger("epics_mcp.audit")
    saved_handlers = audit.handlers[:]
    audit.handlers.clear()
    saved_config = config_module._config
    saved_safety = safety_module._safety
    try:
        config_module._config = EpicsConfig(
            allow_pv_write=False, audit_log_file=str(tmp_path / "nope" / "audit.log")
        )
        safety_module._safety = None
        # Must NOT raise: a read-only deploy boots despite the unusable (unused) audit path.
        main([])
        assert run_called == [True]
    finally:
        audit.handlers.clear()
        audit.handlers.extend(saved_handlers)
        config_module._config = saved_config
        safety_module._safety = saved_safety


# --- QA-41: the entry point answers --help instead of starting the server ----------------------


def test_main_help_prints_usage_and_does_not_start_the_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``epics-mcp --help`` must print usage and exit cleanly. Before QA-41 ``main()`` took no argv
    at all, so the option reached the MCP transport instead of a parser and the command "succeeded"
    by starting a server that ends on stdin EOF: exit 0, no output, no hint that the flag was never
    understood.

    The asserted usage prefix also pins ``prog``: argparse derives it from the interpreter
    otherwise, and that derivation differs across the versions this package supports.
    """
    from epics_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: epics-mcp")
    assert not run_called  # help is not a way to start the server


def test_main_help_answers_even_when_the_write_config_would_refuse_the_boot(
    monkeypatch: pytest.MonkeyPatch, loopback_write_env: None
) -> None:
    """The parser must sit AHEAD of ``get_config()``. A write-enabled instance without a durable
    audit sink refuses to boot (``SafetyConfigError``), and on such an install asking for help is
    exactly what an operator does next. If the parser sat after the config validation, the one
    command that explains the usage would be the one command they cannot run.

    Rot-Beweis: move the parser below the audit-sink check and this raises SafetyConfigError
    instead of SystemExit.
    """
    import epics_mcp.config as config_module
    import epics_mcp.safety as safety_module
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import main, mcp

    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: None)
    saved_config = config_module._config
    saved_safety = safety_module._safety
    try:
        # PV writes ON with no audit path: main([]) refuses this config at boot (pinned above).
        config_module._config = EpicsConfig(allow_pv_write=True, pv_write_pattern=".*")
        safety_module._safety = None
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
    finally:
        config_module._config = saved_config
        safety_module._safety = saved_safety


def test_main_version_prints_the_installed_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--version`` prints the package version. The bug report template asks a reporter for the
    ``epics-mcp`` version, and until QA-41 no command line answered that question."""
    from epics_mcp import __version__
    from epics_mcp.server import main, mcp

    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_rejects_an_unknown_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised option is a usage error (exit 2), not a silently started server."""
    from epics_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    with pytest.raises(SystemExit) as exc:
        main(["--no-such-option"])

    assert exc.value.code == 2
    assert not run_called


def test_main_with_an_empty_argv_still_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The argv-taking signature must not change the no-argument behaviour: an explicit empty list
    is the normal MCP client start (a client launches the server with no options at all)."""
    from epics_mcp.server import main, mcp

    run_called: list[bool] = []
    monkeypatch.setattr(mcp, "run", lambda *args, **kwargs: run_called.append(True))

    main([])

    assert run_called == [True]


# --- S26 / N05+N06: core-only + installed-but-broken consistency ------------------------------


def test_load_display_registrar_degrades_loud_on_broken_extra(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard A (S26/N06, degrade-loud): an INSTALLED-but-broken [displays] extra must log at ERROR
    with the correct attribution and return None (→ registrar unavailable, so NO surface over-claims
    the display tools), WITHOUT crashing, the core PV tools stay available. Rot-Beweis vs. the old
    broad ``except ImportError`` that logged INFO "not installed". Install-independent: forces the
    availability signal True and injects a broken import via sys.modules, so opi_navigation need not
    be importable (this test also runs in a core-only install)."""
    import sys

    import epics_mcp.server as server

    monkeypatch.setattr(server, "_display_tools_available", lambda: True)
    # A None entry makes ``from epics_mcp.display_tools import ...`` raise ImportError.
    monkeypatch.setitem(sys.modules, "epics_mcp.display_tools", None)

    with caplog.at_level(logging.ERROR, logger="epics_mcp.server"):
        registrar = server._load_display_registrar()  # must NOT raise

    assert registrar is None  # broken extra → no registrar → no over-claim on any surface
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a broken installed extra must be logged at ERROR"
    assert "display tools failed to load" in errors[0].getMessage()


def test_load_display_registrar_skips_when_extra_absent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard A positive control 1: a MISSING extra (find_spec None) is the supported core-only
    degrade, return None SILENTLY (no ERROR), symmetric to Guard A's loud degrade on a broken
    extra."""
    import epics_mcp.server as server

    monkeypatch.setattr(server, "_display_tools_available", lambda: False)

    with caplog.at_level(logging.ERROR, logger="epics_mcp.server"):
        assert server._load_display_registrar() is None
    assert not [r for r in caplog.records if r.levelno == logging.ERROR], (
        "an absent extra must degrade silently, without an ERROR log"
    )


def test_the_server_module_imports_under_a_finder_that_raises() -> None:
    """Guard A positive control 3: the availability probe must ANSWER, never crash.

    ``_display_tools_available`` is reached at MODULE level through ``_load_display_registrar``, and
    ``find_spec`` propagates whatever a meta-path finder raises. So a restricted or broken import
    system used to take ``import epics_mcp.server`` down with a traceback, killing the core PV
    server over an OPTIONAL capability, while the display-aware CLIs degraded cleanly because
    ``cli_common.require_display_engine`` had been given this treatment first (QA-14). The same
    question was being answered two ways in one package.

    Faking ``find_spec`` inside this process proves nothing about a line that already ran at import
    time, so this runs a SUBPROCESS whose import system raises for that package. The blocker is the
    one ``tests/test_cli_without_display_engine.py`` already uses, pointed at the server instead.

    Asserted from the outside, because the claim is that nothing propagates: the import survives, no
    ModuleNotFoundError reaches stderr, and the server settles into the core-only state rather than
    advertising a capability it could not register.

    Red-proof: remove the try/except from ``_display_tools_available`` and this dies with
    ModuleNotFoundError raised out of server.py through ``_load_display_registrar``.
    """
    blocker = (
        "import sys\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'opi_navigation':\n"
        '            raise ModuleNotFoundError(f"No module named {name!r}")\n'
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "import epics_mcp.server as m\n"
        "print(m._DISPLAY_TOOLS_AVAILABLE)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", blocker],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=120,
        check=False,
    )

    assert "ModuleNotFoundError" not in result.stderr, (
        f"the server still dies on its import chain under a raising finder:\n{result.stderr[-800:]}"
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip() == "False", (
        "the server must settle into the core-only state instead of advertising the display group: "
        f"stdout={result.stdout!r}"
    )


def test_load_display_registrar_returns_registrar_when_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard A positive control 2 (non-vacuous happy path, install-independent): with the extra
    present and importable, the loader returns the registrar (→ _DISPLAY_TOOLS_AVAILABLE True) and
    that registrar registers exactly the four display tools. A fake display_tools module is injected
    so this pins the plumbing even in a core-only CI where opi_navigation is absent, excluding the
    mutant that returns None on the importable branch."""
    import sys
    import types

    import epics_mcp.server as server

    def _fake_register(m: object) -> None:
        for name in ("validate_pvs", "crossplane_check", "coverage_audit", "find_device"):
            m.tool()(_named(name))  # type: ignore[attr-defined]

    fake = types.ModuleType("epics_mcp.display_tools")
    fake.register_display_tools = _fake_register  # type: ignore[attr-defined]
    monkeypatch.setattr(server, "_display_tools_available", lambda: True)
    monkeypatch.setitem(sys.modules, "epics_mcp.display_tools", fake)

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


# Evaluated at COLLECTION time, so a bare find_spec here propagated a raising meta-path finder
# into the collector and killed the run rather than skipping this one test (QA-48).
@pytest.mark.skipif(
    not engine_available(),
    reason="[displays] extra (opi_navigation) not installed",
)
def test_load_display_registrar_returns_real_registrar_when_installed() -> None:
    """Guard A positive control 2b: with opi_navigation actually installed, the loader returns the
    REAL register_display_tools (identity), and driving that PRODUCTION registrar registers exactly
    the four display tools, real coverage (not the injected fake of 2a), so a dropped/renamed
    production tool fails here. Runs in the local commit gate; skipped in a core-only CI."""
    import epics_mcp.server as server
    from epics_mcp.display_tools import register_display_tools

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
    the real capability. With display tools unavailable, rendering must not instruct validate_pvs:
    proves server.py actually passes _DISPLAY_TOOLS_AVAILABLE (a forgotten pass-through would leave
    the pure-function Guard B1 green while the live prompt stayed broken)."""
    import epics_mcp.server as server

    monkeypatch.setattr(server, "_DISPLAY_TOOLS_AVAILABLE", False)
    result = server.compare_machine_state("MPS:", reference_file="status.bob")
    assert "validate_pvs" not in result


def test_compare_machine_state_prompt_hides_capability_arg() -> None:
    """Guard B2 (schema): the capability flag must NOT be an LLM-settable prompt argument, the
    wrapper's exposed signature stays (pv_prefix, reference_file), else the LLM could force
    display_tools_available=True and re-instruct the missing tool."""
    import inspect

    import epics_mcp.server as server

    params = inspect.signature(server.compare_machine_state).parameters
    assert "display_tools_available" not in params


def test_build_instructions_omits_display_claims_core_only() -> None:
    """Guard C (S26): the instructions advertise the display-gated capabilities only when
    the extra is available, a core-only install must not over-claim them. Both branches by direct
    call of the pure builder."""
    from epics_mcp.server import build_instructions

    full = build_instructions(True)
    core = build_instructions(False)
    assert "validate the PVs of a .bob display" in full
    assert "validate the PVs of a .bob display" not in core
    assert "device lookup (screens + live + source IOC)" not in core
    # the core capabilities remain in both
    assert "ChannelFinder lookups" in core
    assert "set_pv_value" in core


def test_mcp_instructions_wired_to_capability_flag() -> None:
    """Guard D (S26/N06, the instructions-wiring closer, symmetric to Guard B2 for the prompt): the
    LIVE server object's instructions must be built FROM the real capability flag, i.e.
    mcp.instructions == build_instructions(_DISPLAY_TOOLS_AVAILABLE). Pins that the initialize-
    handshake advertisement follows the flag; a hardcoded build_instructions(True) would diverge
    from this in a core-only install (flag False), the asymmetric gap the pure-function Guard C
    alone cannot close."""
    from epics_mcp.server import _DISPLAY_TOOLS_AVAILABLE, build_instructions, mcp

    assert mcp.instructions == build_instructions(_DISPLAY_TOOLS_AVAILABLE)


def test_build_instructions_under_2048_bytes() -> None:
    """MA-1 Commit B: the initialize-handshake instructions must stay a tight header, under 2048
    bytes in BOTH capability branches, so the operator guide (epics-pv://guide) carries the detail
    and MA-2/2b keep head-room for new tool lines. Red at the pre-MA-1 size (3108 / 3003 bytes)."""
    from epics_mcp.server import build_instructions

    assert len(build_instructions(True).encode("utf-8")) < 2048
    assert len(build_instructions(False).encode("utf-8")) < 2048


@pytest.mark.asyncio
async def test_olog_tools_expose_typed_output_schema() -> None:
    """MA-1 Commit C: each Olog tool advertises a STRUCTURED outputSchema, its per-function
    TypedDict's typed ``properties``, not the bare ``{additionalProperties: true}`` a plain
    ``dict[str, object]`` return yields. Red before the retype (no ``properties`` at all).

    Three independent red directions so the guard is not one-way (CLAUDE.md: a one-way guard
    under-protects): (1) the schema ``title`` is the mapped TypedDict, catches a mis-wired tool,
    e.g. reply_to_log must share OlogCreateResult; (2) every advertised property is present and
    mapped, completeness (mypy --strict guards the inverse, an undeclared key in a return literal);
    (3) every field carries the right JSON BASE type, read via :func:`_base_type` from the bare
    ``type`` (a non-nullable field) OR the non-null branch of the ``anyOf`` (an ``X | None`` field).
    Checking the base type of the NULLABLE fields too closes a widening that hides behind
    nullability: ``found: bool | None`` -> ``int | None`` passes mypy (bool ⊆ int) and stays
    an anyOf, so a bare-``type`` check skips it, but it flips the base type boolean -> integer."""
    from epics_mcp.server import mcp
    from epics_mcp.services.checkers_olog import (
        OlogAddAttachmentResult,
        OlogCreateResult,
        OlogDownloadResult,
        OlogEntryResult,
        OlogLevelsResult,
        OlogListAttachmentsResult,
        OlogLogbooksResult,
        OlogSearchResult,
        OlogTagsResult,
        OlogUpdateResult,
    )

    # Identity anchored on the exact TypedDict FIELD SET: standalone FastMCP omits the root
    # ``title`` that the SDK stack carried as the TypedDict name. Set equality catches a tool
    # wired to the WRONG TypedDict, including reply_to_log, which MUST SHARE OlogCreateResult
    # (same field set) rather than own a type of its own.
    expected_type = {
        "search_logbook": OlogSearchResult,
        "get_log_entry": OlogEntryResult,
        "list_logbooks": OlogLogbooksResult,
        "list_tags": OlogTagsResult,
        "list_log_levels": OlogLevelsResult,
        "create_log_entry": OlogCreateResult,
        "reply_to_log": OlogCreateResult,
        "add_log_attachment": OlogAddAttachmentResult,
        "update_log_entry": OlogUpdateResult,
        "download_log_attachment": OlogDownloadResult,
        "list_log_attachments": OlogListAttachmentsResult,
    }
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    for name, typ in expected_type.items():
        schema = tools[name].outputSchema or {}
        properties = schema.get("properties", {})
        assert properties, f"{name}: outputSchema carries no typed properties"
        assert set(properties) == set(typ.__annotations__), (
            f"{name}: wrong TypedDict wired, properties {sorted(properties)} != "
            f"{typ.__name__} fields {sorted(typ.__annotations__)}"
        )
        for field, prop in properties.items():
            assert field in _OLOG_FIELD_BASE_TYPE, (
                f"{name}.{field}: unmapped Olog field, add its expected base type to "
                "_OLOG_FIELD_BASE_TYPE"
            )
            actual = _base_type(prop)
            assert actual == _OLOG_FIELD_BASE_TYPE[field], (
                f"{name}.{field}: schema base type {actual!r} != {_OLOG_FIELD_BASE_TYPE[field]!r}"
            )


class _ProbeResult(TypedDict):
    """Return shape of the throwaway probe tool below. Deliberately a bare, non-nullable ``bool``:
    the narrowest advertisable constraint, so a violation of it is unambiguous."""

    flag: bool


@pytest.mark.asyncio
async def test_wire_validates_output_schema_while_in_process_does_not() -> None:
    """The MECHANISM every S29 conformance test rests on, pinned, so it cannot retire silently.

    Those tests are only worth anything because output validation actually happens somewhere. It
    does, but NOT where one would guess: it is the MCP SDK's low-level handler that runs jsonschema
    over the emitted structuredContent against the advertised outputSchema, so it fires only for a
    caller that goes over the wire. The in-process ``FastMCP.call_tool`` shortcut hands a return
    back UNVALIDATED. That asymmetry is a behaviour of two moving dependencies, and the jsonschema
    it leans on is not even a declared one here, it arrives transitively. A version bump that
    moves or drops the check would leave every wire conformance test GREEN AND EMPTY. This test is
    the canary; without it, nothing in the suite would notice.

    Both halves matter, but NOT as independent seams, measured, and the earlier wording here was
    wrong about it. fastmcp's in-memory transport routes the wire call THROUGH
    ``FastMCP.call_tool``, so the wire path strictly contains the in-process one: if the in-process
    path ever started validating, half 1 would go red too (with the in-process validator's wording).
    What half 2 actually pins is the shortcut's CURRENT behaviour, which is what makes an
    in-process-only conformance test insufficient, the premise the rest of the suite rests on.

    Type and message are BOTH load-bearing, which is likewise measured rather than assumed: when
    the probe tool fails to register at all, the call still raises ``ToolError``, the type
    assertion passes, and only the ``"validation"`` substring keeps the test from going vacuously
    green. So the substring is not decoration. It is still the more brittle half (an SDK-internal
    string is exactly the drift this guard exists to survive), which is why it carries its own
    failure message saying so rather than being asserted silently.

    Red-proof: return a real ``bool`` from the probe tool and half 1 goes red, which shows the
    ``pytest.raises`` is not passing for some unrelated reason."""
    from fastmcp import Client

    probe: FastMCP[None] = FastMCP("output-schema-mechanism-probe")

    @probe.tool
    async def lying_tool() -> _ProbeResult:
        """Emits a payload that violates its own advertised outputSchema (a str where the schema
        says boolean). The ``cast`` is the point: it is how a real mis-typed return slips past
        mypy, which is why a runtime guard is needed at all."""
        return cast(_ProbeResult, {"flag": "not-a-bool"})

    # Half 1: over the wire the violation is caught and surfaces as an error result, which the
    # client re-raises.
    with pytest.raises(ToolError) as excinfo:
        async with Client(probe) as client:
            await client.call_tool("lying_tool", {})
    assert "validation" in str(excinfo.value).lower(), (
        "the wire path still rejects a schema violation, but no longer says so in the words this "
        f"suite reports to a reader: {excinfo.value!r}"
    )

    # Half 2: in-process the SAME return comes back untouched, wrong type and all.
    structured = (await probe.call_tool("lying_tool", {})).structured_content
    assert structured == {"flag": "not-a-bool"}, (
        "the in-process shortcut no longer hands the return back unvalidated (got "
        f"{structured!r}), if it validates now, the conformance tests' wire/in-process split and "
        "the rationale recorded in services/checkers_olog.py both need revisiting"
    )


def _schema_permits_null(prop: dict[str, Any]) -> bool:
    """True iff a JSON-schema property accepts a null value.

    A property permits null if it carries no type constraint at all, is itself ``type: null``, or is
    an ``anyOf``/``oneOf`` with a null branch, the shape FastMCP emits for an ``X | None`` field
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


def _enum_members(prop: dict[str, Any]) -> frozenset[object] | None:
    """The declared ``enum`` members of a property, or ``None`` if it constrains no membership.

    Reads the bare ``enum`` of a non-nullable field, or the enum-carrying branch of the
    ``anyOf``/``oneOf`` pydantic emits for an ``X | None`` field. Deliberately separate from
    :func:`_base_type`, which sees only the coarse JSON type and therefore cannot tell a
    ``Literal[...]`` from the bare ``str`` it collapses to.

    S31: the members are returned as the RAW JSON values, not stringified. An earlier version
    applied ``str()`` at both extraction sites, which made ``Literal[1, 2]`` and
    ``Literal["1", "2"]`` indistinguishable, a second collapse one level below the one this
    helper exists to undo. Residual collapse, named rather than hidden: ``frozenset`` dedupes by
    ``==``/``hash``, so ``1`` and ``1.0``, and ``True`` and ``1``, stay indistinguishable here.
    That pair is covered elsewhere, :func:`_base_type` separates them into "integer"/"number"/
    "boolean", and the per-tool base-type tables pin that by set equality."""
    declared = prop.get("enum")
    if isinstance(declared, list):
        return frozenset(declared)
    for branch in prop.get("anyOf", []) + prop.get("oneOf", []):
        if isinstance(branch, dict) and isinstance(branch.get("enum"), list):
            return frozenset(branch["enum"])
    return None


def _base_type(prop: dict[str, Any]) -> str | None:
    """The non-null JSON base type of a property, the bare ``type`` of a non-nullable field, or the
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


def _array_items(prop: dict[str, Any]) -> dict[str, object] | None:
    """The declared ``items`` schema of an array property, VERBATIM, or ``None`` if absent.

    The complement of :func:`_base_type` one level down: that one reads "array" from both
    ``list[str]`` and ``list[Any]``, so it cannot see the element condition at all. Returned
    unprojected on purpose, reducing the element schema to its base type would re-introduce the
    exact collapse :func:`_enum_members` was just repaired for, and would read
    ``{"type": "object"}`` and ``{"type": "object", "additionalProperties": true}`` as equal.

    Handles the ``anyOf``/``oneOf`` pydantic emits for an ``X | None`` field with the same idiom
    as its two neighbours, but selects the branch by ``type == "array"`` rather than taking the
    first non-null one, so a hypothetical ``list[str] | str | None`` still resolves to the array
    branch instead of whichever happens to be written first."""
    if prop.get("type") == "array":
        items = prop.get("items")
        return items if isinstance(items, dict) else None
    for branch in prop.get("anyOf", []) + prop.get("oneOf", []):
        if isinstance(branch, dict) and branch.get("type") == "array":
            items = branch.get("items")
            return items if isinstance(items, dict) else None
    return None


# The JSON base type every Olog outputSchema property must advertise (the non-null type for an
# X | None field, the bare type otherwise). An INDEPENDENT source of truth, hardcoded, NOT
# reflected from the TypedDicts, so a base-type WIDENING of an annotation is caught even when
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


# The envelope keys each Olog tool emits on EVERY return path, so FastMCP never serializes them
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
    outputSchema. A sometimes-absent property is typed ``X | None`` by the S29 convention, so it
    MUST permit null in the schema, otherwise the server advertises a shape its own output does
    not honour. The measured rationale (an absent key is DROPPED from the wire; an EXPLICIT None is
    emitted and the wire path validates it; the in-process shortcut does not validate) lives once
    in services/checkers_olog.py's "Tool result shapes" header. Pre-fix this tripped on 8/11 tools
    on the disabled path alone and all 11 once ``note`` is absent on a success path.

    Part A (runtime): drive each tool on the disabled path through the in-process
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits
    null in the schema. Note that this shortcut does NOT validate returns, so Part A only ever sees
    the nulls actually emitted, it is inert for a tool that emits none. Part B (static, all 11)
    is what carries the guarantee: every advertised property outside the always-present envelope
    must permit null, which also covers the note/success-only fields of the write + download tools
    without mocking the write gate.
    """
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers_olog

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_OLOG_URL).
    monkeypatch.setattr(checkers_olog, "get_config", lambda: EpicsConfig(olog_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
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

    # Part A: the emitted structuredContent conforms on the disabled path. Standalone FastMCP's
    # call_tool returns a ToolResult; we assert on its .structured_content dict.
    for name, args in disabled_args.items():
        structured = cast(dict[str, Any], (await mcp.call_tool(name, args)).structured_content)
        properties = (tools[name].outputSchema or {}).get("properties", {})
        for key, value in structured.items():
            if value is None:
                assert _schema_permits_null(properties[key]), (
                    f"{name}.{key}: emitted null but its outputSchema property forbids null "
                    f"({properties[key]})"
                )

    # Part B: every sometimes-absent advertised property permits null (uniform over all 11 tools).
    for name, always in _OLOG_ALWAYS_PRESENT.items():
        properties = (tools[name].outputSchema or {}).get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name in always:
                continue
            assert _schema_permits_null(prop_schema), (
                f"{name}.{prop_name}: sometimes-absent property must permit null in its "
                f"outputSchema (the S29 convention), got {prop_schema}"
            )


# --- S29: get_pv_history typed output schema (ArchiverHistoryResult) --------------------------

# The JSON base type get_pv_history's outputSchema advertises for each of ArchiverHistoryResult's 11
# fields (the non-null type for an X | None field, the bare type otherwise). Independent source of
# truth, hardcoded, NOT reflected from the TypedDict, so a base-type WIDENING of an annotation is
# caught even where nullability hides it from a bare-``type`` check (the _OLOG_FIELD_BASE_TYPE
# rationale, applied to the archiver result).
#
# What this table does NOT catch, measured: it compares the COARSE JSON type only. status is a
# Literal["ok","empty","withheld"] | None, which pydantic renders as anyOf[{enum:[...], type:
# string}, {type: null}], and a bare ``str | None`` renders as anyOf[{type: string}, {type: null}].
# _base_type reads "string" from BOTH, so widening the Literal away leaves this table green. Same
# blindness one level down for arrays: list[str] and list[Any] are both "array". Neither gap is
# this table's job: enum membership is guarded by _OUTPUT_ENUM_MEMBERS, array element schemas by
# _OUTPUT_ARRAY_ITEMS (S31, both relational, both auto-discovering).
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

# The envelope keys get_pv_history emits on EVERY return path (disabled + enabled), FastMCP never
# serializes them as a spurious null. Every OTHER advertised property is sometimes-absent and MUST
# permit null (see test_archiver_history_structured_output_conforms_to_its_schema).
_ARCHIVER_HISTORY_ALWAYS_PRESENT = frozenset({"enabled", "pv", "samples", "total"})


@pytest.mark.asyncio
async def test_archiver_history_exposes_typed_output_schema() -> None:
    """S29: get_pv_history advertises a STRUCTURED outputSchema, its ArchiverHistoryResult
    TypedDict's typed ``properties``, not the bare ``{additionalProperties: true}`` a plain
    ``dict[str, object]`` return yields. Red before the retype (no ``properties`` at all).

    Mirrors test_typed_tools_expose_typed_output_schema's directions for the single archiver tool:
    (1) the schema title is ArchiverHistoryResult; (2) the advertised properties are EXACTLY the 11
    mapped fields, completeness both ways (mypy --strict guards an undeclared key in a return
    literal; this guards a dropped one); (3) every field carries the right JSON BASE type, read via
    :func:`_base_type` from the bare ``type`` (non-nullable) OR the non-null ``anyOf`` branch
    (``X | None``). Checking the NULLABLE fields' base type too closes a widening that hides behind
    nullability, e.g. capped: bool | None widened to int | None, mypy-legal since bool ⊆ int, and
    anyOf-shaped so a presence check skips it.

    NOT covered here, and measured rather than assumed: a widening WITHIN one JSON base type. Both
    ``Literal["ok","empty","withheld"] | None`` and a bare ``str | None`` yield base type "string",
    so dropping the Literal's members leaves this test green. That belongs to
    test_typed_output_schema_enums_declare_their_members, which pins the members themselves."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["get_pv_history"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "get_pv_history: outputSchema carries no typed properties"
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
    must conform to its own outputSchema. A sometimes-absent field is typed ``X | None`` by the S29
    convention (rationale: services/checkers_olog.py's "Tool result shapes" header), so a field
    absent on
    some return path, from/to/capped/meta/status/withheld_reason on the disabled path, plus note on
    a success path with no note, MUST permit null in the schema. A non-nullable ``status:
    Literal[...]`` would make the disabled-path output violate the tool's own advertised schema.

    Part A (runtime, real serialization): drive get_pv_history on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits
    null, the direct bug repro. Part B (static): every advertised property outside the
    always-present envelope must permit null, extending the check to note (absent only on a success
    path, which the
    disabled path does not exercise)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.tools import archiver

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_ARCHIVER_URL).
    # get_pv_history resolves get_config in the archiver module's OWN namespace (a plain
    # ``from ... import get_config``), so patch it THERE, NOT checkers_olog.get_config, which the
    # Olog conformance test patches (a different module for a different tool cluster).
    monkeypatch.setattr(archiver, "get_config", lambda: EpicsConfig(archiver_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["get_pv_history"].outputSchema or {}).get("properties", {})

    # Part A: the emitted structuredContent conforms on the disabled path. Standalone FastMCP's
    # call_tool returns a ToolResult; we assert on its .structured_content dict.
    structured = cast(
        dict[str, Any],
        (
            await mcp.call_tool(
                "get_pv_history",
                {
                    "pv_name": "SIM:PS-01:Cur-RB",
                    "start": "2026-01-01T00:00:00.000Z",
                    "end": "2026-01-02T00:00:00.000Z",
                },
            )
        ).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"get_pv_history.{key}: emitted null but its outputSchema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _ARCHIVER_HISTORY_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"get_pv_history.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (the S29 convention), got {prop_schema}"
        )


# The JSON base type every is_alarm_configured (AlarmConfiguredResult) property must advertise (the
# non-null branch for an X | None field, the bare type otherwise). An INDEPENDENT source of truth:
# hardcoded, NOT reflected from the TypedDict, so a base-type WIDENING of an annotation is caught
# even where nullability hides it from a bare-``type`` check (the _ARCHIVER_HISTORY_BASE_TYPE /
# _OLOG_FIELD_BASE_TYPE rationale, applied to the alarm-configured result). configured's non-null
# branch is "boolean", the client returns bool | None (not str), so a widen-to-str trips this.
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
# spurious null, enabled and pv. Every OTHER advertised property is sometimes-absent (configured is
# present on every path but is None on the disabled/withheld/tree-unknown paths, so it too must
# permit null) and MUST permit null (see the conformance test below).
_ALARM_CONFIGURED_ALWAYS_PRESENT = frozenset({"enabled", "pv"})


@pytest.mark.asyncio
async def test_alarm_configured_exposes_typed_output_schema() -> None:
    """S29 (2nd target): is_alarm_configured advertises a STRUCTURED outputSchema, its
    AlarmConfiguredResult TypedDict's typed ``properties``, not the bare
    ``{additionalProperties: true}`` a plain ``dict[str, object]`` return yields. Red before the
    retype (no ``properties`` at all).

    Mirrors test_archiver_history_exposes_typed_output_schema for the single alarm tool: (1) the
    schema title is AlarmConfiguredResult; (2) the advertised properties are EXACTLY the 7 mapped
    fields, completeness both ways (mypy --strict guards an undeclared key in a return literal;
    this guards a dropped one); (3) every field carries the right JSON BASE type, read via
    :func:`_base_type` from the bare ``type`` (non-nullable) OR the non-null ``anyOf`` branch
    (``X | None``). Checking the NULLABLE fields' base type too closes a widening that hides behind
    nullability (e.g. configured: bool | None widened to int | None, mypy-legal since bool ⊆ int,
    anyOf-shaped so a presence check skips it, but boolean -> integer here)."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["is_alarm_configured"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "is_alarm_configured: outputSchema carries no typed properties"
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
    must conform to its own outputSchema. A sometimes-absent field is typed ``X | None`` by the S29
    convention (rationale: services/checkers_olog.py's "Tool result shapes" header), so a field
    absent on
    some return path, config/withheld/detail on the disabled path, plus note on a success path with
    no note, MUST permit null in the schema. And ``configured`` is explicitly None on the disabled
    path (the tri-state), so a non-nullable ``configured: bool`` would make the disabled-path output
    violate the tool's own advertised schema.

    Part A (runtime, real serialization): drive is_alarm_configured on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits
    null, the direct bug repro. Part B (static): every advertised property outside the
    always-present envelope must permit null, extending the check to note (absent only on a success
    path, which the
    disabled path does not exercise)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_ALARM_URL).
    # query_alarm_configured resolves get_config in the checkers module's OWN namespace (a plain
    # ``from ... import get_config``), so patch it THERE, NOT tools.alarm.get_config (the thin
    # adapter has none) and NOT checkers_olog.get_config (a different module for the Olog cluster).
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["is_alarm_configured"].outputSchema or {}).get("properties", {})

    # Part A: the emitted structuredContent conforms on the disabled path. Standalone FastMCP's
    # call_tool returns a ToolResult; we assert on its .structured_content dict. config_name is
    # required by the tool but the disabled path returns before it is read (checkers.py:369-370).
    structured = cast(
        dict[str, Any],
        (
            await mcp.call_tool(
                "is_alarm_configured",
                {"pv_name": "SIM:PS-01:Cur-RB", "config_name": "SomeTree"},
            )
        ).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"is_alarm_configured.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _ALARM_CONFIGURED_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"is_alarm_configured.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (the S29 convention), got {prop_schema}"
        )


# The JSON base type every lookup_device_name (NameLookupResult) property must advertise (the
# non-null branch for an X | None field, the bare type otherwise). An INDEPENDENT source of truth:
# hardcoded, NOT reflected from the TypedDict, so a base-type WIDENING of an annotation is caught
# even where nullability hides it from a bare-``type`` check (the _ALARM_CONFIGURED_BASE_TYPE
# rationale, applied to the name-lookup result). registered's non-null branch is "boolean":
# NameStatus.registered is bool, so query_naming_lookup yields bool | None (not str), and a
# widen-to-str would trip this.
_NAME_LOOKUP_BASE_TYPE: dict[str, str] = {
    "enabled": "boolean",
    "name": "string",
    "registered": "boolean",
    "status": "string",
    "message": "string",
    "withheld": "boolean",
    "note": "string",
}

# The keys lookup_device_name emits on EVERY return path (disabled + _run success + withheld) and
# never as a spurious null, enabled and name. Every OTHER advertised property is sometimes-absent
# (registered is present on every path but is None on the disabled/withheld paths, so it too must
# permit null) and MUST permit null (see the conformance test below).
_NAME_LOOKUP_ALWAYS_PRESENT = frozenset({"enabled", "name"})


@pytest.mark.asyncio
async def test_name_lookup_exposes_typed_output_schema() -> None:
    """S29 (3rd target): lookup_device_name advertises a STRUCTURED outputSchema, its
    NameLookupResult TypedDict's typed ``properties``, not the bare accept-all schema a plain
    ``dict[str, object]`` return yields. Red before the retype (no properties at all).

    Mirrors test_alarm_configured_exposes_typed_output_schema for the single naming tool: (1) the
    schema title is NameLookupResult; (2) the advertised properties are EXACTLY the 7 mapped
    fields, completeness both ways (mypy --strict guards an undeclared key in a literal; this
    guards a dropped one); (3) every field carries the right JSON BASE type via :func:`_base_type`
    from the bare ``type`` (non-nullable) OR the non-null ``anyOf`` branch (``X | None``). Also
    checking the NULLABLE fields' base type closes a widening hidden behind nullability (e.g.
    registered: bool | None widened to int | None, mypy-legal since bool ⊆ int, anyOf-shaped, so
    a presence check skips it, but boolean -> integer here)."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["lookup_device_name"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "lookup_device_name: outputSchema carries no typed properties"
    assert set(properties) == set(_NAME_LOOKUP_BASE_TYPE), (
        f"lookup_device_name: advertised properties {sorted(properties)} != "
        f"expected {sorted(_NAME_LOOKUP_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _NAME_LOOKUP_BASE_TYPE[field], (
            f"lookup_device_name.{field}: schema base type {actual!r} != "
            f"{_NAME_LOOKUP_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_name_lookup_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance (the MA-1 null trap for lookup_device_name): the EMITTED structuredContent
    must conform to its own outputSchema. A sometimes-absent field is typed ``X | None`` by the S29
    convention (rationale: services/checkers_olog.py's "Tool result shapes" header), so a field
    absent on
    some return path, status/message/withheld on the disabled path, MUST permit null there.
    And ``registered`` is explicitly None on the disabled path (the tri-state), so a non-nullable
    ``registered: bool`` would make the disabled-path output violate the tool's advertised schema.

    Part A (runtime, real serialization): drive lookup_device_name on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits
    null, the direct bug repro. Part B (static): every advertised property outside the
    always-present envelope must permit null, extending the check to note (absent only on the
    success path, which
    the disabled path does not exercise)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_NAMING_URL).
    # Unlike query_alarm_configured, query_naming_lookup does NOT call get_config directly, it
    # delegates to build_naming_client (checkers.py), which resolves get_config in the checkers
    # module's OWN namespace (a plain ``from ... import get_config``) and returns None on an empty
    # naming_url. So patch checkers.get_config HERE, NOT tools.naming.get_config (the thin adapter
    # has none), to drive build_naming_client -> None -> the disabled ``client is None`` return.
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["lookup_device_name"].outputSchema or {}).get("properties", {})

    # Part A: the emitted structuredContent conforms on the disabled path. Standalone FastMCP's
    # call_tool returns a ToolResult; we assert on its .structured_content dict.
    structured = cast(
        dict[str, Any],
        (
            await mcp.call_tool("lookup_device_name", {"name": "DEV-TEST01:Ctrl-EVR-01"})
        ).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"lookup_device_name.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _NAME_LOOKUP_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"lookup_device_name.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (the S29 convention), got {prop_schema}"
        )


# The JSON base type every is_archived (ArchiveStatusResult) property must advertise. Hardcoded as
# an INDEPENDENT source of truth (not reflected from the TypedDict) so a base-type widening is
# caught. The 8 DS-4A/AR-D enrichment fields are typed ``object | None`` (their wire value is copied
# uncoerced from getPVStatus and is genuinely bool OR str across the fixtures), which renders as
# ``anyOf[{}, {type: null}]``, a non-null branch with NO ``type``, so ``_base_type`` returns None
# for them. Mapping them to None here pins that "unconstrained" intent and still red-flags any
# accidental narrowing to a wrong scalar. Only ``archived``/``status``/``enabled``/``pv``/``note``
# are genuine scalars (archived is a computed bool, not str).
_ARCHIVE_STATUS_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "pv": "string",
    "archived": "boolean",
    "status": "string",
    "connection_state": None,
    "last_event": None,
    "is_monitored": None,
    "sampling_period": None,
    "appliance": None,
    "connection_loss_regain_count": None,
    "connection_first_established": None,
    "connection_last_restablished": None,
    "note": "string",
}

# The keys is_archived emits on EVERY return path and never as a spurious null, enabled and pv.
# archived is present on every path but None on the disabled path, so it too must permit null.
_ARCHIVE_STATUS_ALWAYS_PRESENT = frozenset({"enabled", "pv"})


@pytest.mark.asyncio
async def test_is_archived_exposes_typed_output_schema() -> None:
    """S29 (4th target): is_archived advertises a STRUCTURED outputSchema, its ArchiveStatusResult
    TypedDict's typed ``properties``, not the bare accept-all schema a plain ``dict[str, object]``
    return yields. Red before the retype (no properties at all).

    Mirrors test_name_lookup_exposes_typed_output_schema: (1) the schema title is
    ArchiveStatusResult; (2) the advertised properties are EXACTLY the 13 mapped fields:
    completeness both ways (mypy --strict guards an undeclared key in a literal; this guards a
    dropped one); (3) every field carries the expected JSON base type via :func:`_base_type`. The 8
    ``object | None`` enrichment fields must advertise NO base type (``None``), a widening to a
    concrete scalar (e.g. one wrongly typed ``str``) would flip _base_type to ``"string"`` and trip
    the assertion."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["is_archived"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "is_archived: outputSchema carries no typed properties"
    assert set(properties) == set(_ARCHIVE_STATUS_BASE_TYPE), (
        f"is_archived: advertised properties {sorted(properties)} != "
        f"expected {sorted(_ARCHIVE_STATUS_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _ARCHIVE_STATUS_BASE_TYPE[field], (
            f"is_archived.{field}: schema base type {actual!r} != "
            f"{_ARCHIVE_STATUS_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_is_archived_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance (the MA-1 null trap for is_archived): the EMITTED structuredContent must
    conform to its own outputSchema. A sometimes-absent field is typed ``X | None`` by the S29
    convention (rationale: services/checkers_olog.py's "Tool result shapes" header), so a field
    absent on the disabled path, status + the 8 enrichment fields, MUST permit null. And
    ``archived``
    is explicitly None on the disabled path (the tri-state), so a non-nullable ``archived: bool``
    would make the disabled-path output violate the tool's advertised schema.

    Part A (runtime, real serialization): drive is_archived on the disabled path through
    ``mcp.call_tool`` and assert every None-valued key in the emitted structuredContent permits
    null, the direct bug repro. Part B (static): every advertised property outside the
    always-present
    envelope must permit null."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers

    # Force the disabled path deterministically (independent of the ambient EPICS_MCP_ARCHIVER_URL).
    # query_archived resolves get_config in the checkers module's OWN namespace (a plain
    # ``from ... import get_config``), so patch it THERE, NOT tools.archiver.get_config (the
    # get_pv_history/get_archive_info seam, a different module) and NOT the thin _is_archived one.
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["is_archived"].outputSchema or {}).get("properties", {})

    # Part A: the emitted structuredContent conforms on the disabled path.
    structured = cast(
        dict[str, Any],
        (await mcp.call_tool("is_archived", {"pv_name": "SIM:PS-01:Cur-RB"})).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"is_archived.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _ARCHIVE_STATUS_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"is_archived.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (the S29 convention), got {prop_schema}"
        )


# The JSON base type every list_archived_pvs (ArchivedPvsResult) property must advertise. An
# INDEPENDENT source of truth (not reflected from the TypedDict) so a base-type widening is caught.
# No object|None field here, all five are concrete scalars/containers; ``capped``/``note`` are
# ``X | None`` but their non-null branch still carries a type (boolean/string).
_LIST_ARCHIVED_PVS_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "pvs": "array",
    "total": "integer",
    "capped": "boolean",
    "note": "string",
}

# The keys list_archived_pvs emits on EVERY return path and never as a spurious null.
_LIST_ARCHIVED_PVS_ALWAYS_PRESENT = frozenset({"enabled", "pvs", "total"})


@pytest.mark.asyncio
async def test_list_archived_pvs_exposes_typed_output_schema() -> None:
    """S29: list_archived_pvs advertises a STRUCTURED outputSchema, its ArchivedPvsResult
    TypedDict's typed ``properties``, not the accept-all schema a plain ``dict[str, object]`` return
    yields. Red before the retype (no properties). Mirrors the is_archived exposes test:
    (1) properties are non-empty; (2) they are EXACTLY the 5 mapped fields, completeness both ways
    (mypy --strict guards an undeclared key in a literal; this guards a dropped one); (3) each field
    carries the expected JSON base type via :func:`_base_type`."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["list_archived_pvs"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "list_archived_pvs: outputSchema carries no typed properties"
    assert set(properties) == set(_LIST_ARCHIVED_PVS_BASE_TYPE), (
        f"list_archived_pvs: advertised properties {sorted(properties)} != "
        f"expected {sorted(_LIST_ARCHIVED_PVS_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _LIST_ARCHIVED_PVS_BASE_TYPE[field], (
            f"list_archived_pvs.{field}: schema base type {actual!r} != "
            f"{_LIST_ARCHIVED_PVS_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_list_archived_pvs_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must conform to its own outputSchema. Part A
    (runtime): drive list_archived_pvs on the disabled path via ``mcp.call_tool`` and assert every
    None-valued key permits null. Part B (static): every advertised property outside the
    always-present envelope must permit null (the S29 convention for a sometimes-absent field)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.tools import archiver

    # list_archived_pvs is tool-only: it resolves get_config in tools/archiver's OWN namespace, so
    # patch archiver.get_config, NOT checkers.get_config (that seam is for the shared query_*).
    monkeypatch.setattr(archiver, "get_config", lambda: EpicsConfig(archiver_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["list_archived_pvs"].outputSchema or {}).get("properties", {})

    # Part A: the emitted structuredContent conforms on the disabled path.
    structured = cast(
        dict[str, Any],
        (await mcp.call_tool("list_archived_pvs", {})).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"list_archived_pvs.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _LIST_ARCHIVED_PVS_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"list_archived_pvs.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (the S29 convention), got {prop_schema}"
        )


# The JSON base type every get_appliance_info (ApplianceInfoResult) property must advertise. An
# INDEPENDENT source of truth (not reflected from the TypedDict). The 8 object|None topology fields
# render as ``anyOf[{}, {type: null}]`` (non-null branch has no ``type``) so _base_type returns None
# for them; a widening to a concrete scalar would flip that and trip the assertion.
_GET_APPLIANCE_INFO_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "note": "string",
    "identity": None,
    "mgmt_url": None,
    "engine_url": None,
    "retrieval_url": None,
    "etl_url": None,
    "data_retrieval_url": None,
    "cluster_inet_port": None,
    "version": None,
}

# The only key get_appliance_info emits on EVERY return path and never as a spurious null.
_GET_APPLIANCE_INFO_ALWAYS_PRESENT = frozenset({"enabled"})


@pytest.mark.asyncio
async def test_get_appliance_info_exposes_typed_output_schema() -> None:
    """S29: get_appliance_info advertises a STRUCTURED outputSchema (ApplianceInfoResult), not the
    accept-all schema a plain ``dict[str, object]`` return yields. Red before the retype. Checks
    properties non-empty, EXACTLY the 10 mapped fields (completeness both ways), and each field's
    :func:`_base_type`, the 8 object|None topology fields advertise NO base type (None)."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["get_appliance_info"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "get_appliance_info: outputSchema carries no typed properties"
    assert set(properties) == set(_GET_APPLIANCE_INFO_BASE_TYPE), (
        f"get_appliance_info: advertised properties {sorted(properties)} != "
        f"expected {sorted(_GET_APPLIANCE_INFO_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _GET_APPLIANCE_INFO_BASE_TYPE[field], (
            f"get_appliance_info.{field}: schema base type {actual!r} != "
            f"{_GET_APPLIANCE_INFO_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_get_appliance_info_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must conform to its own outputSchema. Part A
    (runtime): drive get_appliance_info on the disabled path through ``mcp.call_tool`` and assert
    every None-valued key permits null. Part B (static): every advertised property outside the
    always-present envelope must permit null (the S29 convention for a sometimes-absent field)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.tools import archiver

    # tool-only: resolves get_config in tools/archiver's OWN namespace, patch archiver.get_config.
    monkeypatch.setattr(archiver, "get_config", lambda: EpicsConfig(archiver_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["get_appliance_info"].outputSchema or {}).get("properties", {})

    # Part A: the emitted structuredContent conforms on the disabled path.
    structured = cast(
        dict[str, Any],
        (await mcp.call_tool("get_appliance_info", {})).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"get_appliance_info.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _GET_APPLIANCE_INFO_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"get_appliance_info.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (the S29 convention), got {prop_schema}"
        )


# The JSON base type every get_archive_info (ArchiveInfoResult) property must advertise. An
# INDEPENDENT source of truth (not reflected from the TypedDict). ``found`` is tri-state bool | None
# (non-null branch boolean). The 26 getPVTypeInfo projection fields are object|None → None here (a
# widening to a concrete scalar, e.g. one wrongly typed str, would flip _base_type and trip this).
_GET_ARCHIVE_INFO_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "pv": "string",
    "found": "boolean",
    "note": "string",
    "dbr_type": None,
    "sampling_method": None,
    "sampling_period": None,
    "event_rate": None,
    "storage_rate": None,
    "bytes_per_event": None,
    "element_count": None,
    "archive_fields": None,
    "data_stores": None,
    "host_name": None,
    "creation_time": None,
    "appliance": None,
    "paused": None,
    "upper_alarm_limit": None,
    "upper_warning_limit": None,
    "lower_warning_limit": None,
    "lower_alarm_limit": None,
    "upper_display_limit": None,
    "lower_display_limit": None,
    "upper_ctrl_limit": None,
    "lower_ctrl_limit": None,
    "precision": None,
    "units": None,
    "controlling_pv": None,
    "policy_name": None,
    "modification_time": None,
}

# The keys get_archive_info emits on EVERY path and never as a spurious null, enabled and pv.
# found is present on every path but None on the disabled path (tri-state), so it too permits null.
_GET_ARCHIVE_INFO_ALWAYS_PRESENT = frozenset({"enabled", "pv"})


@pytest.mark.asyncio
async def test_get_archive_info_exposes_typed_output_schema() -> None:
    """S29: get_archive_info advertises a STRUCTURED outputSchema (ArchiveInfoResult), not the
    accept-all schema a plain ``dict[str, object]`` return yields. Red before the retype. Checks
    properties non-empty, EXACTLY the 30 mapped fields (completeness both ways), and each field's
    :func:`_base_type`, the 26 object|None type-info fields advertise NO base type (None)."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["get_archive_info"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "get_archive_info: outputSchema carries no typed properties"
    assert set(properties) == set(_GET_ARCHIVE_INFO_BASE_TYPE), (
        f"get_archive_info: advertised properties {sorted(properties)} != "
        f"expected {sorted(_GET_ARCHIVE_INFO_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _GET_ARCHIVE_INFO_BASE_TYPE[field], (
            f"get_archive_info.{field}: schema base type {actual!r} != "
            f"{_GET_ARCHIVE_INFO_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_get_archive_info_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must be representable by its own outputSchema.
    The disabled path sets ``found`` EXPLICITLY to None (emitted as null), so a non-nullable
    ``found: bool`` would make the schema misrepresent it. Part A (runtime): drive get_archive_info
    on the disabled path and assert every None-valued key (found!) permits null. Part B (static):
    every advertised property outside the always-present envelope must permit null. (Measured:
    standalone FastMCP does not reject the mismatch itself, so this test is the guard.)"""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.tools import archiver

    # tool-only: resolves get_config in tools/archiver's OWN namespace, patch archiver.get_config.
    monkeypatch.setattr(archiver, "get_config", lambda: EpicsConfig(archiver_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["get_archive_info"].outputSchema or {}).get("properties", {})

    # Part A: emitted structuredContent conforms on the disabled path (found is emitted as null).
    structured = cast(
        dict[str, Any],
        (
            await mcp.call_tool("get_archive_info", {"pv_name": "SIM:PS-01:Cur-RB"})
        ).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"get_archive_info.{key}: emitted null but its schema property forbids null "
                f"({properties[key]})"
            )

    # Part B: every sometimes-absent advertised property permits null.
    for prop_name, prop_schema in properties.items():
        if prop_name in _GET_ARCHIVE_INFO_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"get_archive_info.{prop_name}: sometimes-absent property must permit null in its "
            f"outputSchema (FastMCP emits an explicit None as null), got {prop_schema}"
        )


# The JSON base type every list_channel_vocabulary (ChannelVocabularyResult) property must
# advertise. An INDEPENDENT source of truth (not reflected from the TypedDict). All four are
# concrete scalars/containers; only ``note`` is nullable (its non-null branch is still a string).
_LIST_CHANNEL_VOCABULARY_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "properties": "array",
    "tags": "array",
    "note": "string",
}

# The keys list_channel_vocabulary emits on EVERY return path and never as a spurious null.
_LIST_CHANNEL_VOCABULARY_ALWAYS_PRESENT = frozenset({"enabled", "properties", "tags"})


@pytest.mark.asyncio
async def test_list_channel_vocabulary_exposes_typed_output_schema() -> None:
    """S29: list_channel_vocabulary advertises a STRUCTURED outputSchema (ChannelVocabularyResult),
    not the accept-all schema a plain ``dict[str, object]`` return yields. Red before the retype.
    Checks properties non-empty, EXACTLY the 4 mapped fields (completeness both ways), and each
    field's :func:`_base_type`."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["list_channel_vocabulary"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "list_channel_vocabulary: outputSchema carries no typed properties"
    assert set(properties) == set(_LIST_CHANNEL_VOCABULARY_BASE_TYPE), (
        f"list_channel_vocabulary: advertised properties {sorted(properties)} != "
        f"expected {sorted(_LIST_CHANNEL_VOCABULARY_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _LIST_CHANNEL_VOCABULARY_BASE_TYPE[field], (
            f"list_channel_vocabulary.{field}: schema base type {actual!r} != "
            f"{_LIST_CHANNEL_VOCABULARY_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_list_channel_vocabulary_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must conform to its own outputSchema. Part A
    (runtime): drive list_channel_vocabulary on the disabled path via ``mcp.call_tool`` and assert
    every None-valued key permits null. Part B (static): every advertised property outside the
    always-present envelope must permit null (the S29 convention for a sometimes-absent field)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers

    # shared query_*: resolves get_config in the checkers module's OWN namespace, patch it there.
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(channelfinder_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["list_channel_vocabulary"].outputSchema or {}).get("properties", {})

    structured = cast(
        dict[str, Any],
        (await mcp.call_tool("list_channel_vocabulary", {})).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"list_channel_vocabulary.{key}: emitted null but its schema forbids null "
                f"({properties[key]})"
            )

    for prop_name, prop_schema in properties.items():
        if prop_name in _LIST_CHANNEL_VOCABULARY_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"list_channel_vocabulary.{prop_name}: sometimes-absent property must permit null "
            f"(the S29 convention), got {prop_schema}"
        )


# The JSON base type every get_alarm_history (AlarmHistoryResult) property must advertise. An
# INDEPENDENT source of truth (not reflected from the TypedDict). ``events`` is an array of opaque
# alarm docs; the enabled-only start/end/total/capped and disabled-only note are X | None but their
# non-null branch carries a concrete type.
_GET_ALARM_HISTORY_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "pv": "string",
    "events": "array",
    "start": "string",
    "end": "string",
    "total": "integer",
    "capped": "boolean",
    "note": "string",
}

# The keys get_alarm_history emits on EVERY return path and never as a spurious null.
_GET_ALARM_HISTORY_ALWAYS_PRESENT = frozenset({"enabled", "pv", "events"})


@pytest.mark.asyncio
async def test_get_alarm_history_exposes_typed_output_schema() -> None:
    """S29: get_alarm_history advertises a STRUCTURED outputSchema (AlarmHistoryResult), not the
    accept-all schema a plain ``dict[str, object]`` return yields. Red before the retype. Checks
    properties non-empty, EXACTLY the 8 mapped fields (completeness both ways), and each field's
    :func:`_base_type`."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["get_alarm_history"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "get_alarm_history: outputSchema carries no typed properties"
    assert set(properties) == set(_GET_ALARM_HISTORY_BASE_TYPE), (
        f"get_alarm_history: advertised properties {sorted(properties)} != "
        f"expected {sorted(_GET_ALARM_HISTORY_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _GET_ALARM_HISTORY_BASE_TYPE[field], (
            f"get_alarm_history.{field}: schema base type {actual!r} != "
            f"{_GET_ALARM_HISTORY_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_get_alarm_history_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must conform to its own outputSchema. Part A
    (runtime): drive get_alarm_history on the disabled path via ``mcp.call_tool`` and assert every
    None-valued key permits null. Part B (static): every advertised property outside the
    always-present envelope must permit null (the S29 convention for a sometimes-absent field)."""
    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers

    # shared query_*: resolves get_config in the checkers module's OWN namespace, patch it there.
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["get_alarm_history"].outputSchema or {}).get("properties", {})

    args = {
        "pv_name": "SIM:PS-01:Cur-RB",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
    }
    structured = cast(
        dict[str, Any],
        (await mcp.call_tool("get_alarm_history", args)).structured_content,
    )
    for key, value in structured.items():
        if value is None:
            assert _schema_permits_null(properties[key]), (
                f"get_alarm_history.{key}: emitted null but its schema forbids null "
                f"({properties[key]})"
            )

    for prop_name, prop_schema in properties.items():
        if prop_name in _GET_ALARM_HISTORY_ALWAYS_PRESENT:
            continue
        assert _schema_permits_null(prop_schema), (
            f"get_alarm_history.{prop_name}: sometimes-absent property must permit null "
            f"(the S29 convention), got {prop_schema}"
        )


# The JSON base type every discover_pvs (DiscoverPvsResult) property must advertise. An INDEPENDENT
# source of truth (hand-written, NOT reflected from the TypedDict), so a base-type WIDENING is
# caught even where nullability hides it from a bare-``type`` check. ``pvs`` is an array of
# heterogeneous entry dicts (concrete hit / miss / registry match), hence "array" over opaque items.
_DISCOVER_PVS_BASE_TYPE: dict[str, str | None] = {
    "pattern": "string",
    "pvs": "array",
    "total": "integer",
    "capped": "boolean",
    "source": "string",
    "note": "string",
}

# The keys discover_pvs emits on EVERY one of its four return paths (concrete hit, concrete miss,
# wildcard with ChannelFinder disabled, wildcard enabled) and never as a spurious null.
_DISCOVER_PVS_ALWAYS_PRESENT = frozenset({"pattern", "pvs", "total"})


@pytest.mark.asyncio
async def test_discover_pvs_exposes_typed_output_schema() -> None:
    """S29: discover_pvs advertises a STRUCTURED outputSchema (DiscoverPvsResult), not the
    accept-all schema a plain ``dict[str, object]`` return yields. Red before the retype, and red
    while the tool still carries the explicit ``@mcp.tool(output_schema=None)`` opt-out, which
    OVERRIDES the annotation-derived schema (that kwarg, not any post-pass, is what keeps the
    untyped tools schema-less). Checks properties non-empty, EXACTLY the 6 mapped fields
    (completeness both ways), and each field's :func:`_base_type`."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["discover_pvs"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "discover_pvs: outputSchema carries no typed properties"
    assert set(properties) == set(_DISCOVER_PVS_BASE_TYPE), (
        f"discover_pvs: advertised properties {sorted(properties)} != "
        f"expected {sorted(_DISCOVER_PVS_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _DISCOVER_PVS_BASE_TYPE[field], (
            f"discover_pvs.{field}: schema base type {actual!r} != "
            f"{_DISCOVER_PVS_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_discover_pvs_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must conform to its own outputSchema.

    Part A (runtime) drives ALL FOUR return paths through a REAL ``fastmcp.Client`` rather than the
    in-process ``FastMCP.call_tool`` the sibling conformance tests use. Both halves of that sentence
    are load-bearing and were measured:

    * The CLIENT matters. In-process, a return is handed back UNVALIDATED, so a payload violating
      its own advertised schema sails through; over the wire the MCP SDK's low-level handler
      validates the structuredContent against ``outputSchema`` and turns a mismatch into an
      ``Output validation error``. Going through the client asserts what a real caller experiences,
      using the SDK's own check rather than re-implementing one against jsonschema (an undeclared,
      merely transitive dependency here).
    * ALL FOUR paths matter. Only the wildcard-enabled path emits ``capped``/``source`` and only the
      concrete-hit path emits an entry carrying ``value``, so a one-path test cannot see a
      mis-typed field on the other three. Measured on a faithful mutant (``capped`` raw-copied from
      the ChannelFinder payload instead of ``bool()``-coerced, the exact hazard documented for the
      archiver's uncoerced fields): a single-path version of this test stayed GREEN while a real
      client got ``Output validation error``. Hence the fan-out below.

    The explicit null check is a belt on top: no path of this tool emits an explicit ``None`` today,
    so it is inert here, it exists to catch a future field that does.

    Part B (static) enforces the nullability convention in BOTH directions: a sometimes-absent
    property must permit null, and an always-present one must NOT (otherwise the advertised shape
    invites a caller to expect a null that never comes, and a later widening to ``X | None`` would
    pass unnoticed). It gets a non-empty floor: with an empty ``properties`` both loops would pass
    vacuously, which is precisely the state the ``output_schema=None`` opt-out produces."""
    from fastmcp import Client

    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp
    from epics_mcp.services import checkers
    from epics_mcp.tools import discover as discover_mod

    # discover_pvs -> _discover_by_channelfinder -> query_channels, which resolves get_config in
    # the checkers module's OWN namespace, patch it there (NOT tools.discover).
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(channelfinder_url=""))
    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["discover_pvs"].outputSchema or {}).get("properties", {})

    async def _fake_pv_get(pv: str, timeout: float | None = None) -> dict[str, object]:
        return {"value": 1.25}

    async def _raise_not_found(pv: str, timeout: float | None = None) -> dict[str, object]:
        raise PVNotFoundError(f"no such PV: {pv}")

    async def _fake_query_channels(name_pattern: str, **kwargs: object) -> dict[str, object]:
        """An ENABLED ChannelFinder payload, the only path that emits capped/source.

        ``capped`` is deliberately a truthy STRING, not a bool. The seam into this function IS typed
        (``ChannelQueryResult``, S29), and that is exactly why this double matters: an annotation
        is not a runtime guarantee when the seam itself is monkeypatched away. That pins the
        ``bool()`` coercion in ``_discover_by_channelfinder``: raw-copying the value instead
        would emit a string into a ``boolean | null`` field and the client call below would go red.
        """
        return {
            "enabled": True,
            "channels": [
                {"name": "SIM:PS-01:Cur-RB", "ioc_name": "ioc-01", "host_name": "host-01"}
            ],
            "total": 1,
            "capped": "true",
        }

    # (label, patch) per return path. The wildcard-disabled path needs no patch beyond get_config.
    paths: list[tuple[str, str, Callable[[], None]]] = [
        ("wildcard-disabled", "SIM:PS-*", lambda: None),
        (
            "wildcard-enabled",
            "SIM:PS-*",
            lambda: monkeypatch.setattr(discover_mod, "query_channels", _fake_query_channels),
        ),
        (
            "concrete-hit",
            "SIM:PS-01:Cur-RB",
            lambda: monkeypatch.setattr(discover_mod, "pv_get", _fake_pv_get),
        ),
        (
            "concrete-miss",
            "SIM:PS-01:Cur-RB",
            lambda: monkeypatch.setattr(discover_mod, "pv_get", _raise_not_found),
        ),
    ]
    seen: set[str] = set()
    for label, pattern, apply_patch in paths:
        apply_patch()
        async with Client(mcp) as client:
            # Raises ToolError("Output validation error: ...") if the payload does not conform.
            result = await client.call_tool("discover_pvs", {"pattern": pattern})
        structured = cast(dict[str, Any], result.structured_content)
        seen |= set(structured)
        for key, value in structured.items():
            if value is None:
                assert _schema_permits_null(properties[key]), (
                    f"discover_pvs[{label}].{key}: emitted null but its schema forbids null "
                    f"({properties[key]})"
                )
        for always in _DISCOVER_PVS_ALWAYS_PRESENT:
            assert always in structured, (
                f"discover_pvs[{label}]: {always!r} is declared always-present (non-nullable) but "
                f"is absent from the emitted payload {sorted(structured)}"
            )
    # The four paths together must exercise every advertised field, otherwise a field could be
    # mis-typed in a corner this test never reaches, which is exactly how the mutant above survived.
    assert seen == set(properties), (
        f"discover_pvs: the four driven paths emitted {sorted(seen)}, "
        f"which does not cover the advertised {sorted(properties)}"
    )

    assert properties, "discover_pvs: outputSchema carries no typed properties"
    for prop_name, prop_schema in properties.items():
        if prop_name in _DISCOVER_PVS_ALWAYS_PRESENT:
            assert not _schema_permits_null(prop_schema), (
                f"discover_pvs.{prop_name}: declared always-present, so it must NOT permit null "
                f"in its outputSchema, got {prop_schema}"
            )
            continue
        assert _schema_permits_null(prop_schema), (
            f"discover_pvs.{prop_name}: sometimes-absent property must permit null "
            f"(the S29 convention), got {prop_schema}"
        )


# The JSON base type every find_channels (ChannelQueryResult) property must advertise. An
# INDEPENDENT source of truth (hand-written, NOT reflected from the TypedDict), so a base-type
# WIDENING is caught even where nullability hides it from a bare-``type`` check. ``channels`` is an
# array of opaque channel objects; the item condition itself is pinned by _OUTPUT_ARRAY_ITEMS.
_FIND_CHANNELS_BASE_TYPE: dict[str, str | None] = {
    "enabled": "boolean",
    "channels": "array",
    "total": "integer",
    "capped": "boolean",
    "match_count": "integer",
    "note": "string",
}

# The keys find_channels emits on EVERY one of its four return paths. It is a SINGLE key, and that
# is the tool's shape rather than a gap in the table: the two MODES are disjoint (count_only carries
# ``match_count`` and no ``channels``/``total``, a list path the reverse) AND the configuration
# splits them again (``note`` only unconfigured, ``capped`` only on the configured list path), so
# ``enabled`` is the only field every path has. The exact per-path sets are asserted in the
# conformance test; this constant is only their intersection.
_FIND_CHANNELS_ALWAYS_PRESENT = frozenset({"enabled"})


@pytest.mark.asyncio
async def test_find_channels_exposes_typed_output_schema() -> None:
    """S29: find_channels advertises a STRUCTURED outputSchema (ChannelQueryResult), not the
    accept-all schema a plain ``dict[str, object]`` return yields. Red before the retype, and red
    while the tool still carries ``@mcp.tool(output_schema=None)``, which OVERRIDES the
    annotation-derived schema (that kwarg, not any post-pass, is what keeps the untyped tools
    schema-less). Checks properties non-empty, EXACTLY the 6 mapped fields (completeness both
    ways), and each field's :func:`_base_type`."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    schema = tools["find_channels"].outputSchema or {}
    properties = schema.get("properties", {})
    assert properties, "find_channels: outputSchema carries no typed properties"
    assert set(properties) == set(_FIND_CHANNELS_BASE_TYPE), (
        f"find_channels: advertised properties {sorted(properties)} != "
        f"expected {sorted(_FIND_CHANNELS_BASE_TYPE)}"
    )
    for field, prop in properties.items():
        actual = _base_type(prop)
        assert actual == _FIND_CHANNELS_BASE_TYPE[field], (
            f"find_channels.{field}: schema base type {actual!r} != "
            f"{_FIND_CHANNELS_BASE_TYPE[field]!r}"
        )


@pytest.mark.asyncio
async def test_find_channels_structured_output_conforms_to_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29 conformance: the EMITTED structuredContent must conform to its own outputSchema on ALL
    FOUR paths, and here the four are two MODES times two configurations, which is the whole risk
    of this tool. Configured, ``count_only`` returns {enabled, match_count} while the list mode
    returns {enabled, channels, total, capped}; UNCONFIGURED both gain a ``note`` and the list
    loses ``capped``. A single-path test would leave one mode's fields unproven, and no other test
    drives both modes over the wire.

    Driven through a REAL ``fastmcp.Client``, because only the wire validates, the in-process
    shortcut hands a return back UNVALIDATED, pinned by
    test_wire_validates_output_schema_while_in_process_does_not.

    SEAM, chosen deliberately: the two ENABLED paths fake the TRANSPORT
    (``channelfinder_client.get_shared_session``), not the client CLASS. Three consequences, each
    measured rather than assumed: the real ``_project`` builds the channel elements, so what the
    wire validates is a payload this server actually produces rather than a fake's invention
    (⚠️ honestly, the ITEM CONDITION cannot tell the difference, it is the opaque
    ``{type: object}``, so any dict passes it; what the real projection buys is that the four
    client-edge error paths sit in the way); the real client-edge
    guards stay in the path, which is what CLAUDE.md's evidence discipline point 8 recommends; and
    a class-level double would be COUNTED by scripts/guard_audit.py as a payload-claiming test,
    silently shifting the S31 audit's recorded numbers with nothing going red. The price, named:
    one faked response cannot serve both enabled paths, the list route rejects a number and the
    count route rejects a list, so the response is swapped between calls, and because this fakes
    the transport the call runs the real read throttle (the pre-existing repo-wide
    ``EPICS_MCP_READ_RATE_LIMIT`` exposure the search_logbook payload test already documents).

    Both CF allowlists are passed EXPLICITLY rather than left to their defaults: the client
    resolves them from the REAL config in its OWN module namespace, so an
    ``EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES`` exported on the machine would otherwise decide
    what this test sees (conftest isolates the ``EPICS_*`` search vars, NOT the ``EPICS_MCP_*``
    ones). ``owner`` is deliberately an allowlisted account: the projection blanks a non-allowlisted
    one, so a payload assertion on it would be about the redactor, not the schema.

    The STRONGEST assertion here is the per-path EXACT key set (see the comment at the ``paths``
    table): it is what makes all four rows load-bearing and what carries the mode disjointness,
    which the schema itself cannot express (``total=False`` without ``oneOf`` permits all six keys
    at once). Part B (static) checks nullability in BOTH directions against
    _FIND_CHANNELS_ALWAYS_PRESENT; ``seen == set(properties)`` is the weaker union check, kept
    because it is the direction that reddens when a NEW field is advertised and no row drives it.

    ⚠️ The runtime null check below is INERT on this tool: no path emits an explicit
    ``None``, so its body never runs (the same honest note the discover_pvs sibling carries).

    Red-proofs, all EXECUTED: delete the disabled-path ``count_only`` special case in
    services/checkers.py and the per-path equality goes red at the ``disabled-count`` row. When
    that was first measured over the FULL suite it reddened ONLY this test (1 failed / 1447
    passed), i.e. NO test guarded that mode's field set. It now reddens two, because the same
    review added the missing key-set assertion to test_channelfinder.py's
    test_tool_count_disabled_makes_no_call as well; the '1447' is kept as the dated measurement
    that justified this row, not as a current count. Widen ``ChannelQueryResult.channels`` to
    ``list[Any]`` and the sibling item-condition test goes red. Widen
    _FIND_CHANNELS_ALWAYS_PRESENT by ``capped`` and the always-present loop below goes red
    (together with the generic wire test, that mutation is not isolating, and it says so here
    rather than in a claim)."""
    from fastmcp import Client

    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    properties = (tools["find_channels"].outputSchema or {}).get("properties", {})
    assert properties, "find_channels: outputSchema carries no typed properties"

    disabled = EpicsConfig(channelfinder_url="")
    enabled = EpicsConfig(
        channelfinder_url="http://channelfinder:8080/ChannelFinder",
        channelfinder_safe_property_names="iocName,hostName,pvStatus",
        channelfinder_safe_owner_accounts="recceiver",
    )
    # ONE patch per namespace, re-aimed through a holder: the config is read at call time in both
    # places (checkers gates on the URL, the client resolves the allowlists), and re-patching inside
    # the loop would bind the loop variable.
    holder: dict[str, EpicsConfig] = {"cfg": disabled}
    monkeypatch.setattr("epics_mcp.services.checkers.get_config", lambda: holder["cfg"])
    monkeypatch.setattr("epics_mcp.services.channelfinder_client.get_config", lambda: holder["cfg"])
    session = Mock()
    monkeypatch.setattr(
        "epics_mcp.services.channelfinder_client.get_shared_session",
        lambda **_kwargs: session,
    )
    one_channel: object = [
        {
            "name": "SIM:PS-01:Cur-RB",
            "owner": "recceiver",
            "properties": [
                {"name": "iocName", "value": "ioc-01"},
                {"name": "hostName", "value": "host-01"},
                {"name": "pvStatus", "value": "Active"},
            ],
            "tags": [{"name": "archived"}],
        }
    ]
    # The EXACT key set each path must emit: the assertion that carries the mode disjointness, and
    # the reason all four paths are load-bearing. Without it the loop below only ever checked
    # "⊆ advertised" plus the one always-present key, which all four literals satisfy:
    # a path could silently take the WRONG branch (measured: deleting the disabled-path count
    # special-case makes disabled-count emit the LIST literal) and nothing would notice, while
    # ``seen`` stays complete because it is a UNION, it is in fact satisfiable by two of the four.
    # Nor could the schema catch it: ``total=False`` without ``oneOf`` permits all six keys at once,
    # so disjointness is not expressible there. Four directions, one per row.
    paths: list[tuple[str, EpicsConfig, dict[str, Any], object, set[str]]] = [
        (
            "disabled-list",
            disabled,
            {"name_pattern": "SIM:*"},
            None,
            {"enabled", "channels", "total", "note"},
        ),
        (
            "disabled-count",
            disabled,
            {"name_pattern": "SIM:*", "count_only": True},
            None,
            {"enabled", "match_count", "note"},
        ),
        (
            "enabled-list",
            enabled,
            {"name_pattern": "SIM:*"},
            one_channel,
            {"enabled", "channels", "total", "capped"},
        ),
        (
            "enabled-count",
            enabled,
            {"name_pattern": "SIM:*", "count_only": True},
            7,
            {"enabled", "match_count"},
        ),
    ]

    seen: set[str] = set()
    for label, cfg, args, response, expected_keys in paths:
        holder["cfg"] = cfg
        session.get.return_value = _fake_json_response(response)
        async with Client(mcp) as client:
            # Raises ToolError("Output validation error: ...") if the payload does not conform.
            result = await client.call_tool("find_channels", args)
        structured = cast(dict[str, Any], result.structured_content)
        seen |= set(structured)
        assert set(structured) == expected_keys, (
            f"find_channels[{label}]: emitted {sorted(structured)}, expected exactly "
            f"{sorted(expected_keys)}, either the path taken is not the one this row drives, or "
            "the two modes are no longer disjoint"
        )
        for key, value in structured.items():
            if value is None:
                assert _schema_permits_null(properties[key]), (
                    f"find_channels[{label}].{key}: emitted null but its schema forbids null "
                    f"({properties[key]})"
                )
        for always in _FIND_CHANNELS_ALWAYS_PRESENT:
            assert always in structured, (
                f"find_channels[{label}]: {always!r} is declared always-present (non-nullable) "
                f"but is absent from the emitted payload {sorted(structured)}"
            )
    # Every advertised field is reached by at least one path. Weaker than the per-path equality
    # above (a union cannot identify a path) and kept because it is the direction that goes red when
    # a NEW field is advertised and no row drives it.
    assert seen == set(properties), (
        f"find_channels: the four driven paths emitted {sorted(seen)}, "
        f"which does not cover the advertised {sorted(properties)}"
    )

    for prop_name, prop_schema in properties.items():
        if prop_name in _FIND_CHANNELS_ALWAYS_PRESENT:
            assert not _schema_permits_null(prop_schema), (
                f"find_channels.{prop_name}: declared always-present, so it must NOT permit null "
                f"in its outputSchema, got {prop_schema}"
            )
            continue
        assert _schema_permits_null(prop_schema), (
            f"find_channels.{prop_name}: sometimes-absent property must permit null "
            f"(the S29 convention), got {prop_schema}"
        )


def test_installed_but_broken_extra_keeps_all_surfaces_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard E (S26/N06, the fix's central regression guard): reproduce the INSTALLED-but-broken
    state at import time (find_spec truthy, the display_tools import fails) via importlib.reload,
    then assert the LIVE surfaces stay consistent, ``_DISPLAY_TOOLS_AVAILABLE`` False, instructions
    omit the display clause, the prompt omits validate_pvs.

    The ONLY test that fails if the single-truth derivation ``_DISPLAY_TOOLS_AVAILABLE =
    _display_registrar is not None`` regresses to the Round-1 find_spec formulation
    ``= _display_tools_available()``: the two diverge only in this state, which neither CI lane
    otherwise exercises. Install-independent (forces find_spec truthy + a broken import); restores
    the clean module afterwards so the rest of the suite is unaffected."""
    import importlib
    import sys

    import epics_mcp.server as server

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: (
            object() if name == "opi_navigation" else real_find_spec(name, *a, **k)
        ),
    )
    # A None entry makes ``from epics_mcp.display_tools import ...`` raise ImportError at reload.
    monkeypatch.setitem(sys.modules, "epics_mcp.display_tools", None)
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


# --- tools/list schema hygiene: standalone FastMCP emits lean schemas natively ---

# The tools whose TypedDict return yields a TYPED outputSchema (properties present). Named here as
# an INDEPENDENT source of truth, deliberately NOT reflected from the code, and deliberately not
# summarised in prose either: an earlier version of this comment enumerated the members and rotted
# behind the set below (it still named four when there were nine). The set IS the list; read it.
#
# Every OTHER tool returns dict[str, object] and advertises NO outputSchema. The mechanism is an
# explicit per-tool opt-out, ``@mcp.tool(output_schema=None)``, NOT a post-pass that strips empty
# schemas. Such a pass (``_prune_tool_schemas``) did exist and was DELETED in the standalone-fastmcp
# migration `6bd12c6`, because fastmcp emits lean schemas natively. Believing the deleted pass was
# still live is not hypothetical: it cost a later session a wrong plan, since it explains the None
# without pointing at the decorator that actually causes it. To type a tool, DELETE its kwarg:
# ``None`` is not "unset", it overrides the annotation-derived schema entirely.
#
# These are core tools, so the set is identical on a core-only ([displays] absent, 28 tools) and a
# full (32) install -> the assertions are RELATIONAL, never a COUNT (which would break core-only).
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
        "lookup_device_name",
        "is_archived",
        "list_archived_pvs",
        "get_appliance_info",
        "get_archive_info",
        "list_channel_vocabulary",
        "get_alarm_history",
        "discover_pvs",
        "find_channels",
    }
)

# Minimal args that reach each typed tool's DISABLED return over the wire. ONE ROW PER MEMBER of
# _TYPED_OUTPUT_TOOLS, the completeness assertion in the wire test below is what keeps it that way,
# and is the reason this table exists at all rather than another hand-maintained per-tool test.
#
# download_log_attachment needs a fully-specified identity/sink pair: it validates those BEFORE the
# config gate, so a "simpler" arg set raises there and the call never reaches a conformance check
# (over the wire that is an error result, which carries no structuredContent to validate at all).
#
# The Olog rows duplicate the local table in the Olog conformance test. That is safe by
# construction, not by luck: both tables are DRIVEN, so a drift in either one goes red in its own
# test. What a duplicate cannot hide is a MISSING tool, that is what the completeness check below
# covers, and no per-test table can.
_DISABLED_WIRE_ARGS: dict[str, dict[str, Any]] = {
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
    "get_pv_history": {
        "pv_name": "SIM:PS-01:Cur-RB",
        "start": "2026-01-01T00:00:00.000Z",
        "end": "2026-01-02T00:00:00.000Z",
    },
    "is_alarm_configured": {"pv_name": "SIM:PS-01:Cur-RB", "config_name": "SomeTree"},
    "lookup_device_name": {"name": "DEV-TEST01:Ctrl-EVR-01"},
    "is_archived": {"pv_name": "SIM:PS-01:Cur-RB"},
    "list_archived_pvs": {},
    "get_appliance_info": {},
    "get_archive_info": {"pv_name": "SIM:PS-01:Cur-RB"},
    "list_channel_vocabulary": {},
    "get_alarm_history": {
        "pv_name": "SIM:PS-01:Cur-RB",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
    },
    "discover_pvs": {"pattern": "SIM:PS-*"},
    # No count_only: the LIST mode's disabled return is the wider of the two (channels/total/note),
    # so it is the one worth driving generically. The count mode is covered per-tool, over the wire,
    # by test_find_channels_structured_output_conforms_to_its_schema.
    "find_channels": {"name_pattern": "SIM:PS-*"},
}

# DERIVED from the per-tool constants, never re-typed: the eleven explicit rows point straight at
# the constant its sibling conformance test declares; the Olog rows are import-time ``frozenset``
# conversions of _OLOG_ALWAYS_PRESENT's values (a copy in memory, but with no second place to edit,
# so it cannot drift either). Its purpose is to make those constants carry weight at
# RUNTIME. Measured, so the claim stays the right size: TEN of the twelve are consulted only to
# SKIP a field in a static check, so an over-broad entry weakens that test in silence instead of
# failing it (mutation-checked: widening _ARCHIVER_HISTORY_ALWAYS_PRESENT or
# _LIST_CHANNEL_VOCABULARY_ALWAYS_PRESENT leaves their tests green). The remaining TWO,
# _DISCOVER_PVS_ALWAYS_PRESENT and _FIND_CHANNELS_ALWAYS_PRESENT, are ALREADY runtime-bound by
# their own conformance tests and go red on the same mutation.
_ALWAYS_PRESENT_BY_TOOL: dict[str, frozenset[str]] = {
    **{name: frozenset(fields) for name, fields in _OLOG_ALWAYS_PRESENT.items()},
    "get_pv_history": _ARCHIVER_HISTORY_ALWAYS_PRESENT,
    "is_alarm_configured": _ALARM_CONFIGURED_ALWAYS_PRESENT,
    "lookup_device_name": _NAME_LOOKUP_ALWAYS_PRESENT,
    "is_archived": _ARCHIVE_STATUS_ALWAYS_PRESENT,
    "list_archived_pvs": _LIST_ARCHIVED_PVS_ALWAYS_PRESENT,
    "get_appliance_info": _GET_APPLIANCE_INFO_ALWAYS_PRESENT,
    "get_archive_info": _GET_ARCHIVE_INFO_ALWAYS_PRESENT,
    "list_channel_vocabulary": _LIST_CHANNEL_VOCABULARY_ALWAYS_PRESENT,
    "get_alarm_history": _GET_ALARM_HISTORY_ALWAYS_PRESENT,
    "discover_pvs": _DISCOVER_PVS_ALWAYS_PRESENT,
    "find_channels": _FIND_CHANNELS_ALWAYS_PRESENT,
}


@pytest.mark.asyncio
async def test_every_typed_tool_conforms_to_its_schema_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29: EVERY typed tool's disabled-path payload is validated by a REAL client, and the set of
    tools driven here is pinned to _TYPED_OUTPUT_TOOLS, so the coverage cannot silently fall behind.

    Why this exists next to the per-tool conformance tests rather than instead of them. Those tests
    each carry a hand-written nullability contract for ONE tool and are the right home for it. But
    they share two blind spots that no amount of care inside them can close:

    * They cannot notice a tool that has NO wire coverage. The completeness assertions below close
      that, but ONLY as the second link of a chain, and the distinction is measured, not assumed:
      a newly typed tool is DETECTED by the pre-existing
      test_output_schema_typed_only_for_typed_tools, which compares the server's REAL typed set
      against ``_TYPED_OUTPUT_TOOLS``. The assertions here compare two TABLES against that same
      constant, so a tool nobody has added to it is invisible to them (measured: registering a
      23rd typed tool leaves this test green and turns the relational one red). What they DO
      guarantee is the step after: once the first red forces the constant to be updated, wire
      coverage can no longer be forgotten, both tables must equal it. Neither link alone suffices;
      claiming this one detects new tools would be false.
    * TEN of the twelve drive ``FastMCP.call_tool``, which hands a return back UNVALIDATED (pinned
      by test_wire_validates_output_schema_while_in_process_does_not). Their runtime half therefore
      only ever checked the ONE thing it asserts explicitly, that an emitted null is permitted,
      and 13 of the 20 tools THOSE TEN cover emit no null at all on the driven path, so for those
      the runtime half asserts nothing beyond "the call did not raise". The other TWO,
      test_discover_pvs_structured_output_conforms_to_its_schema and its find_channels sibling,
      are the exceptions on every count: they already drive a real client over four paths, check
      always-present fields at runtime, and pin field coverage with ``seen == set(properties)``.
      (find_channels needs all four for a second reason: its two MODES return disjoint fields.)

    So the three per-tool assertions below are NOT new inventions, they are that one test's checks
    (its ``seen`` equality relaxed to a subset, since one disabled path cannot cover every field)
    carried from ONE tool to all 22. The non-vacuity floor is older still: every ``exposes`` test
    already carries it. Claiming otherwise would be the very defect this file keeps having to
    remove.

    HONEST SCOPE: this is a coverage guard, not a bug hunt. The same population was measured
    once over a real client with 0 violations (CHANGELOG, S29/LO), and mypy --strict already
    rejects almost every mis-typed return literal at commit time. What is bought here is that
    the measurement keeps holding, for every future tool, without anyone remembering to re-run
    it. Payload-carrying paths are a different and sharper question; they are driven per-tool,
    and only where a guard actually depends on one.

    Red-proof (both measured): drop a row from _DISABLED_WIRE_ARGS without dropping the tool from
    _TYPED_OUTPUT_TOOLS -> the completeness half goes red; emit a mis-typed value from any disabled
    return -> the wire half goes red while the in-process sibling test stays green."""
    from fastmcp import Client

    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp

    assert set(_DISABLED_WIRE_ARGS) == _TYPED_OUTPUT_TOOLS, (
        "_DISABLED_WIRE_ARGS is out of step with _TYPED_OUTPUT_TOOLS, missing rows: "
        f"{sorted(_TYPED_OUTPUT_TOOLS - set(_DISABLED_WIRE_ARGS))}, stale rows: "
        f"{sorted(set(_DISABLED_WIRE_ARGS) - _TYPED_OUTPUT_TOOLS)}"
    )
    assert set(_ALWAYS_PRESENT_BY_TOOL) == _TYPED_OUTPUT_TOOLS, (
        "_ALWAYS_PRESENT_BY_TOOL is out of step with _TYPED_OUTPUT_TOOLS, missing rows: "
        f"{sorted(_TYPED_OUTPUT_TOOLS - set(_ALWAYS_PRESENT_BY_TOOL))}, stale rows: "
        f"{sorted(set(_ALWAYS_PRESENT_BY_TOOL) - _TYPED_OUTPUT_TOOLS)}"
    )

    # EVERY plane empty in ONE config, so no tool's disabled path depends on what the environment
    # happens to carry. conftest isolates the EPICS_* search vars but deliberately NOT the
    # EPICS_MCP_* URLs, which is why the per-tool tests patch get_config instead of unsetting env.
    # Each plane resolves get_config in its OWN module namespace, hence three patch targets.
    disabled = EpicsConfig(
        channelfinder_url="", archiver_url="", alarm_url="", naming_url="", olog_url=""
    )
    for module_path in (
        "epics_mcp.services.checkers",
        "epics_mcp.services.checkers_olog",
        "epics_mcp.tools.archiver",
    ):
        monkeypatch.setattr(f"{module_path}.get_config", lambda: disabled)

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    async with Client(mcp) as client:
        for name in sorted(_DISABLED_WIRE_ARGS):
            # Raises ToolError("Output validation error: ...") if the payload does not conform.
            result = await client.call_tool(name, _DISABLED_WIRE_ARGS[name])
            structured = cast(dict[str, Any], result.structured_content)
            properties = (tools[name].outputSchema or {}).get("properties", {})

            # Non-vacuity floor: with no advertised properties the wire validates NOTHING and every
            # check below passes over an empty schema, the exact state an output_schema=None
            # opt-out produces.
            assert properties, f"{name}: outputSchema carries no typed properties"
            assert set(structured) <= set(properties), (
                f"{name}: emitted {sorted(set(structured) - set(properties))}, which the "
                "outputSchema does not advertise (the wire permits extra keys, and mypy only "
                "catches an undeclared key in a return LITERAL, not one merged in at runtime)"
            )
            for always in _ALWAYS_PRESENT_BY_TOOL[name]:
                assert always in structured, (
                    f"{name}: {always!r} is declared always-present (and therefore non-nullable) "
                    f"but is absent from the emitted payload {sorted(structured)}"
                )
            for key, value in structured.items():
                if value is None:
                    assert _schema_permits_null(properties[key]), (
                        f"{name}.{key}: emitted null but its schema forbids null "
                        f"({properties[key]})"
                    )


# Every (tool, field) whose outputSchema constrains MEMBERSHIP, with the members it must advertise.
# One row per enum that exists; the test below pins the mapping in BOTH directions, so this is not
# a list someone has to remember to extend.
_OUTPUT_ENUM_MEMBERS: dict[tuple[str, str], frozenset[object]] = {
    ("get_pv_history", "status"): frozenset({"ok", "empty", "withheld"}),
}


@pytest.mark.asyncio
async def test_typed_output_schema_enums_declare_their_members() -> None:
    """S29: a ``Literal[...]`` in a typed output schema keeps its members, the one constraint the
    base-type tables structurally cannot see.

    Those tables (_OLOG_FIELD_BASE_TYPE, _ARCHIVER_HISTORY_BASE_TYPE and their siblings) compare
    the COARSE JSON type. Measured: ``Literal["ok","empty","withheld"] | None`` and a bare
    ``str | None`` both render a non-null branch of type "string", so :func:`_base_type` returns
    "string" for either and widening the Literal away leaves every existing test green. Two live
    comments claimed the opposite; they are corrected where they stand.

    That matters because an enum is the SHARPEST constraint anything here advertises, the wire
    validator rejects an out-of-vocabulary value, whereas "string" accepts every string there is.
    Losing the members is a real narrowing of the contract that no caller can detect afterwards.

    The check is RELATIONAL in both directions, so it does not rot: the enums DISCOVERED across all
    typed schemas must be exactly the declared rows. A new Literal without a row goes red (nobody
    has to remember this test exists), and a Literal that loses its members goes red because its
    (tool, field) disappears from the discovered set.

    SCOPE, measured rather than implied: the walk covers TOP-LEVEL properties only. It does not
    descend into ``items``, ``$defs`` or nested ``anyOf`` branches, today that costs nothing (a
    full recursive traversal of all 22 schemas finds the same single enum, no ``const``, and no
    ``$defs`` at all, because every array element is an unconstrained object), but a ``Literal``
    inside a future list-element MODEL would go unnoticed. The auto-discovery promise above is
    therefore about scalar top-level fields, not about every enum a schema could ever carry.

    Red-proof: widen tools/archiver.py's ``status`` annotation from ``Literal[...] | None`` to
    ``str | None``."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    discovered: dict[tuple[str, str], frozenset[object]] = {}
    for name in sorted(_TYPED_OUTPUT_TOOLS):
        properties = (tools[name].outputSchema or {}).get("properties", {})
        assert properties, f"{name}: outputSchema carries no typed properties"
        for field, prop in properties.items():
            members = _enum_members(prop)
            if members is not None:
                discovered[name, field] = members

    assert set(discovered) == set(_OUTPUT_ENUM_MEMBERS), (
        "the enums advertised by the typed output schemas no longer match the declared ones, "
        f"undeclared: {sorted(set(discovered) - set(_OUTPUT_ENUM_MEMBERS))}, gone (a Literal was "
        f"widened away?): {sorted(set(_OUTPUT_ENUM_MEMBERS) - set(discovered))}"
    )
    for key, expected in _OUTPUT_ENUM_MEMBERS.items():
        assert discovered[key] == expected, (
            f"{key[0]}.{key[1]}: advertised enum members {sorted(map(repr, discovered[key]))} != "
            f"{sorted(map(repr, expected))}"
        )


def test_enum_members_keeps_the_value_type() -> None:
    """S31: :func:`_enum_members` must not collapse an enum's VALUE TYPE.

    It used to apply ``str()`` at both of its extraction sites, so ``Literal[1, 2]`` and
    ``Literal["1", "2"]`` produced the identical frozenset. That is the same blindness as the one
    the helper exists to remove, one level further down: the base-type tables cannot tell a
    Literal from a bare str, and a stringifying _enum_members cannot tell an int-enum from a
    str-enum. Today it is inert, the single enum in the estate is all-string, but it would
    silently accept a future widening from numbers to their spellings.

    Why a HELPER unit test and not a source mutation, measured rather than assumed: a recursive
    walk of all 22 typed schemas finds exactly one ``enum`` and no ``const``
    (``get_pv_history.status``), and _ARCHIVER_HISTORY_BASE_TYPE already pins it as "string" by
    set equality. Retyping it to ``Literal[1, 2]`` therefore reddens that table too, and the red
    would not be attributable to this guard. There is no source mutation that isolates the VALUE
    TYPE collapse, so the guard sits where the defect sits.

    Red-proof: restore ``frozenset(str(member) for member in declared)`` at the bare-``enum`` site
    and the first assertion fails; restore it at the ``anyOf`` site and the third fails. Both
    sites need their own assertion, one covers the other's line not at all. mypy --strict stays
    green under both mutations (frozenset is covariant, so ``frozenset[str]`` is a legal
    ``frozenset[object]``), which is why a type checker cannot stand in for this test."""
    assert _enum_members({"enum": [1, 2]}) == frozenset({1, 2})
    assert _enum_members({"anyOf": [{"enum": [1, 2]}, {"type": "null"}]}) == frozenset({1, 2})
    assert _enum_members({"type": "string"}) is None
    # A bare enum wins over an anyOf branch, and the branches are never merged, the same
    # precedence _base_type and _schema_permits_null use, pinned here because nothing else does.
    mixed = {"enum": [1], "anyOf": [{"enum": [2]}]}
    assert _enum_members(mixed) == frozenset({1})


# The two element schemas the estate actually advertises, shared by the rows below. Read-only by
# convention: they are module dicts, so assigning into one would silently move 7 (resp. 9) rows.
_STRING_ITEMS: dict[str, object] = {"type": "string"}
_OPAQUE_OBJECT_ITEMS: dict[str, object] = {"type": "object", "additionalProperties": True}

# Every (tool, field) whose outputSchema advertises an ARRAY, with the element schema it must
# declare. FIVE of these are anyOf[array, null] (an ``X | None`` field): find_channels.channels,
# update_log_entry.warnings, and attachments_uploaded on create_log_entry / reply_to_log /
# add_log_attachment, a helper that only read prop["items"] would find 11 of 16 and drop those
# five in silence. Note ("list_tags",
# "tags") is the OLOG tool; the ChannelFinder tag names live under list_channel_vocabulary.
# No entry may be None: an array without an element condition is not declarable here, so an
# annotation that loses one stays red instead of being papered over with a row.
_OUTPUT_ARRAY_ITEMS: dict[tuple[str, str], dict[str, object]] = {
    ("list_logbooks", "logbooks"): _STRING_ITEMS,
    ("list_tags", "tags"): _STRING_ITEMS,
    ("list_log_levels", "levels"): _STRING_ITEMS,
    ("list_archived_pvs", "pvs"): _STRING_ITEMS,
    ("list_channel_vocabulary", "properties"): _STRING_ITEMS,
    ("list_channel_vocabulary", "tags"): _STRING_ITEMS,
    ("update_log_entry", "warnings"): _STRING_ITEMS,
    ("search_logbook", "entries"): _OPAQUE_OBJECT_ITEMS,
    ("get_pv_history", "samples"): _OPAQUE_OBJECT_ITEMS,
    ("get_alarm_history", "events"): _OPAQUE_OBJECT_ITEMS,
    ("list_log_attachments", "attachments"): _OPAQUE_OBJECT_ITEMS,
    ("discover_pvs", "pvs"): _OPAQUE_OBJECT_ITEMS,
    ("find_channels", "channels"): _OPAQUE_OBJECT_ITEMS,
    ("create_log_entry", "attachments_uploaded"): _OPAQUE_OBJECT_ITEMS,
    ("reply_to_log", "attachments_uploaded"): _OPAQUE_OBJECT_ITEMS,
    ("add_log_attachment", "attachments_uploaded"): _OPAQUE_OBJECT_ITEMS,
}


@pytest.mark.asyncio
async def test_typed_output_schema_arrays_declare_their_element_schema() -> None:
    """S31: an array in a typed output schema keeps its ``items`` condition, the constraint the
    base-type tables cannot see, one level below the enum they also cannot see.

    Those tables compare the COARSE JSON type, so ``list[str]`` and ``list[Any]`` are both
    "array" and widening one leaves them green. Measured: pydantic renders ``list[Any]`` as
    ``{"type": "array", "items": {}}``: the ``items`` KEY survives and turns empty. A guard that
    only asked "does it have items?" would therefore not notice at all; what has to be compared
    is the element schema itself.

    That matters most for the 7 rows carrying ``{"type": "string"}``: over the wire the validator
    rejects ``["a", 42]`` against them, and accepts it once the condition is gone. Stated
    honestly, the other 9 rows are ``{"type": "object", "additionalProperties": true}``, a
    deliberately opaque element (no output shape in this server nests a TypedDict). There the
    guard pins only that the schema still promises "some object" rather than "anything at all";
    it cannot catch a wrong object.

    RELATIONAL in both directions, so it does not rot. The discovery is anchored on
    ``_base_type(prop) == "array"``, a property the mutation cannot touch, rather than on
    "carries items", which the mutation could have emptied. Two consequences: the key set stays
    stable under a widening, so the precise VALUE assertion fires instead of an indirect "gone",
    and an array field that was NEVER constrained still appears and goes red without a row.

    SCOPE, measured rather than implied: top-level properties of the 22 tools in
    _TYPED_OUTPUT_TOOLS. No recursion into ``items``, no ``$defs``, no ``prefixItems`` (a scan of
    all 22 schemas finds zero of either). Deliberately outside: fields typed ``object | None``
    that hold a list at runtime, get_archive_info's ``archive_fields``/``data_stores`` are the
    live example, since _base_type returns None for them; this pins DECLARED arrays, not every
    field that can carry one. Also outside: the list fields of the 10 tools that are not typed
    yet, which have no advertised schema to pin.

    Red-proof: widen services/checkers.py's ``ChannelVocabularyResult.tags`` to ``list[Any]``."""
    from epics_mcp.server import mcp

    tools = {tool.name: tool for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    discovered: dict[tuple[str, str], dict[str, object] | None] = {}
    for name in sorted(_TYPED_OUTPUT_TOOLS):
        properties = (tools[name].outputSchema or {}).get("properties", {})
        assert properties, f"{name}: outputSchema carries no typed properties"
        for field, prop in properties.items():
            if _base_type(prop) == "array":
                discovered[name, field] = _array_items(prop)

    assert set(discovered) == set(_OUTPUT_ARRAY_ITEMS), (
        "the arrays advertised by the typed output schemas no longer match the declared ones, "
        f"undeclared: {sorted(set(discovered) - set(_OUTPUT_ARRAY_ITEMS))}, gone (a list field "
        f"was retyped or dropped?): {sorted(set(_OUTPUT_ARRAY_ITEMS) - set(discovered))}"
    )
    for key, expected in _OUTPUT_ARRAY_ITEMS.items():
        assert discovered[key] == expected, (
            f"{key[0]}.{key[1]}: advertised element schema {discovered[key]!r} != {expected!r}"
        )


def test_array_items_reads_both_array_shapes() -> None:
    """S31: :func:`_array_items` finds the element schema in BOTH shapes an array is emitted in.

    Five of the 16 declared arrays are ``anyOf[array, null]``, so a helper that only read
    ``prop["items"]`` would silently cover 11 of them. Nothing else can prove that branch runs:
    tests/ is outside ``--cov=src``, so no coverage number ever shows it, and the schema test
    above passes either way as long as the five rows happen to match.

    Red-proofs, both executed: drop the ``anyOf``/``oneOf`` loop and the second assertion fails;
    return ``prop.get("items")`` unchecked and the LAST assertion fails. ⚠️ The last one is not
    decoration, an earlier version of this docstring claimed the ``{"type": "array"}``-without-
    items case pinned that narrowing, and it does not: the unchecked version returns ``None``
    there too, so all five assertions stayed green. A non-dict ``items`` is the only input that
    tells the two paths apart."""
    assert _array_items({"type": "array", "items": {"type": "string"}}) == {"type": "string"}
    nullable = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
    assert _array_items(nullable) == {"type": "string"}
    assert _array_items({"type": "array"}) is None
    assert _array_items({"type": "array", "items": {}}) == {}
    assert _array_items({"type": "string"}) is None
    assert _array_items({"type": "array", "items": "junk"}) is None


def _fake_json_response(payload: object) -> Mock:
    """A ``requests``-shaped response double carrying *payload*.

    ``is_redirect`` is set explicitly: on a bare Mock every attribute is truthy, so a plain double
    trips the clients' redirect refusal before its body is ever read."""
    resp = Mock(is_redirect=False)
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# A minimal readable Olog entry: _require_entry demands a dict with a non-null ``id`` and nothing
# else, so this is the smallest payload that reaches the projection instead of being rejected.
_MINIMAL_OLOG_ENTRY: dict[str, object] = {
    "id": 1,
    "createdDate": 1717200000000,
    "level": "Info",
    "state": "Active",
    "logbooks": [{"name": "Ops"}],
    "tags": [],
    "attachments": [],
    "properties": [],
}


@pytest.mark.asyncio
async def test_search_logbook_payload_path_is_guarded_below_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29: drive search_logbook's ENABLED path with a payload, faking HTTP rather than the client.

    WHERE THE FAKE SITS IS THE POINT, and it is the lesson this test exists to bank. The house
    style elsewhere replaces a whole client class (``monkeypatch.setattr(..., "OlogClient",
    _Fake)``). That is convenient and it silently removes every guard the real client runs at its
    edge, a test written that way can appear to protect ``_hit_count`` while never executing it
    (measured with a spy: not reached). Faking ``get_shared_session`` instead leaves the real
    OlogClient in the path and only replaces the socket.

    Two halves, both driven over a real client so the emitted payload is schema-validated:

    * A well-formed response: the enabled path emits entries and a real ``total_matches``. This is
      the first time a NON-EMPTY entries list is validated at all, an empty array satisfies any
      item constraint by construction, so the disabled-path coverage cannot reach it. Worth the
      right size, though: ``entries`` is ``list[dict[str, object]]``, so its item constraint is
      only "is an object". The sharp one (``items: {type: string}``) lives on the sibling test's
      ``tags``; what this half really pins is ``total_matches``, below.

    ENV NOTE, measured: because this fakes the TRANSPORT, the call runs the real read throttle
    (services/_http.py). conftest resets that throttle but rebuilds it from the REAL config, and
    ``EPICS_MCP_*`` is deliberately not isolated there, so a machine exporting
    ``EPICS_MCP_READ_RATE_LIMIT`` fails this test. It is a pre-existing, repo-wide exposure:
    measured 2026-07-25 under ``EPICS_MCP_READ_RATE_LIMIT=1``: 19 other tests fail the same way
    (the figure named here was 15 and had already drifted by three before that measurement, which
    is what an unasserted count does). The limit VALUE belongs in the claim: a test drawing two
    tokens only trips at 1. Not something this test introduced; CI runs
    with a clean env. Named here rather than worked around locally, because the fix belongs in
    conftest, for all of them at once.
    * A ``hitCount`` of ``true``: JSON has no separate boolean-vs-integer question the way Python
      does not either (``bool`` IS an ``int``), so this value would flow straight into an
      ``integer | null`` field. The client-edge guard rejects it FIRST, with its own diagnosis
      rather than a schema error, that is what this half pins.

    Red-proof: drop ``and not isinstance(count, bool)`` from services/olog_client.py's
    ``_hit_count``. Half 2 then goes red, and instructively so: the failure becomes the wire's
    ``Output validation error``, because the schema is the backstop that catches exactly what the
    guard was there to stop. mypy stays green throughout (bool ⊆ int), which is why a type checker
    cannot stand in for this test."""
    from fastmcp import Client

    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp

    session = Mock()
    monkeypatch.setattr(
        "epics_mcp.services.checkers_olog.get_config",
        lambda: EpicsConfig(olog_url="http://logbook:8080/Olog"),
    )
    monkeypatch.setattr(
        "epics_mcp.services.olog_client.get_shared_session", lambda **_kwargs: session
    )

    async with Client(mcp) as client:
        session.get.return_value = _fake_json_response(
            {"logs": [_MINIMAL_OLOG_ENTRY], "hitCount": 7}
        )
        structured = cast(
            dict[str, Any], (await client.call_tool("search_logbook", {})).structured_content
        )
        assert structured["enabled"] is True, structured
        assert structured["total_matches"] == 7, structured
        assert len(cast(list[object], structured["entries"])) == 1, structured

        session.get.return_value = _fake_json_response({"logs": [], "hitCount": True})
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("search_logbook", {})
    assert "hitCount" in str(excinfo.value), (
        "a boolean hitCount no longer produces the client-edge guard's own diagnosis. Two very "
        "different causes look identical here, so check which one it is: an 'Output validation "
        "error' means the GUARD IS GONE and only the schema caught the value; any other wording "
        f"means the guard still fired and merely says it differently. Got: {excinfo.value!r}"
    )


@pytest.mark.asyncio
async def test_channel_vocabulary_payload_path_is_guarded_below_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S29: the sibling of the search_logbook payload test, on the other kind of list constraint.

    ``tags`` is a ``list[str]``, so its schema carries ``items: {type: string}``, the sharpest
    element constraint anything here advertises, and one that NOTHING exercises today: the disabled
    path emits an empty list, and an empty array satisfies any item constraint by construction.

    Same seam discipline as its sibling and for the same measured reason: faking
    ``get_shared_session`` keeps the real ChannelFinderClient, and therefore its ``_named_list``
    edge guard, in the path, where replacing the client class would remove it unnoticed.

    HONEST DIFFERENCE from the search_logbook sibling, measured rather than assumed: that one is
    the ONLY guard on its branch (dropping the bool defence in ``_hit_count`` leaves every other
    test green). ``_named_list`` is NOT in that position, test_channelfinder.py's
    test_list_vocabulary_strict_on_bad_payload already pins it, and goes red on the same mutant.
    So this test does not claim to rescue an unguarded branch. What it adds is the part that
    client-level test structurally cannot reach: that the guard's refusal survives the tool layer
    and arrives at a REAL caller as a diagnosis rather than being remapped or swallowed, and that
    the ``items`` constraint is exercised at all.

    Two scope limits, both measured with a spy rather than reasoned about:

    * Half 2's bad payload is rejected inside ``list_properties``, which runs FIRST, ``list_tags``
      is never reached on that call. The guard being pinned is ``_named_list``, which both share;
      it is not a statement about the tags endpoint specifically.
    * One faked response serves BOTH endpoints, so ``structured["tags"] == ["vacuum"]`` pins the
      result SLOT, not the URL behind it. Measured: making ``list_tags`` read the properties URL
      leaves the whole suite green. That gap belonged to the ChannelFinder client's own tests, not
      here, and is now CLOSED there by
      test_channelfinder.py::test_each_route_targets_its_own_endpoint (S31), which asserts the
      requested URL of each route; the sibling gap in the Olog client is closed by
      test_olog.py::test_each_listing_route_targets_its_own_endpoint. The scope limit on THIS test
      stands unchanged, it still pins the slot, not the address.

    Red-proof: drop the ``isinstance(item.get("name"), str)`` half of ``_named_list``'s item check
    in services/channelfinder_client.py. Half 2 goes red and the failure turns into the wire's
    ``Output validation error: 42 is not of type 'string'``, the schema catching what the guard
    was there to stop. mypy stays green (the narrowed dict yields ``Any``), so again a type checker
    cannot stand in for this."""
    from fastmcp import Client

    from epics_mcp.config import EpicsConfig
    from epics_mcp.server import mcp

    session = Mock()
    monkeypatch.setattr(
        "epics_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://channelfinder:8080/ChannelFinder"),
    )
    monkeypatch.setattr(
        "epics_mcp.services.channelfinder_client.get_shared_session",
        lambda **_kwargs: session,
    )

    async with Client(mcp) as client:
        # The owner is deliberately present: it is DS-privacy data the client must drop, so its
        # absence downstream is part of what a payload-carrying path should show.
        session.get.return_value = _fake_json_response([{"name": "vacuum", "owner": "someone"}])
        structured = cast(
            dict[str, Any],
            (await client.call_tool("list_channel_vocabulary", {})).structured_content,
        )
        assert structured["enabled"] is True, structured
        assert structured["tags"] == ["vacuum"], structured

        session.get.return_value = _fake_json_response([{"name": 42}])
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("list_channel_vocabulary", {})
    assert "unreadable item" in str(excinfo.value), (
        "a non-string name no longer meets the client-edge guard's own diagnosis, if this now "
        f"reads as a schema violation, the guard was removed and only the wire caught it: "
        f"{excinfo.value!r}"
    )


# The four tools that carry a parameter literally NAMED ``title`` (the schema-aware-strip trap).
_TITLE_PARAMETER_TOOLS = ("create_log_entry", "reply_to_log", "update_log_entry", "search_logbook")


def _count_title_keys(node: object) -> int:
    """Naive, traversal-independent count of EVERY ``title`` key in a schema structure, annotation
    OR real property name. Deliberately NOT schema-aware, so it cross-checks the schema-aware walk
    below without sharing its logic. (It once cross-checked a hand-written strip pass; that pass was
    deleted in `6bd12c6` when standalone fastmcp began omitting the annotations natively. The
    cross-check stays as the SDK-regression net.)"""
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
    an annotation. An independent re-statement of the spec the emitted schemas must satisfy."""
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


def _schema_nodes_with_null_default(node: object, path: str = "root") -> list[str]:
    """Schema-aware walk: a label for every SCHEMA NODE carrying a ``default: null``, pure wire
    noise. The SDK-bundled FastMCP 1.0 emitted one per ``TypedDict total=False`` field and a
    hand-written pass removed them; standalone fastmcp emits none (measured: no ``default`` key
    anywhere in any typed schema), so this is now purely the regression net. Walks a
    properties/$defs map by its VALUES so a property NAMED ``default`` is never mistaken for it."""
    hits: list[str] = []
    if isinstance(node, dict):
        if "default" in node and node["default"] is None:
            hits.append(path)
        for key, value in node.items():
            if key in _JSCHEMA_MAP_KW:
                if isinstance(value, dict):
                    for name, sub in value.items():
                        hits += _schema_nodes_with_null_default(sub, f"{path}.{key}[{name}]")
            elif key in _JSCHEMA_SUB_KW:
                hits += _schema_nodes_with_null_default(value, f"{path}.{key}")
            elif key in _JSCHEMA_LIST_KW and isinstance(value, list):
                for i, sub in enumerate(value):
                    hits += _schema_nodes_with_null_default(sub, f"{path}.{key}[{i}]")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            hits += _schema_nodes_with_null_default(item, f"{path}[{i}]")
    return hits


@pytest.mark.asyncio
async def test_input_schemas_carry_no_title_annotation() -> None:
    """MA-Q1 A1: NO inputSchema node advertises a ``title`` annotation (schema-aware walk).
    Standalone fastmcp omits them natively, the hand-written post-pass this once guarded was
    deleted in `6bd12c6`. The guard stays: it is what would catch an SDK regression that starts
    emitting them again. Red on the SDK-bundled FastMCP 1.0 code (titles on every node)."""
    from epics_mcp.server import mcp

    for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]:
        residual = _schema_nodes_with_title(tool.inputSchema)
        assert not residual, f"{tool.name}: title annotation(s) survived at {residual}"


@pytest.mark.asyncio
async def test_output_schema_fields_carry_no_title_annotation() -> None:
    """Standalone FastMCP emits a typed outputSchema WITHOUT any ``title`` annotations at all:
    neither field-level nor the root TypedDict name (the SDK stack carried the latter; the
    ``*_exposes_typed_output_schema`` tests re-anchor identity on the field SET instead). This
    guards that no title noise regrows on the wire."""
    from epics_mcp.server import mcp

    for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]:
        if tool.name not in _TYPED_OUTPUT_TOOLS:
            continue
        assert tool.outputSchema is not None, f"{tool.name}: typed tool lost its outputSchema"
        titled = _schema_nodes_with_title(tool.outputSchema)
        assert titled == [], (
            f"{tool.name}: standalone FastMCP emits no title annotations at all; "
            f"got title nodes at {titled}"
        )


@pytest.mark.asyncio
async def test_output_schemas_carry_no_null_default() -> None:
    """MA-Q1 A3 (L1): NO typed-outputSchema node carries a ``default: null`` (the always-null
    ``total=False`` annotation). Standalone fastmcp omits them natively; the hand-written pass this
    once guarded was deleted in `6bd12c6`, and the guard stays as the SDK-regression net. Red
    against the SDK-bundled FastMCP 1.0 code (every field emitted default:null)."""
    from epics_mcp.server import mcp

    for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]:
        if tool.name not in _TYPED_OUTPUT_TOOLS:
            continue
        assert tool.outputSchema is not None, f"{tool.name}: typed tool lost its outputSchema"
        residual = _schema_nodes_with_null_default(tool.outputSchema)
        assert not residual, f"{tool.name}: null default(s) survived at {residual}"


@pytest.mark.asyncio
async def test_only_real_title_parameters_remain() -> None:
    """MA-Q1 A1 (traversal-independent cross-check): the ONLY ``title`` keys left across all
    inputSchemas are the real ``title`` PARAMETER names, one per tool that declares one at the top
    level. Naive count == count of tools with a top-level ``title`` property. Red against the
    SDK-bundled FastMCP 1.0 code, where the annotations inflated the naive count."""
    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    residual = sum(_count_title_keys(t.inputSchema) for t in tools)
    expected = sum(1 for t in tools if "title" in (t.inputSchema.get("properties") or {}))
    assert residual == expected, (
        f"{residual} residual 'title' keys != {expected} real title parameters, "
        "an annotation survived or a real title parameter was deleted"
    )


@pytest.mark.asyncio
async def test_title_parameter_tools_keep_their_title_property() -> None:
    """MA-Q1 critical trap: the four tools with a ``title`` PARAMETER still expose
    ``properties['title']`` WITH its description. A naive 'pop every title key' deletes it. Red on a
    naive strip.

    AND the tuple itself is pinned to the wire, which it was not: ``_TITLE_PARAMETER_TOOLS`` is
    hand-typed, this test already had the full tool map in its hand, and nothing compared the two.
    Giving a fifth tool a ``title`` parameter therefore left the whole lane green while every
    sentence about that set was false, a loop over a list can only check the members it was
    given, never that the list is all of them.

    Compared as SETS, deliberately: the tuple is in the author's order and the wire is in
    registration order (``search_logbook`` is registered first but typed last), so a sequence
    comparison would go red for a reason that is not a change in the finding, the mistake this
    estate's ``_UNOBSERVED`` table records for line numbers.

    Red proof, executed: add a ``title`` parameter to ``list_tags`` in server.py. Before this
    assertion the node stayed green; with it the node names list_tags as on-the-wire-only."""
    from epics_mcp.server import mcp

    tools = {t.name: t for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    for name in _TITLE_PARAMETER_TOOLS:
        properties = tools[name].inputSchema.get("properties") or {}
        assert "title" in properties, f"{name}: title parameter lost from properties"
        assert properties["title"].get("description"), (
            f"{name}: title parameter kept but its description was stripped"
        )

    on_the_wire = {
        name
        for name, tool in tools.items()
        if "title" in (tool.inputSchema.get("properties") or {})
    }
    assert on_the_wire == set(_TITLE_PARAMETER_TOOLS), (
        "_TITLE_PARAMETER_TOOLS no longer names the tools that actually put a ``title`` property "
        f"on the wire, on the wire only: {sorted(on_the_wire - set(_TITLE_PARAMETER_TOOLS))}, "
        f"in the tuple only: {sorted(set(_TITLE_PARAMETER_TOOLS) - on_the_wire)}"
    )


@pytest.mark.asyncio
async def test_every_required_arg_exists_in_properties() -> None:
    """MA-Q1 critical trap (the falsifiable core): for every tool, each ``required`` entry exists
    in ``properties``. A naive title-strip leaves ``title`` in create_log_entry/reply_to_log's
    ``required`` but deletes it from ``properties``, a required arg hidden from the wire while
    pydantic still enforces it. Red on a naive strip."""
    from epics_mcp.server import mcp

    for tool in [_t.to_mcp_tool() for _t in await mcp.list_tools()]:
        schema = tool.inputSchema
        properties = schema.get("properties") or {}
        for req in schema.get("required", []):
            assert req in properties, (
                f"{tool.name}: required arg {req!r} missing from properties "
                "(schema-breaking title strip?)"
            )


@pytest.mark.asyncio
async def test_output_schema_typed_only_for_typed_tools() -> None:
    """MA-Q1 A2 (RELATIONAL, not a count, so core-only [28] and full [32] both pass): a tool
    advertises an outputSchema WITH properties iff it is in ``_TYPED_OUTPUT_TOOLS``; every other
    present tool advertises NONE. (The membership list is that set, not this docstring, see its
    comment for why enumerating it here is a rotting hazard.) Proves the accept-all schemas stay
    suppressed AND the typed ones survive. Red on a blanket suppression (typed schemas gone) or on
    a lost ``output_schema=None`` opt-out (an accept-all schema reappears).

    Also the built-in S29 red-proof: add a tool to the set BEFORE typing it and this goes red,
    because its ``@mcp.tool(output_schema=None)`` still overrides the annotation. Two sibling tests
    (title-annotation, null-default) assert ``outputSchema is not None`` for set members too, so
    that red-proof trips THREE tests, not one, measured, not assumed."""
    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
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
    """MA-Q1: whatever suppresses the ``title`` ANNOTATIONS must not touch field ``description``s:
    the point-of-need semantics the repo DoD requires. Spot-check a distinctive, anchored one."""
    from epics_mcp.server import mcp

    tools = {t.name: t for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    name_pattern = tools["find_channels"].inputSchema["properties"]["name_pattern"]
    assert "ANCHORED" in name_pattern["description"]


@pytest.mark.asyncio
async def test_find_channels_description_separates_a_refusal_from_a_silent_zero() -> None:
    """Description pin for the distinction _claim_property actually makes.

    A property name off the allowlist, which every misspelling is, is refused client-side with
    INVALID_INPUT before the request leaves; an ALLOWLISTED name this instance does not carry
    narrows the result to 0. The description used to fold both into one "unknown/misspelled is
    NOT a server error, it narrows to 0", which sent a reader hunting for a typo that would in
    fact have raised. test_channelfinder.py proves the behaviour (a gate ValueError surfaces as
    INVALID_INPUT before any network call); this pins that the description still says so, since
    a wrong tool description is read as fact and nothing else guards this prose.
    """
    from epics_mcp.server import mcp

    tools = {t.name: t for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    # Whitespace-normalised: the description is a wrapped docstring, so a phrase that reads as one
    # line in the source is split by a newline plus indentation here. Asserting on the raw text
    # would pin the line breaks rather than the claim, and go red on a harmless re-wrap.
    description = " ".join((tools["find_channels"].description or "").split())
    lowered = description.lower()
    # POSITIVE: each half by a phrase only THIS caveat carries. Bare "INVALID_INPUT" would not do:
    # caveat (1) already contains it, so the refuted wording could return with every token still
    # present. Case-folded, because the emphasis capitals are formatting, not the claim.
    assert "not on the allowlist" in lowered, "the refusal half of the distinction is gone"
    assert "refused client-side with invalid_input" in lowered, "the refusal lost its error code"
    assert "name this instance does not carry" in lowered, "the narrows-to-0 half is gone"
    assert "tag names are never allowlisted" in lowered, "the tag exemption is gone"
    # NEGATIVE: the measured-false wording must not come back. It folded the two cases into one
    # and sent a reader hunting a typo that would in fact have raised.
    assert "misspelled property name is not a server error" not in lowered, (
        "the refuted wording is back: a misspelled property name IS refused, see "
        "channelfinder_client._claim_property"
    )


@pytest.mark.asyncio
async def test_stripped_tool_still_returns_structured_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA-Q1 A2 is ADVERTISE-ONLY: suppressing the wire outputSchema does not stop a tool from
    returning structuredContent at call time. Drive ``get_pv_value``, an untyped tool, i.e. one
    that still carries ``@mcp.tool(output_schema=None)`` so its accept-all schema is never
    advertised, over a faked ``pv_get`` (no network) and assert it still yields a structured dict.

    S29: the FIRST assertion guards this test's own PREMISE, and that is the point of it. Without
    it, typing the driven tool leaves this test GREEN and silently VACUOUS: a payload keeps
    arriving, so the second assertion still passes while "an untyped tool" has quietly stopped
    being true. This file's own history is the evidence, get_archive_info, then discover_pvs, then
    find_channels all aged out of this slot, and each time what was left behind was a prose
    instruction to re-point the test, which nothing enforced. The guard was added while
    find_channels still sat here and then, when find_channels was typed, it went red and forced
    this move, which is what it is for.

    HONEST CHANGE OF KIND with that move, so nobody reads more into it than it shows: the three
    predecessors each had a CONFIG-GATED real path (no ``*_URL`` set → an ``enabled: false``
    literal, no network by construction). ``get_pv_value`` has no such path, it is a bare
    ``await pv_get(...)``: so determinism comes from faking that one seam instead. The claim under
    test is unaffected: it is about the SERVER's schema-advertising versus its structuredContent
    plumbing, and where the payload came from does not enter it. The seam is the same
    module-namespace patch tests/test_read.py and this file's own get_pv_value tests already use.

    Red-proof: point ``driven`` at a member of _TYPED_OUTPUT_TOOLS (discover_pvs, args
    ``{"pattern": "SIM:*"}``), the premise assertion fails first, by test node id."""
    from epics_mcp.server import mcp

    async def _fake_pv_get(pv_name: str, timeout: float | None = None) -> dict[str, object]:
        return {"pv_name": pv_name, "value": 42, "connected": True}

    driven = "get_pv_value"
    args: dict[str, Any] = {"pv_name": "SIM:PS-01:Cur-RB"}
    tools = {t.name: t for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    assert tools[driven].outputSchema is None, (
        f"{driven} now advertises an outputSchema, so driving it no longer demonstrates the "
        "ADVERTISE-ONLY property this test exists for, re-point it at a still-untyped tool "
        "with a no-network path (_TYPED_OUTPUT_TOOLS lists the ones already taken)"
    )
    # get_pv_value -> _get_pv_value -> pv_get, resolved in the tools.read module's OWN namespace.
    monkeypatch.setattr("epics_mcp.tools.read.pv_get", _fake_pv_get)
    structured = cast(
        dict[str, Any],
        (await mcp.call_tool(driven, args)).structured_content,
    )
    # Produced structuredContent despite the dropped wire schema.
    assert structured.get("value") == 42


# tools/list size-gate ceiling (chars of the compact ListToolsResult wire payload). Until
# 2026-07-25 this was a TIGHT budget guard at 70_000 that forced S29 output-schema typing to land
# one tool per session. It was raised to 200_000: typed output-schema bytes make tool RESULTS
# machine-readable (the core value of S29) and cost only ~1% more context per agent turn, so the
# tools we need anyway may be typed freely. The guard is now a SOFT catastrophe-ceiling: it no
# longer bounds each tool's growth, only trips on an extreme accidental blow-up. It stays
# RELATIONAL (a ``<=`` check) so both lanes pass. Measured 2026-08-08 after GB-65: the
# core lane is 64_719 and the full lane 75_853 (the docstring below carries the deltas, and the
# pairs 64_719 / 75_699 from earlier the same day, 64_719 / 74_899 from 2026-08-06 and
# 63_017 / 72_508 from 2026-08-02 are superseded).
# ⚠ This sentence and the docstring's are TWO copies of the same measurement and they have
# drifted apart once already, in the very commit that added the third figure: keep them in one
# edit. Re-MEASURE these two after
# ANY change that can reach the wire, a schema OR a description edit; the split below is why the
# narrower wording was a gap. They are prose, nothing asserts them, and an estimate written
# instead of a measurement had to be
# corrected by a follow-up commit once already (`6c0a2ec`). Sizes of the last three steps: +406/+411
# for discover_pvs, then +775/+775 for find_channels, then +137/+137 for the follow-up that only
# corrected that tool's DESCRIPTION, and that last one is the reason the instruction above says
# "ANY change that can reach the wire": the schema alone would have been ~+410, the rest is
# description text, and description bytes ride the same wire as schema bytes. Measured twice here
# because the follow-up's first commit message claimed "unchanged" without measuring.
# Raising the ceiling is a conscious, CHANGELOG-documented one-line change, never a silent bump.
_TOOLS_LIST_WIRE_CEILING = 200_000


@pytest.mark.asyncio
async def test_tools_list_within_budget() -> None:
    """Size-gate: the wire tools/list payload must stay within the agreed ceiling. Standalone
    FastMCP's native-lean schemas plus the S29 typing keep the core lane 64_719 and the full lane
    75_853 (re-measured 2026-08-08 on both lanes, four times, for four edits to display-gated
    tools: GB-28 made the ``file_path`` echo a file-mode field of ``validate_pvs`` and said so in
    three of its argument descriptions, 74_899 -> 75_176 (+277 full, +0 core); GB-29 gave the
    glob cap its own sentence in the view description, 75_176 -> 75_434 (+258 full, +0 core);
    the post-build review corrected both of those plus ``find_device``'s completeness claim,
    75_434 -> 75_699 (+265 full, +0 core); and GB-65 replaced that tool's "does not report the
    walk caps" with what it now reports, 75_699 -> 75_853 (+154 full, +0 core). Every
    char of all four is description text. The four zeros are the informative half: unlike
    the GB-6 edit below, these touch display-gated tools only, so the core lane cannot move,
    and a nonzero core delta would have meant the edit landed somewhere it was not aimed.
    The previous figures were core 64_719 and full 74_899, re-measured 2026-08-06 on both lanes,
    when GB-6 pointed ``is_alarm_configured`` and
    ``coverage_audit`` at the config-tree discovery recipe: +232 core and +391 full, every char of
    it description text, which is exactly the case the instruction above calls out as reaching the
    wire without touching a schema. Unlike the GB-4 edit before it, that one moved BOTH lanes,
    because ``is_alarm_configured`` is a core tool while ``coverage_audit`` is display-gated.
    NOTE, and this is why the sentence is re-measured rather than trusted: both figures had
    ALREADY drifted before this edit. The pre-edit tree measured 64_487 and 74_508 against a prose
    still claiming 63_017 and 72_508 from 2026-08-02, so about 1_500 and 2_000 chars had
    accumulated here unrecorded. A ``<=`` ceiling cannot notice that by construction); a new
    tool or an SDK change that
    inflates the wire could grow that UNNOTICED with an otherwise green suite. This is that guard,
    now a soft catastrophe-ceiling at 200_000 (see the constant's comment for the raise rationale).

    What this guard does NOT catch, stated so nobody relies on it: a LOST
    ``@mcp.tool(output_schema=None)`` opt-out. Dropping it on a ``dict[str, object]`` tool restores
    an information-empty accept-all schema (``{additionalProperties: true, type: object}``), not a
    typed one, and doing that to all untyped tools grows the wire by 671 chars, measured over the
    ELEVEN that existed on 2026-07-25 (ten today, the figure deliberately not re-scaled by hand):
    0.3 % of this ceiling. The guard for that is
    :func:`test_output_schema_typed_only_for_typed_tools`, which asserts the iff in both directions.

    RELATIONAL (a ``<=`` ceiling, not an exact count) so both the core-only lane (28 tools) and the
    full lane (32) pass, a count-pinned assert would break core-only CI. Provably red: lower the
    ceiling below the current full-lane payload."""
    from mcp.types import ListToolsResult

    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    wire = len(ListToolsResult(tools=tools).model_dump_json(by_alias=True, exclude_none=True))
    assert wire <= _TOOLS_LIST_WIRE_CEILING, (
        f"tools/list wire payload grew to {wire} chars (> {_TOOLS_LIST_WIRE_CEILING} ceiling), "
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

    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
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
    # Key presence rather than whole-dict: standalone FastMCP also appends "fastmcp":{"tags":[]}
    # to _meta (see the _CONSENT_META comment above; other anthropic/* keys are allowed anyway).
    assert '"anthropic/requiresUserInteraction":true' in wire


@pytest.mark.asyncio
async def test_consent_meta_tools_document_the_client_scope() -> None:
    """A tool carrying the consent _meta MUST document the key in its description.

    Honesty drift-guard, encoded as a test (Tier 1) rather than a prose convention (which rots):
    the _meta hint is client- and version-conditional and fails OPEN for a non-honouring client,
    so the point-of-need docstring must name it. Stable marker = the key name itself (it does not
    drift like a version number). Honest limit: it proves a caveat EXISTS, not that it is good.

    Provably red: remove ``requiresUserInteraction`` from set_pv_value's docstring -> goes red.
    """
    from epics_mcp.server import mcp

    undocumented = [
        t.name
        for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]
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
# a hint matches the tool's true behaviour, that stays a review). Self-contained: no plan ref.

# The four boolean hint fields, in the fixed order used by the golden map below.
_ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# Golden map {tool_name: (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)}, measured
# 2026-07-23 from the ToolAnnotations in server.py + display_tools.py, a human-reviewed snapshot
# of every tool's client-facing safety labels. Any live annotation change, or a new/renamed tool,
# turns test_tool_annotations_match_golden_map RED until this map is CONSCIOUSLY updated: that edit
# is the review checkpoint. Covers all 32 full-lane tools; the 4 display-extra tools are absent in
# the core-only lane (tolerated, see _annotation_drift). Regenerate by re-measuring, not by guess.
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
# collection there, the same reason test_guide_matches_code.py AST-scans it.
_DISPLAY_TOOLS_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "epics_mcp" / "display_tools.py"
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

    Returns ``(unclassified, unexpected_missing)``, both must be empty for Guard C to pass:
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
    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    assert len(tools) >= 28, "list_tools() returned < core-lane count, tool registration broke"
    offenders = [
        t.name
        for t in tools
        if t.annotations is None
        or any(getattr(t.annotations, hint) is None for hint in _ANNOTATION_HINTS)
    ]
    assert not offenders, (
        f"tool(s) with missing/incomplete annotations: {offenders}, every tool must set all four "
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
    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    assert len(tools) >= 28, "list_tools() returned < core-lane count, tool registration broke"
    offenders = [
        t.name
        for t in tools
        if t.annotations
        and t.annotations.destructiveHint is True
        and t.annotations.readOnlyHint is not False
    ]
    assert not offenders, (
        f"destructive tool(s) also marked read-only (contradictory): {offenders}, a tool with "
        "destructiveHint=True must have readOnlyHint=False."
    )


@pytest.mark.asyncio
async def test_tool_annotations_match_golden_map() -> None:
    """Every tool's four annotation hints MUST match the frozen golden map, the drift guard.

    The load-bearing guard: the ONLY one that catches a silent change of an EXISTING tool's
    annotations, e.g. flipping set_pv_value to destructive=False/read-only, which Guards A and
    B AND the MA-Q2 consent guards K1/K2 all pass (A: annotations still present; B: vacuous once not
    destructive; K1: drops out of the destructive net, consent _meta untouched; K2: consent _meta
    still documented). A new/renamed tool or any hint change goes red until _ANNOTATION_GOLDEN is
    consciously updated.

    Two-lane robust (core-only CI = 28 tools, full = 32) via _annotation_drift: a golden tool absent
    live is tolerated ONLY if it is display-extra. Honest limit: STRUCTURE + DRIFT, not SEMANTICS.

    Provably red: change a golden tuple, flip a live annotation, or rename a tool.
    """
    from epics_mcp.server import mcp

    tools = [_t.to_mcp_tool() for _t in await mcp.list_tools()]
    assert len(tools) >= 28, "list_tools() returned < core-lane count, tool registration broke"

    golden_names = set(_ANNOTATION_GOLDEN)
    unclassified, unexpected_missing = _annotation_drift(
        {t.name for t in tools}, golden_names, _display_tool_names()
    )
    assert not unclassified, (
        f"tool(s) not in the annotation golden map: {sorted(unclassified)}, classify their safety "
        "hints in _ANNOTATION_GOLDEN (the review checkpoint for a new tool's client-facing labels)."
    )
    assert not unexpected_missing, (
        f"golden tool(s) missing live and not display-extra: {sorted(unexpected_missing)}, a "
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
        f"tool annotation(s) drifted from the golden map (name: live vs golden): {detail}, update "
        "_ANNOTATION_GOLDEN only after a conscious review of the client-facing hint."
    )
