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
async def test_crossplane_tool_module_db_root_emits_broken(tmp_path: Path) -> None:
    """End-to-end Phase-2 payoff: with a provably complete IOC .db (only dbLoadRecords, no iocsh),
    the linked PV ``$(P)Cmd`` — absent from the .db — is reported ``broken``; ``status`` (present)
    is not. The IOC .db's $(P) is bound by the st.cmd's per-load macro (P=$(P) → env P).
    """
    displays, st_cmd = _setup(tmp_path)
    module_db = tmp_path / "moddb"
    module_db.mkdir()
    # evr.db serves only ...:status (NOT ...:Cmd) → Cmd is provably broken.
    (module_db / "evr.db").write_text('record(stringin, "$(P)status") {}\n', encoding="utf-8")

    result = await _crossplane_check(str(displays), str(st_cmd), module_db_root=str(module_db))
    report = result["report"]
    assert isinstance(report, dict)
    assert report["broken"] == ["DEV-TEST01:Ctrl-EVR-01:Cmd"]
    assert "DEV-TEST01:Ctrl-EVR-01:status" not in report["broken"]
    # Cmd comes from a textentry widget (write role) → also surfaced through JSON as broken_write.
    assert report["broken_write"] == ["DEV-TEST01:Ctrl-EVR-01:Cmd"]
    assert report["ioc_db_resolved"] == 1


@pytest.mark.asyncio
async def test_crossplane_tool_module_db_root_withholds_broken_when_incomplete(
    tmp_path: Path,
) -> None:
    """With an iocshLoad present the IOC .db cannot be proven complete → ``broken`` withheld (the
    dln01-EVR reality: most records arrive via iocshLoad'ed .iocsh we cannot statically follow)."""
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    # st.cmd that DOES load records via iocshLoad → unsupported → never complete.
    st_cmd = tmp_path / "st.cmd"
    st_cmd.write_text(
        'epicsEnvSet("P", "DEV-TEST01:Ctrl-EVR-01:")\n'
        'iocshLoad("evrEss.iocsh", "P=$(P)")\n'
        'dbLoadRecords("evr.db", "P=$(P)")\n',
        encoding="utf-8",
    )
    module_db = tmp_path / "moddb"
    module_db.mkdir()
    (module_db / "evr.db").write_text('record(stringin, "$(P)status") {}\n', encoding="utf-8")

    result = await _crossplane_check(str(displays), str(st_cmd), module_db_root=str(module_db))
    report = result["report"]
    assert isinstance(report, dict)
    assert report["broken"] == []  # withheld — completeness cannot be proven
    assert any("withheld" in note.lower() for note in report["notes"])


@pytest.mark.asyncio
async def test_crossplane_tool_rejects_bad_module_db_root(tmp_path: Path) -> None:
    """A non-existent module_db_root is rejected before any work."""
    displays, st_cmd = _setup(tmp_path)
    with pytest.raises(EpicsError) as exc:
        await _crossplane_check(str(displays), str(st_cmd), module_db_root=str(tmp_path / "nope"))
    assert exc.value.error_code == "INVALID_INPUT"


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
async def test_crossplane_tool_rejects_displays_dir_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: an existing displays_dir outside the opt-in allowed_roots is rejected."""
    import epics_pv_mcp.config as config_module

    displays, st_cmd = _setup(tmp_path)  # both exist, but outside the allowed root
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(allowed))
    config_module._config = None
    try:
        with pytest.raises(EpicsError) as exc:
            await _crossplane_check(str(displays), str(st_cmd))
        assert exc.value.error_code == "PATH_OUTSIDE_WORKSPACE"
    finally:
        config_module._config = None


@pytest.mark.asyncio
async def test_crossplane_tool_module_db_root_honors_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3-S2: the boundary also covers module_db_root (e3_db os.walk + read_text)."""
    import epics_pv_mcp.config as config_module

    proj = tmp_path / "proj"  # displays + st_cmd live here (inside the allowed root)
    displays = proj / "displays"
    displays.mkdir(parents=True)
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    st_cmd = proj / "st.cmd"
    st_cmd.write_text(_ST_CMD, encoding="utf-8")
    module_db = tmp_path / "moddb"  # sibling of proj → OUTSIDE the allowed root
    module_db.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(proj))
    config_module._config = None
    try:
        with pytest.raises(EpicsError) as exc:
            await _crossplane_check(str(displays), str(st_cmd), module_db_root=str(module_db))
        assert exc.value.error_code == "PATH_OUTSIDE_WORKSPACE"
    finally:
        config_module._config = None


