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


@pytest.mark.asyncio
async def test_cli_and_tool_render_identical_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C2-iii parity: the CLI and the MCP tool drive the SAME orchestrator (run_crossplane), so
    against one fixture they render byte-identical Markdown — the duplicated join is gone (M2)."""
    displays, st_cmd = _setup(tmp_path)
    tool_result = await _crossplane_check(str(displays), str(st_cmd))
    assert isinstance(tool_result["markdown"], str)
    rc = main(["--displays", str(displays), "--st-cmd", str(st_cmd)])
    cli_out = capsys.readouterr().out
    assert rc == 0
    assert cli_out == tool_result["markdown"] + "\n"


def test_cli_crossplane_rejects_missing_displays(tmp_path: Path) -> None:
    """A non-existent displays directory exits 2 with an error on stderr (no join)."""
    _, st_cmd = _setup(tmp_path)
    rc = main(["--displays", str(tmp_path / "nope"), "--st-cmd", str(st_cmd)])
    assert rc == 2


def test_cli_crossplane_rejects_displays_dir_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4-4 CLI boundary lock: the CLI must honor EPICS_MCP_ALLOWED_ROOTS (via resolve_user_path in
    the shared orchestrator), not just check ``is_dir()``. An EXISTING displays_dir outside the
    allowed root exits 2. Distinct from the missing-dir test (which the old bare Path.is_dir() code
    also returned 2 for) — here the dir exists, so only the boundary check produces the exit 2."""
    import epics_pv_mcp.config as config_module

    displays, st_cmd = _setup(tmp_path)  # both exist, but outside the allowed root
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(allowed))
    config_module._config = None
    try:
        rc = main(["--displays", str(displays), "--st-cmd", str(st_cmd)])
        assert rc == 2
    finally:
        config_module._config = None


def test_cli_crossplane_channelfinder_without_url_notes_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI --channelfinder threads through (build-once parity); with no URL it prints the honest
    'skipped' note (offline, no network call)."""
    import epics_pv_mcp.config as config_module

    displays, st_cmd = _setup(tmp_path)
    monkeypatch.delenv("EPICS_MCP_CHANNELFINDER_URL", raising=False)
    config_module._config = None
    try:
        rc = main(["--displays", str(displays), "--st-cmd", str(st_cmd), "--channelfinder"])
    finally:
        config_module._config = None
    out = capsys.readouterr().out
    assert rc == 0
    assert "EPICS_MCP_CHANNELFINDER_URL is unset" in out


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
    # Fragment PV resolved concretely and prefix-sharing: linked, attributed to the OPERATOR parent.
    assert "DEV-TEST01:Ctrl-EVR-01:Cmd" in report["pvs_linked"]
    assert report["displays_linked"] == ["operator.bob"]  # NOT frag.bob (fragment seed filtered)


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
async def test_crossplane_tool_st_cmd_honors_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: the boundary covers st_cmd_path too (displays_dir inside, st_cmd outside)."""
    import epics_pv_mcp.config as config_module

    proj = tmp_path / "proj"
    displays = proj / "displays"
    displays.mkdir(parents=True)
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    st_cmd = tmp_path / "st.cmd"  # OUTSIDE proj (the allowed root)
    st_cmd.write_text(_ST_CMD, encoding="utf-8")
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(proj))
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
    from epics_pv_mcp.display_tools import crossplane_check

    _, st_cmd = _setup(tmp_path)
    with pytest.raises(ToolError, match="INVALID_INPUT"):
        await crossplane_check(str(tmp_path / "nope"), str(st_cmd))


