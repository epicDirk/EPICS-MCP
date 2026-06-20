"""Tests for the crossplane MCP tool wrapper (offline; no network, no running IOC)."""

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.tools.crossplane import _crossplane_check

# One display with a concrete prefixed PV and one macro-bearing PV (kept by the extractor
# because "$(P)Cmd" is not a pure-macro reference), so the join yields linked + indeterminate.
_BOB = (
    '<display version="2.0.0">'
    '<widget type="textupdate"><pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>'
    '<widget type="textupdate"><pv_name>$(P)Cmd</pv_name></widget>'
    "</display>"
)
# The prefix is derived from a dbLoadRecords P= macro (not from epicsEnvSet alone).
_ST_CMD = 'epicsEnvSet("P", "DEV-TEST01:Ctrl-EVR-01:")\ndbLoadRecords("evr.db", "P=$(P)")\n'


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Write a one-display directory and an st.cmd under *tmp_path*; return both paths."""
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    st_cmd = tmp_path / "st.cmd"
    st_cmd.write_text(_ST_CMD, encoding="utf-8")
    return displays, st_cmd


@pytest.mark.asyncio
async def test_crossplane_tool_offline_join(tmp_path: Path) -> None:
    """The tool joins display PVs with the IOC prefix and renders Markdown, offline."""
    displays, st_cmd = _setup(tmp_path)
    result = await _crossplane_check(str(displays), str(st_cmd), query_naming=False)

    report = result["report"]
    assert isinstance(report, dict)
    assert report["ioc_prefix"] == "DEV-TEST01:Ctrl-EVR-01:"
    assert "DEV-TEST01:Ctrl-EVR-01:status" in report["pvs_linked"]
    assert report["pvs_indeterminate"] == 1
    assert report["naming"] is None  # query_naming=False → never touches the network

    markdown = result["markdown"]
    assert isinstance(markdown, str)
    assert "Cross-Plane PV Provenance" in markdown


@pytest.mark.asyncio
async def test_crossplane_tool_rejects_bad_displays_dir(tmp_path: Path) -> None:
    """A non-existent displays directory is rejected before any work."""
    _, st_cmd = _setup(tmp_path)
    with pytest.raises(EpicsError) as exc:
        await _crossplane_check(str(tmp_path / "nope"), str(st_cmd))
    assert exc.value.error_code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_crossplane_tool_rejects_bad_st_cmd(tmp_path: Path) -> None:
    """A non-existent st.cmd file is rejected before any work."""
    displays, _ = _setup(tmp_path)
    with pytest.raises(EpicsError) as exc:
        await _crossplane_check(str(displays), str(tmp_path / "nope.cmd"))
    assert exc.value.error_code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_server_crossplane_converts_error_to_tool_error(tmp_path: Path) -> None:
    """The server wrapper maps EpicsError to ToolError with the error_code tag."""
    from epics_pv_mcp.server import crossplane_check

    _, st_cmd = _setup(tmp_path)
    with pytest.raises(ToolError, match="INVALID_INPUT"):
        await crossplane_check(str(tmp_path / "nope"), str(st_cmd))
