"""End-to-end tests for the find_device tool (real .bob via analyze_pv_inventory; p4p read mocked).

These exercise the WIRED path: a real operator .bob over the macro-aware ``opi_navigation``
inventory → ``find_displays`` → channel collection → live read → report. The p4p batch read is
mocked AT THE find_device IMPORT SITE (``epics_mcp.tools.find_device.pv_get_batch``) with a
hand-built ``{results, errors}``, no real IOC, no shared p4p fake. ChannelFinder is disabled by
default (no URL), so source IOC is honestly absent. The pure merge is in ``test_device_lookup.py``.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from opi_navigation.pv_analysis import PvDiagnostics, PvInventory, analyze_pv_inventory

from epics_mcp.errors import EpicsConnectionError, EpicsError
from epics_mcp.tools.find_device import _find_device

# Operator-facing root display: one read channel (pva://-prefixed) + one write channel (bare), both
# sharing the device prefix the query targets.
_BOB = (
    '<display version="2.0.0"><name>DLN01</name>'
    '<widget type="textupdate"><name>s</name>'
    "<pv_name>pva://DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>"
    '<widget type="textentry"><name>c</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:Cmd</pv_name></widget>"
    "</display>"
)
_STATUS = "DEV-TEST01:Ctrl-EVR-01:status"
_CMD = "DEV-TEST01:Ctrl-EVR-01:Cmd"


def _displays(tmp_path: Path) -> Path:
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    return displays


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_screens_live_and_disabled_cf(
    mock_batch: AsyncMock, tmp_path: Path
) -> None:
    """The wired payoff: the device's screen is found, the channel_name-stripped channels are read
    live (one connected, one disconnected); ChannelFinder disabled yields an honest note."""
    mock_batch.return_value = {
        "results": [{"pv_name": _STATUS, "value": 1, "alarm": {"severity_text": "MINOR"}}],
        "errors": [{"pv_name": _CMD, "error": "Timeout"}],
    }
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))

    report = result["report"]
    assert isinstance(report, dict)
    assert report["query"] == "DEV-TEST01:Ctrl-EVR-01"
    assert [s["display_path"] for s in report["screens"]] == ["panel.bob"]
    # pva:// stripped in the screen's matched_channels; both channels present, sorted.
    assert set(report["screens"][0]["matched_channels"]) == {_STATUS, _CMD}

    by_channel = {c["channel"]: c for c in report["channels"]}
    assert by_channel[_STATUS]["connected"] is True
    assert by_channel[_STATUS]["value"] == 1
    assert by_channel[_STATUS]["severity"] == "MINOR"
    assert by_channel[_CMD]["connected"] is False
    assert by_channel[_CMD]["error"] == "Timeout"
    assert report["channelfinder_enabled"] is False
    assert any("ChannelFinder disabled" in note for note in report["notes"])

    # The live read got the protocol-stripped channels (channel_name applied), not pva://.
    mock_batch.assert_awaited_once()
    assert mock_batch.await_args is not None
    read_arg = mock_batch.await_args.args[0]
    assert set(read_arg) == {_STATUS, _CMD}
    assert all("://" not in ch for ch in read_arg)

    assert isinstance(result["markdown"], str)
    assert "# Device Lookup" in result["markdown"]


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.get_config", return_value=SimpleNamespace(max_batch_size=1))
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_live_capped(
    mock_batch: AsyncMock, _mock_cfg: object, tmp_path: Path
) -> None:
    """With max_batch_size=1 and 2 matched channels, the live read is capped to one and an honest
    'N of M' note is emitted; THIS cap does not shorten the screen list (the walk's own two caps
    can, and they carry their own notes since GB-65)."""
    mock_batch.return_value = {"results": [{"pv_name": _CMD, "value": 0}], "errors": []}
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))
    report = result["report"]
    assert isinstance(report, dict)
    assert report["live_capped"] is True
    assert report["total_matched_channels"] == 2
    assert len(report["channels"]) == 1  # only one channel read live
    assert len(report["screens"]) == 1  # untouched by the LIVE cap (not a completeness claim)
    assert any("1 of 2 matched channels" in note for note in report["notes"])
    # Exactly one channel (the cap) was read.
    assert mock_batch.await_args is not None
    assert len(mock_batch.await_args.args[0]) == 1


@pytest.mark.asyncio
async def test_find_device_tool_rejects_empty_query(tmp_path: Path) -> None:
    with pytest.raises(EpicsError) as exc:
        await _find_device("   ", str(_displays(tmp_path)))
    assert exc.value.error_code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_find_device_tool_rejects_bad_displays_dir(tmp_path: Path) -> None:
    with pytest.raises(EpicsError) as exc:
        await _find_device("DEV-TEST01", str(tmp_path / "nope"))
    assert exc.value.error_code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_find_device_tool_rejects_displays_dir_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: an existing displays_dir outside the opt-in allowed_roots is rejected."""
    import epics_mcp.config as config_module

    displays = _displays(tmp_path)  # exists, but outside the allowed root below
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(allowed))
    config_module._config = None
    try:
        with pytest.raises(EpicsError) as exc:
            await _find_device("DEV-TEST01", str(displays))
        assert exc.value.error_code == "PATH_OUTSIDE_WORKSPACE"
    finally:
        config_module._config = None


