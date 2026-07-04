"""End-to-end tests for the coverage tool + CLI (offline; real .bob via analyze_display_index)."""

from __future__ import annotations

from pathlib import Path

import pytest

from epics_pv_mcp.cli_coverage import main
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.tools.coverage_audit import _coverage_audit

# Operator-facing display: a concrete prefixed PV + a macro PV bound by the display's own <macros>
# scope (so it resolves to DEV-TEST01:Ctrl-EVR-01:Cmd) → the index carries both.
_BOB = (
    '<display version="2.0.0"><name>Panel</name>'
    "<macros><P>DEV-TEST01:Ctrl-EVR-01:</P></macros>"
    '<widget type="textupdate"><name>s</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>"
    '<widget type="textentry"><name>c</name><pv_name>$(P)Cmd</pv_name></widget>'
    "</display>"
)


def _setup(tmp_path: Path) -> Path:
    """Write a one-display project root under *tmp_path*; return the displays dir."""
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    return displays


class _StubCFClient:
    """Stub of ChannelFinderClient: registers one of the two display PVs plus one extra."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def find_channels(self, pattern: str, max_results: int = 500) -> list[dict[str, str]]:
        return [
            {"name": "DEV-TEST01:Ctrl-EVR-01:status"},
            {"name": "DEV-TEST01:Ctrl-EVR-01:extra"},
        ]


@pytest.mark.asyncio
async def test_coverage_tool_display_set_from_bob(tmp_path: Path) -> None:
    """The wired path: real .bob → analyze_display_index → audit. CF disabled → only the raw D set,
    every cf verdict withheld (CF is the anchor)."""
    displays = _setup(tmp_path)
    result = await _coverage_audit(str(displays), scope="DEV-TEST01:Ctrl-EVR-01:")
    report = result["report"]
    assert isinstance(report, dict)
    pvs = {row["pv"] for row in report["rows"]}
    assert "DEV-TEST01:Ctrl-EVR-01:status" in pvs
    assert "DEV-TEST01:Ctrl-EVR-01:Cmd" in pvs  # macro PV resolved into the index
    assert all(row["registered_cf"] == "withheld" for row in report["rows"])
    assert "channelfinder" not in report["planes_live"]


@pytest.mark.asyncio
async def test_coverage_tool_cf_stubbed_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ChannelFinder wired (stubbed), the cross-matrix computes: status is cf_and_display,
    the registered-but-unshown 'extra' is a cf_only blind-spot, the shown-but-unregistered 'Cmd'
    is display_only."""
    import epics_pv_mcp.config as config_module

    displays = _setup(tmp_path)
    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://cf")
    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _StubCFClient)
    config_module._config = None
    try:
        result = await _coverage_audit(
            str(displays), scope="DEV-TEST01:Ctrl-EVR-01:", query_channelfinder=True
        )
    finally:
        config_module._config = None
    report = result["report"]
    assert isinstance(report, dict)
    assert "channelfinder" in report["planes_live"]
    assert report["cf_and_display"] == ["DEV-TEST01:Ctrl-EVR-01:status"]
    assert report["cf_only"] == ["DEV-TEST01:Ctrl-EVR-01:extra"]  # registered, no screen
    assert report["display_only"] == ["DEV-TEST01:Ctrl-EVR-01:Cmd"]  # shown, not registered
    assert isinstance(result["markdown"], str)
    assert "Cross-Plane Coverage Audit" in result["markdown"]


@pytest.mark.asyncio
async def test_coverage_tool_rejects_missing_dir(tmp_path: Path) -> None:
    """A non-existent displays directory raises (path-safety rejection before any walk)."""
    with pytest.raises(EpicsError):
        await _coverage_audit(str(tmp_path / "nope"))


def test_cli_coverage_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI joins offline and writes the Markdown report to stdout (exit 0)."""
    displays = _setup(tmp_path)
    rc = main(["--displays", str(displays), "--scope", "DEV-TEST01:Ctrl-EVR-01:"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Cross-Plane Coverage Audit" in out


@pytest.mark.asyncio
async def test_cli_and_tool_render_identical_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C2-iii parity: the CLI and the MCP tool drive the SAME orchestrator (build_coverage_report),
    so against one fixture they render identical Markdown — the duplicated join is gone (M2)."""
    displays = _setup(tmp_path)
    scope = "DEV-TEST01:Ctrl-EVR-01:"
    tool_result = await _coverage_audit(str(displays), scope=scope)
    assert isinstance(tool_result["markdown"], str)
    rc = main(["--displays", str(displays), "--scope", scope])
    cli_out = capsys.readouterr().out
    assert rc == 0
    assert cli_out == tool_result["markdown"] + "\n"


def test_cli_coverage_rejects_missing_dir(tmp_path: Path) -> None:
    """A non-existent displays directory exits 2 with an error on stderr (no join)."""
    rc = main(["--displays", str(tmp_path / "nope")])
    assert rc == 2


def test_cli_coverage_rejects_displays_dir_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4-4 CLI boundary lock: the CLI must honor EPICS_MCP_ALLOWED_ROOTS (via resolve_user_path in
    the shared orchestrator). An EXISTING displays_dir outside the allowed root exits 2 — the case
    the pre-remediation bare Path.is_dir() short-circuit would have let through (it returns 0)."""
    import epics_pv_mcp.config as config_module

    displays = _setup(tmp_path)  # exists, but outside the allowed root
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(allowed))
    config_module._config = None
    try:
        rc = main(["--displays", str(displays)])
        assert rc == 2
    finally:
        config_module._config = None