@pytest.mark.asyncio
async def test_crossplane_tool_query_channelfinder_without_url_emits_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: query_channelfinder=True but EPICS_MCP_CHANNELFINDER_URL unset → honest 'skipped' note,
    cf_unregistered empty, and NO network call (no checker built)."""
    import epics_pv_mcp.config as config_module

    displays, st_cmd = _setup(tmp_path)
    monkeypatch.delenv("EPICS_MCP_CHANNELFINDER_URL", raising=False)
    config_module._config = None
    try:
        result = await _crossplane_check(str(displays), str(st_cmd), query_channelfinder=True)
    finally:
        config_module._config = None
    report = result["report"]
    assert isinstance(report, dict)
    assert report["cf_unregistered"] == []
    assert any("EPICS_MCP_CHANNELFINDER_URL is unset" in note for note in report["notes"])


@pytest.mark.asyncio
async def test_crossplane_tool_query_channelfinder_computes_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """query_channelfinder=True with a configured URL builds the CF checker and computes
    cf_unregistered end-to-end. ChannelFinderClient is stubbed (no network): it registers only
    ...:status, so the linked ...:Cmd (from $(P)Cmd) is cf_unregistered."""
    import epics_pv_mcp.config as config_module
    import epics_pv_mcp.services.checkers as checkers_module

    displays, st_cmd = _setup(tmp_path)

    class _StubClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(self, name_pattern: str, max_results: int = 500) -> list[dict[str, str]]:
            return [{"name": "DEV-TEST01:Ctrl-EVR-01:status"}]

    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://stub:8080/ChannelFinder")
    config_module._config = None
    monkeypatch.setattr(checkers_module, "ChannelFinderClient", _StubClient)
    try:
        result = await _crossplane_check(str(displays), str(st_cmd), query_channelfinder=True)
    finally:
        config_module._config = None
    report = result["report"]
    assert isinstance(report, dict)
    assert report["cf_unregistered"] == ["DEV-TEST01:Ctrl-EVR-01:Cmd"]  # $(P)Cmd not registered
    assert "DEV-TEST01:Ctrl-EVR-01:status" not in report["cf_unregistered"]
    assert report["cf_registered"] == 1


def test_build_cf_checker_passes_configured_max_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W2-Schritt-1: the CF cap is plumbed config → build_cf_checker → checker.

    Without the env override the default stays 500 (site-safe); the sandbox raises it to 2000 so a
    large device prefix (the full mTCA-EVR-300 set, ~576 channels) does not trip CFRegistryCapped.
    """
    import epics_pv_mcp.config as config_module
    from epics_pv_mcp.services.checkers import CFRegistryChecker, build_cf_checker

    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://stub:8080/ChannelFinder")

    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_MAX_RESULTS", "2000")
    config_module._config = None
    try:
        checker = build_cf_checker(True)
    finally:
        config_module._config = None
    # isinstance narrows ChannelFinderChecker | None → CFRegistryChecker for mypy --strict
    # (the Protocol declares no _max_results attribute).
    assert isinstance(checker, CFRegistryChecker)
    assert checker._max_results == 2000

    monkeypatch.delenv("EPICS_MCP_CHANNELFINDER_MAX_RESULTS", raising=False)
    config_module._config = None
    try:
        default_checker = build_cf_checker(True)
    finally:
        config_module._config = None
    assert isinstance(default_checker, CFRegistryChecker)
    assert default_checker._max_results == 500  # default stays site-safe


@pytest.mark.asyncio
async def test_crossplane_cap_override_changes_withhold_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configurable cap actually changes behaviour: a stub CF that would return 600 channels is
    truncated to *max_results*. cap=2000 ⇒ 600 < cap ⇒ NOT withheld (cf_unregistered computed);
    cap=500 (default) ⇒ 500 >= cap ⇒ CFRegistryCapped ⇒ withheld (cf_capped, cf_unregistered empty).
    """
    import epics_pv_mcp.config as config_module
    import epics_pv_mcp.services.checkers as checkers_module

    displays, st_cmd = _setup(tmp_path)

    class _Stub600Client:
        """Emulates ChannelFinder's ~size truncation: never returns more than the cap."""

        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(self, name_pattern: str, max_results: int = 500) -> list[dict[str, str]]:
            return [{"name": f"DEV-TEST01:Ctrl-EVR-01:ch{i}"} for i in range(min(600, max_results))]

    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://stub:8080/ChannelFinder")
    monkeypatch.setattr(checkers_module, "ChannelFinderClient", _Stub600Client)

    # cap=2000: 600 channels fit under the cap → not withheld.
    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_MAX_RESULTS", "2000")
    config_module._config = None
    try:
        big = (await _crossplane_check(str(displays), str(st_cmd), query_channelfinder=True))[
            "report"
        ]
    finally:
        config_module._config = None
    assert isinstance(big, dict)
    assert big["cf_capped"] is False
    assert big["cf_registered"] == 600
    assert big["cf_unregistered"]  # linked PVs not among the 600 → genuinely computed

    # cap=500 (default, env removed): the same 600-channel CF truncates to 500 == cap → withheld.
    monkeypatch.delenv("EPICS_MCP_CHANNELFINDER_MAX_RESULTS", raising=False)
    config_module._config = None
    try:
        capped = (await _crossplane_check(str(displays), str(st_cmd), query_channelfinder=True))[
            "report"
        ]
    finally:
        config_module._config = None
    assert isinstance(capped, dict)
    assert capped["cf_capped"] is True
    assert capped["cf_unregistered"] == []
