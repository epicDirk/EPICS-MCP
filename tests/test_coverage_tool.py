"""End-to-end tests for the coverage tool + CLI (offline; real .bob via analyze_display_index)."""

from __future__ import annotations

from pathlib import Path

import pytest

from epics_mcp.cli_coverage import main
from epics_mcp.errors import EpicsError
from epics_mcp.tools.coverage_audit import _coverage_audit

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
    import epics_mcp.config as config_module

    displays = _setup(tmp_path)
    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://cf")
    monkeypatch.setattr("epics_mcp.services.checkers.ChannelFinderClient", _StubCFClient)
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
    so against one fixture they render identical Markdown, the duplicated join is gone (M2)."""
    displays = _setup(tmp_path)
    scope = "DEV-TEST01:Ctrl-EVR-01:"
    tool_result = await _coverage_audit(str(displays), scope=scope)
    assert isinstance(tool_result["markdown"], str)
    rc = main(["--displays", str(displays), "--scope", scope])
    cli_out = capsys.readouterr().out
    assert rc == 0
    assert cli_out == tool_result["markdown"] + "\n"


async def test_coverage_passes_canonical_displays_dir_to_walker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5-7 lock: the orchestrator must hand the inventory walker the RESOLVED (canonical)
    displays_dir, not the raw string. A non-canonical path (with a ``..`` segment) must reach
    analyze_display_index as ``Path(raw).resolve()``. Against the pre-fix code (raw Path walked) the
    ``..`` would survive and this assertion fails."""
    from unittest.mock import patch

    displays = _setup(tmp_path)  # tmp_path/displays exists
    (tmp_path / "sub").mkdir()
    raw = str(tmp_path / "sub" / ".." / "displays")  # non-canonical → resolves to tmp_path/displays
    assert Path(raw) != Path(raw).resolve()  # guard: the fixture is genuinely non-canonical
    with patch(
        "epics_mcp.services.orchestration.analyze_display_index", return_value=([], (), 0, 0)
    ) as walker:
        await _coverage_audit(raw)
    assert walker.call_args.args[0] == Path(raw).resolve()
    assert displays == Path(raw).resolve()


async def test_coverage_refuses_a_missing_alarm_tree_without_running_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-33's defect class, in the sibling tool: refuse first, then do the expensive work.

    ``build_alarm_checker`` rejects "alarm plane requested, no tree named" outright. Constructed
    AFTER the display-PV walk, that refusal arrived only once the walk had spent the better part
    of a minute on a large dataset, for a verdict the arguments alone decide. Asserted on the
    cause (the walker was never called), not on elapsed time, so it cannot pass by getting faster.
    """
    from unittest.mock import Mock, patch

    displays = _setup(tmp_path)
    monkeypatch.setenv("EPICS_MCP_ALARM_URL", "http://alarm.invalid:8081")
    import epics_mcp.config as config_module

    config_module._config = None
    spy = Mock(side_effect=AssertionError("the display walk must not run before the refusal"))
    try:
        with (
            patch("epics_mcp.services.orchestration.analyze_display_index", spy),
            pytest.raises(EpicsError) as exc_info,
        ):
            await _coverage_audit(str(displays), query_alarm=True)
        assert exc_info.value.error_code == "INVALID_INPUT"
        assert "alarm" in str(exc_info.value).lower()
        spy.assert_not_called()
    finally:
        config_module._config = None


def test_cli_coverage_rejects_missing_dir(tmp_path: Path) -> None:
    """A non-existent displays directory exits 2 with an error on stderr (no join)."""
    rc = main(["--displays", str(tmp_path / "nope")])
    assert rc == 2


def test_cli_coverage_rejects_displays_dir_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4-4 CLI boundary lock: the CLI must honor EPICS_MCP_ALLOWED_ROOTS (via resolve_user_path in
    the shared orchestrator). An EXISTING displays_dir outside the allowed root exits 2, the case
    the pre-remediation bare Path.is_dir() short-circuit would have let through (it returns 0)."""
    import epics_mcp.config as config_module

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


