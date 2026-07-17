"""Tests for server-level tool wrappers (EpicsError → ToolError conversion)."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from epics_pv_mcp.errors import PVNotFoundError, PVTimeoutError


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
