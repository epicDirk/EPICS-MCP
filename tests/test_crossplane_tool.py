"""End-to-end tests for the crossplane tool + CLI (offline; real .bob via analyze_pv_inventory).

These exercise the WIRED path: a real .bob over the macro-aware ``opi_navigation`` inventory →
JoinPv rows → join → report. The display carries a ``<macros>`` scope so the macro PV ``$(P)Cmd``
RESOLVES to a concrete channel and lands in ``linked`` (not ``indeterminate``) — the Wedge-1 payoff.
NOTE: the display ``<macros>`` (P) is the binding namespace, NOT the st.cmd's P= macro; without the
display ``<macros>`` the PV would stay ``dynamic``.
"""

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from epics_pv_mcp.cli_crossplane import main
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.tools.crossplane import _crossplane_check

# Operator-facing root display: a concrete prefixed PV + a macro PV bound by the display's own
# <macros> scope (so it resolves to DEV-TEST01:Ctrl-EVR-01:Cmd and links to the IOC prefix).
_BOB = (
    '<display version="2.0.0"><name>Panel</name>'
    "<macros><P>DEV-TEST01:Ctrl-EVR-01:</P></macros>"
    '<widget type="textupdate"><name>s</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>"
    '<widget type="textentry"><name>c</name><pv_name>$(P)Cmd</pv_name></widget>'
    "</display>"
)
# The IOC prefix is derived from a dbLoadRecords P= macro (not from epicsEnvSet alone).
_ST_CMD = 'epicsEnvSet("P", "DEV-TEST01:Ctrl-EVR-01:")\ndbLoadRecords("evr.db", "P=$(P)")\n'


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Write a one-display project root and an st.cmd under *tmp_path*; return both paths."""
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    st_cmd = tmp_path / "st.cmd"
    st_cmd.write_text(_ST_CMD, encoding="utf-8")
    return displays, st_cmd


@pytest.mark.asyncio
async def test_crossplane_tool_macro_pv_resolves_to_linked(tmp_path: Path) -> None:
    """The Wedge-1 payoff: the macro PV ``$(P)Cmd`` is now RESOLVED (via the display <macros>) to a
    concrete linked PV — no longer ``indeterminate``. Both display PVs share the IOC prefix.
    """
    displays, st_cmd = _setup(tmp_path)
    result = await _crossplane_check(str(displays), str(st_cmd), query_naming=False)

    report = result["report"]
    assert isinstance(report, dict)
    assert report["ioc_prefix"] == "DEV-TEST01:Ctrl-EVR-01:"
    assert "DEV-TEST01:Ctrl-EVR-01:status" in report["pvs_linked"]
    assert "DEV-TEST01:Ctrl-EVR-01:Cmd" in report["pvs_linked"]  # was macro-templated, now linked
    assert report["pvs_indeterminate"] == []
    assert report["pvs_indeterminate_occurrences"] == 0
    assert report["naming"] is None  # query_naming=False → never touches the network

    markdown = result["markdown"]
    assert isinstance(markdown, str)
    assert "Cross-Plane PV Provenance" in markdown
    assert "**Concrete PVs sharing the prefix:** 2" in markdown


def test_cli_crossplane_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI entry point joins offline and writes the Markdown report to stdout (exit 0)."""
    displays, st_cmd = _setup(tmp_path)
    rc = main(["--displays", str(displays), "--st-cmd", str(st_cmd)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Cross-Plane PV Provenance" in out
    assert "**Concrete PVs sharing the prefix:** 2" in out


def test_cli_crossplane_rejects_missing_displays(tmp_path: Path) -> None:
    """A non-existent displays directory exits 2 with an error on stderr (no join)."""
    _, st_cmd = _setup(tmp_path)
    rc = main(["--displays", str(tmp_path / "nope"), "--st-cmd", str(st_cmd)])
    assert rc == 2


@pytest.mark.asyncio
async def test_crossplane_tool_fragment_not_double_attributed(tmp_path: Path) -> None:
    """QA-High wired regression (operator-facing-Filter end-to-end): die PV eines reinen Embed-only-
    Fragments lifted GENAU EINMAL auf das Operator-Eltern-Display; der Standalone-Seed des Fragments
    (operator_facing=False) wird im Adapter gefiltert → frag.bob erscheint NICHT als eigenes
    linked-Display, keine Doppel-Attribution. Echt durch analyze_pv_inventory → Adapter → Join.
    """
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "operator.bob").write_text(
        '<display version="2.0.0"><name>Op</name>'
        '<widget type="embedded"><name>e</name><file>frag.bob</file></widget></display>',
        encoding="utf-8",
    )
    (displays / "frag.bob").write_text(
        '<display version="2.0.0"><name>Frag</name>'
        "<macros><P>DEV-TEST01:Ctrl-EVR-01:</P></macros>"
        '<widget type="textentry"><name>c</name><pv_name>$(P)Cmd</pv_name></widget></display>',
        encoding="utf-8",
    )
    st_cmd = tmp_path / "st.cmd"
    st_cmd.write_text(_ST_CMD, encoding="utf-8")

    result = await _crossplane_check(str(displays), str(st_cmd))
    report = result["report"]
    assert isinstance(report, dict)
    # Fragment-PV konkret aufgelöst + prefix-teilend → linked, dem OPERATOR-Eltern zugeschrieben.
    assert "DEV-TEST01:Ctrl-EVR-01:Cmd" in report["pvs_linked"]
    assert report["displays_linked"] == ["operator.bob"]  # NICHT frag.bob (Fragment-Seed gefiltert)


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