# --- GQ-153: a PV that lives only on a Data Browser trend is not screen-visible ---------------

# The trend fixture: `panel.bob` is a real operator screen, `beam.plt` is a Data Browser trend that
# a button OPENS (an open_file target is a top level of its own, which is what makes it reachable
# by the operator and therefore part of the index). One channel sits on the screen alone, one on
# the trend alone, one on BOTH. Three cases in one fixture on purpose: an implementation that
# calls every top level a trend satisfies the first assertion and fails the third.
_TREND_PANEL_BOB = (
    '<display version="2.0.0"><name>Panel</name>'
    '<widget type="textupdate"><name>s</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>"
    '<widget type="textupdate"><name>b</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:both</pv_name></widget>"
    "</display>"
)
_TREND_MENU_BOB = (
    '<display version="2.0.0"><name>Menu</name>'
    '<widget type="action_button" version="3.0.0"><name>b</name><actions>'
    '<action type="open_file"><file>beam.plt</file><description>Trend</description></action>'
    "</actions></widget></display>"
)
_TREND_PLT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    "<databrowser><title>Beam</title><pvlist>"
    "<pv><name>DEV-TEST01:Ctrl-EVR-01:trendonly</name><visible>true</visible><axis>0</axis></pv>"
    "<pv><name>DEV-TEST01:Ctrl-EVR-01:both</name><visible>true</visible><axis>0</axis></pv>"
    "</pvlist></databrowser>"
)


def _setup_with_trend(tmp_path: Path) -> Path:
    """Write a project root holding a screen, a menu that opens a trend, and that trend."""
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_TREND_PANEL_BOB, encoding="utf-8")
    (displays / "menu.bob").write_text(_TREND_MENU_BOB, encoding="utf-8")
    (displays / "beam.plt").write_text(_TREND_PLT, encoding="utf-8")
    return displays