@pytest.mark.asyncio
async def test_crossplane_tool_pva_prefixed_pv_links(tmp_path: Path) -> None:
    """Wedge-1 mini-fix end-to-end (active bug, crossplane.py:162): an explicitly ``pva://``-prefixed
    display PV that shares the IOC prefix is normalized to its channel name at the adapter edge → it
    lands in ``linked``, not ``other_prefix`` (before the fix the raw ``pva://`` broke startswith).
    """
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(
        '<display version="2.0.0"><name>Panel</name>'
        '<widget type="textupdate"><name>s</name>'
        "<pv_name>pva://DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget></display>",
        encoding="utf-8",
    )
    st_cmd = tmp_path / "st.cmd"
    st_cmd.write_text(_ST_CMD, encoding="utf-8")

    result = await _crossplane_check(str(displays), str(st_cmd), query_naming=False)
    report = result["report"]
    assert isinstance(report, dict)
    assert report["pvs_linked"] == ["DEV-TEST01:Ctrl-EVR-01:status"]  # channel form, NOT pva://…
    assert report["pvs_other_prefix"] == []  # before the fix the raw pva:// PV landed here


@pytest.mark.asyncio
async def test_crossplane_tool_pva_prefixed_pv_broken_against_db(tmp_path: Path) -> None:
    """Wedge-1 mini-fix end-to-end (latent trap, crossplane.py:198): the SAME edge normalization
    fixes the broken comparison — a ``pva://``-prefixed linked PV absent from a provably complete
    IOC .db is now correctly ``broken`` (channel-form linked_pvs vs. channel-form .db records, no
    protocol mismatch). Before the fix the pva:// PV never reached ``linked``, so the trap slept.
    """
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(
        '<display version="2.0.0"><name>Panel</name>'
        '<widget type="textupdate"><name>s</name>'
        "<pv_name>pva://DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>"
        '<widget type="textentry"><name>c</name>'
        "<pv_name>pva://DEV-TEST01:Ctrl-EVR-01:Cmd</pv_name></widget></display>",
        encoding="utf-8",
    )
    st_cmd = tmp_path / "st.cmd"
    st_cmd.write_text(_ST_CMD, encoding="utf-8")
    module_db = tmp_path / "moddb"
    module_db.mkdir()
    # evr.db serves only ...:status (NOT ...:Cmd) → Cmd is provably broken once it is linked.
    (module_db / "evr.db").write_text('record(stringin, "$(P)status") {}\n', encoding="utf-8")

    result = await _crossplane_check(str(displays), str(st_cmd), module_db_root=str(module_db))
    report = result["report"]
    assert isinstance(report, dict)
    assert report["broken"] == ["DEV-TEST01:Ctrl-EVR-01:Cmd"]  # channel-form match against the .db
    assert "DEV-TEST01:Ctrl-EVR-01:status" not in report["broken"]
    assert report["broken_write"] == ["DEV-TEST01:Ctrl-EVR-01:Cmd"]  # textentry = write role


@pytest.mark.asyncio
async def test_server_crossplane_converts_error_to_tool_error(tmp_path: Path) -> None:
    """The server wrapper maps EpicsError to ToolError with the error_code tag."""
    from epics_pv_mcp.server import crossplane_check

    _, st_cmd = _setup(tmp_path)
    with pytest.raises(ToolError, match="INVALID_INPUT"):
        await crossplane_check(str(tmp_path / "nope"), str(st_cmd))