@pytest.mark.asyncio
async def test_server_find_device_maps_error_to_tool_error(tmp_path: Path) -> None:
    """The server wrapper maps EpicsError to ToolError with the error_code tag."""
    from fastmcp.exceptions import ToolError

    from epics_mcp.display_tools import find_device

    with pytest.raises(ToolError, match="INVALID_INPUT"):
        await find_device("DEV-TEST01", str(tmp_path / "nope"))


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.query_channels", new_callable=AsyncMock)
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_channelfinder_enabled_substring(
    mock_batch: AsyncMock, mock_cf: AsyncMock, tmp_path: Path
) -> None:
    """CF-ENABLED end-to-end (match='substring'): source IOC joins by channel name, and the glob
    is broadened to ``*stem*`` so substring-matched channels (which need not start with the query)
    are actually fetched (Impl-QA M1). The captured glob arg proves it."""
    mock_batch.return_value = {
        "results": [{"pv_name": _STATUS, "value": 1}, {"pv_name": _CMD, "value": 0}],
        "errors": [],
    }
    mock_cf.return_value = {
        "enabled": True,
        "channels": [{"name": _STATUS, "ioc_name": "IOC-EVR-01", "host_name": "dln01-host"}],
    }
    result = await _find_device("EVR", str(_displays(tmp_path)), match="substring")
    report = result["report"]
    assert isinstance(report, dict)
    assert report["channelfinder_enabled"] is True
    assert not any("ChannelFinder disabled" in n for n in report["notes"])
    by_channel = {c["channel"]: c for c in report["channels"]}
    assert by_channel[_STATUS]["source_ioc"] == "IOC-EVR-01"
    assert by_channel[_STATUS]["source_host"] == "dln01-host"
    assert by_channel[_CMD]["source_ioc"] is None  # not in the CF result → honest None
    # M1: the substring CF query is broadened (``*EVR*``) so it covers the matched channels.
    assert mock_cf.await_args is not None
    assert mock_cf.await_args.args[0] == "*EVR*"


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.query_channels", new_callable=AsyncMock)
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_channelfinder_unreachable_degrades(
    mock_batch: AsyncMock, mock_cf: AsyncMock, tmp_path: Path
) -> None:
    """A transient ChannelFinder failure must NOT sink the tool, screens + live still return, with
    an honest 'unreachable' note (Impl-QA M2). CF is best-effort, not a hard dependency."""
    mock_batch.return_value = {"results": [{"pv_name": _STATUS, "value": 1}], "errors": []}
    mock_cf.side_effect = EpicsConnectionError("ChannelFinder: connection refused")
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))
    report = result["report"]
    assert isinstance(report, dict)
    assert [s["display_path"] for s in report["screens"]] == ["panel.bob"]  # screens survive
    assert any(c["channel"] == _STATUS for c in report["channels"])  # live read survives
    assert any("unreachable" in n.lower() for n in report["notes"])


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.query_channels", new_callable=AsyncMock)
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_channelfinder_non_epics_error_degrades(
    mock_batch: AsyncMock, mock_cf: AsyncMock, tmp_path: Path
) -> None:
    """S7-6: a NON-EpicsError from ChannelFinder (an unexpected client/projection bug) must ALSO
    degrade, screens + live still return with an 'unreachable' note, the tool never propagates."""
    mock_batch.return_value = {"results": [{"pv_name": _STATUS, "value": 1}], "errors": []}
    mock_cf.side_effect = ValueError("unexpected projection bug")  # NOT an EpicsError
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))
    report = result["report"]
    assert isinstance(report, dict)
    assert [s["display_path"] for s in report["screens"]] == ["panel.bob"]  # screens survive
    assert any(c["channel"] == _STATUS for c in report["channels"])  # live read survives
    assert any("unreachable" in n.lower() for n in report["notes"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("match", "query"),
    [("exact", _STATUS), ("prefix", "DEV-TEST01:Ctrl-EVR-01"), ("substring", "EVR")],
)
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_match_modes(
    mock_batch: AsyncMock, match: str, query: str, tmp_path: Path
) -> None:
    """All three match modes reach the tool and find panel.bob; report['match'] flows (S2)."""
    mock_batch.return_value = {"results": [{"pv_name": _STATUS, "value": 1}], "errors": []}
    disp = str(_displays(tmp_path))
    result = await _find_device(query, disp, match=match)  # type: ignore[arg-type]
    report = result["report"]
    assert isinstance(report, dict)
    assert report["match"] == match
    assert [s["display_path"] for s in report["screens"]] == ["panel.bob"]


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_live_read_contract_error_degrades(
    mock_batch: AsyncMock, tmp_path: Path
) -> None:
    """S27: a provider length-contract breach (UPSTREAM_CONTRACT_ERROR) from the live batch read
    must degrade the LIVE part but keep the already-computed offline screens, must NOT sink the
    whole tool. Goes RED against the pre-S27 find_device (live read sits outside try/except → the
    error propagates and _find_device raises)."""
    mock_batch.side_effect = EpicsError(
        "EPICS provider returned 1 values for 2 requested PVs",
        error_code="UPSTREAM_CONTRACT_ERROR",
        details={"requested": 2, "received": 1},
    )
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))

    report = result["report"]
    assert isinstance(report, dict)
    # Offline screens survive the live-read failure, the point of degrading, not sinking.
    assert [s["display_path"] for s in report["screens"]] == ["panel.bob"]
    # Live degraded to an empty envelope: no per-channel status, but the tool did NOT raise.
    assert report["channels"] == []
    # The degrade is surfaced as a report note (not a silent blank), mirroring CF's honest degrade.
    assert any("Live values unavailable" in note for note in report["notes"])
    # The live read WAS attempted (the wrap caught a real call, not a no-op because read was empty).
    mock_batch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("n_context", "n_glob"), [(1, 2), (3, 1)])
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_the_walk_caps_travel_from_the_inventory_into_the_report(
    mock_batch: AsyncMock,
    n_context: int,
    n_glob: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GB-65: the two cap signals survive the WIRING, not just the merge.

    ``test_device_lookup.py`` proves the notes are built when the values arrive; nothing proved
    they arrive. Measured with two mutants, both of which left all 37 tests of the pair green:
    returning ``((), 0)`` from ``_run_lookup``, and passing empty values for both keywords at the
    ``build_device_report`` call. Either one restores the exact silence GB-65 removes, while the
    suite reports success. This test is the seam those mutants cross.

    ⚠ TWO input vectors, and the second one is not decoration. With a single vector this test
    still passed a wiring that returned a CONSTANT ``("x",), 2``, and one that truncated with
    ``context_capped[:1]`` (invisible at cardinality 1). Both were measured green against the
    one-vector version by the post-build review, which is exactly the "a constant cannot pass"
    claim this docstring used to make and could not keep. The counts differ per vector AND differ
    from each other within a vector, so no constant and no truncation satisfies both rows.

    The inventory RUN is the only thing replaced (its diagnostics are swapped for known values);
    ``find_displays``, the channel collection and the whole report assembly stay real, because the
    claim under test is that ``_run_lookup`` reads the diagnostics off the inventory and hands them
    on. Faking any further out would test the fake. The narrow keyword signature is deliberate: it
    is the set ``_find_device`` actually passes, so a silently added argument reddens this too, and
    the fake now RECORDS what it received, because a signature pins the argument NAMES and says
    nothing about the values reaching the engine (CLAUDE.md, evidence discipline, point 8).
    """
    mock_batch.return_value = {"results": [], "errors": []}
    seen: dict[str, object] = {}

    def _inventory_with_caps(
        repo_root: Path, *, context_cap: int, windows_paths: bool
    ) -> PvInventory:
        seen["context_cap"] = context_cap
        seen["windows_paths"] = windows_paths
        real = analyze_pv_inventory(repo_root, context_cap=context_cap, windows_paths=windows_paths)
        return real.model_copy(
            update={
                "diagnostics": PvDiagnostics(
                    context_capped=tuple(f"capped{i}.bob" for i in range(n_context)),
                    glob_capped=tuple(("panel.bob", f"../{i}/*.bob") for i in range(n_glob)),
                )
            }
        )

    monkeypatch.setattr(
        "epics_mcp.services.inventory_adapter.analyze_pv_inventory", _inventory_with_caps
    )
    result = await _find_device(
        "DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)), context_cap=17, windows_paths=True
    )

    # The caller's knobs reach the engine, not just its parameter names.
    assert seen == {"context_cap": 17, "windows_paths": True}

    report = result["report"]
    assert isinstance(report, dict)
    notes = report["notes"]
    assert any(f"{n_context} display(s) hit the per-display context cap" in n for n in notes), notes
    assert any(f"{n_glob} globbed <file> reference(s) hit the glob cap" in n for n in notes), notes
    # The screens themselves still come through: the caps are notes, never a withhold.
    assert len(report["screens"]) == 1


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_an_uncapped_run_carries_no_cap_note_at_all(
    mock_batch: AsyncMock, tmp_path: Path
) -> None:
    """The negative control the wiring test needs, on the REAL inventory.

    Without it, an implementation that emits both notes unconditionally would satisfy every
    positive assertion above, and a permanent warning is one nobody reads. The sibling commit
    demands exactly this for the "too narrow" note; the wiring half had no such control until the
    post-build review pointed at the asymmetry. The fixture is a single .bob with no embeds, so
    neither cap can fire, and both notes must be absent.
    """
    mock_batch.return_value = {"results": [], "errors": []}
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))

    report = result["report"]
    assert isinstance(report, dict)
    notes = report["notes"]
    assert len(report["screens"]) == 1  # the walk DID find the screen, so this is not a null run
    assert not any("context cap" in n for n in notes), notes
    assert not any("glob cap" in n for n in notes), notes