class _StubCFClientWithTrend:
    """Registers the screen PV, the trend-only PV, the shared one, and one shown nowhere."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def find_channels(self, pattern: str, max_results: int = 500) -> list[dict[str, str]]:
        return [
            {"name": "DEV-TEST01:Ctrl-EVR-01:status"},
            {"name": "DEV-TEST01:Ctrl-EVR-01:both"},
            {"name": "DEV-TEST01:Ctrl-EVR-01:trendonly"},
            {"name": "DEV-TEST01:Ctrl-EVR-01:extra"},
        ]


@pytest.mark.asyncio
async def test_the_fixture_really_carries_a_screen_a_trend_and_a_shared_channel(
    tmp_path: Path,
) -> None:
    """Guard on the FIXTURE, before anything is asserted about the verdicts.

    A red proof over a fixture that produces no trend at all is vacuum-red: it goes green the
    moment the field exists and says nothing about the defect. So this asserts the raw material
    first, that all three channels reach the index and that the trend's own channel arrives
    attributed to the ``.plt`` rather than to the menu that opens it.

    ⚠ GQ-21 did not pay for this, it AVOIDED it, and the difference is worth the correction: an
    earlier draft here claimed the scar. What that item actually carries is a non-vacuity assertion
    of its own (``tests/test_find_device_tool.py``, "Not a vacuous agreement: this fixture really
    does hold one of each"), which is the same guard shaped for a different report. Borrowing an
    incident nobody had is the class of claim this repository's evidence discipline rejects.
    """
    from epics_mcp.services.inventory_adapter import analyze_display_index

    rows, _capped, _glob, walked = analyze_display_index(_setup_with_trend(tmp_path))
    by_pv = {row.pv: row for row in rows}
    assert walked >= 3, "the walk must have visited the two screens and the trend"
    assert by_pv["DEV-TEST01:Ctrl-EVR-01:status"].displays == ("panel.bob",)
    assert by_pv["DEV-TEST01:Ctrl-EVR-01:trendonly"].displays == ("beam.plt",)
    assert set(by_pv["DEV-TEST01:Ctrl-EVR-01:both"].displays) == {"beam.plt", "panel.bob"}


@pytest.mark.asyncio
async def test_a_trend_only_pv_is_not_reported_as_screen_visible(tmp_path: Path) -> None:
    """GQ-153, both directions: the trend-only channel is NOT on a screen, the screen one IS.

    Driven through the TOOL rather than through the engine underneath, and that is the whole
    point: the engine has carried ``node_kind`` since GB-12, so a test one layer down stays green
    through exactly the defect it is meant to pin. What was broken is this server's projection.

    The shared channel is the second direction. It sits on a screen AND on a trend, so it must
    keep ``has_display=yes`` and must not appear in ``trend_only``; an implementation that treats
    "appears on any trend" as "trend-only" passes the first assertion and fails here.
    """
    result = await _coverage_audit(
        str(_setup_with_trend(tmp_path)), scope="DEV-TEST01:Ctrl-EVR-01:"
    )
    report = result["report"]
    assert isinstance(report, dict)
    cells = {row["pv"]: row for row in report["rows"]}

    assert cells["DEV-TEST01:Ctrl-EVR-01:status"]["has_display"] == "yes"
    assert cells["DEV-TEST01:Ctrl-EVR-01:both"]["has_display"] == "yes"
    assert cells["DEV-TEST01:Ctrl-EVR-01:trendonly"]["has_display"] == "no"

    assert cells["DEV-TEST01:Ctrl-EVR-01:status"]["on_trend"] == "no"
    assert cells["DEV-TEST01:Ctrl-EVR-01:both"]["on_trend"] == "yes"
    assert cells["DEV-TEST01:Ctrl-EVR-01:trendonly"]["on_trend"] == "yes"

    assert report["trend_only"] == ["DEV-TEST01:Ctrl-EVR-01:trendonly"]


@pytest.mark.asyncio
async def test_a_trend_only_pv_lands_in_the_cf_only_blind_spot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline: ``cf_only`` is "registered but on NO screen", and it used to lose these.

    A channel delivered by the IOC that an operator can only reach by opening a trend is a blind
    spot, which is the count this tool exists to produce. ``cf_trend_only`` names the subset of
    that blind-spot list that is at least trended, so the two are told apart without the caller
    intersecting two tuples by hand.
    """
    import epics_mcp.config as config_module

    displays = _setup_with_trend(tmp_path)
    monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://cf")
    monkeypatch.setattr("epics_mcp.services.checkers.ChannelFinderClient", _StubCFClientWithTrend)
    config_module._config = None
    try:
        result = await _coverage_audit(
            str(displays), scope="DEV-TEST01:Ctrl-EVR-01:", query_channelfinder=True
        )
    finally:
        config_module._config = None
    report = result["report"]
    assert isinstance(report, dict)

    assert report["cf_and_display"] == [
        "DEV-TEST01:Ctrl-EVR-01:both",
        "DEV-TEST01:Ctrl-EVR-01:status",
    ]
    assert report["cf_only"] == [
        "DEV-TEST01:Ctrl-EVR-01:extra",
        "DEV-TEST01:Ctrl-EVR-01:trendonly",
    ]
    assert report["cf_trend_only"] == ["DEV-TEST01:Ctrl-EVR-01:trendonly"]
    # The delivered, screen-less channel is a proven gap now, so it reaches the red headline.
    assert "DEV-TEST01:Ctrl-EVR-01:trendonly" in report["blind_spots"]
    assert "DEV-TEST01:Ctrl-EVR-01:trendonly" in report["critical_uncovered"]


@pytest.mark.asyncio
async def test_the_rendered_report_names_the_trend_split(tmp_path: Path) -> None:
    """The Markdown half is what a person reads, so the split has to be visible there too.

    GQ-21 made the same call for ``find_device``: a header a reader draws a conclusion from may
    not silently fold a trend into a screen count.
    """
    result = await _coverage_audit(
        str(_setup_with_trend(tmp_path)), scope="DEV-TEST01:Ctrl-EVR-01:"
    )
    markdown = result["markdown"]
    assert isinstance(markdown, str)
    assert "Data Browser trend" in markdown
    assert "DEV-TEST01:Ctrl-EVR-01:trendonly" in markdown
