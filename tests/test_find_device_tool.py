"""End-to-end tests for the find_device tool (real .bob via analyze_pv_inventory; p4p read mocked).

These exercise the WIRED path: a real operator .bob over the macro-aware ``opi_navigation``
inventory → ``find_displays`` → channel collection → live read → report. The p4p batch read is
mocked AT THE find_device IMPORT SITE (``epics_pv_mcp.tools.find_device.pv_get_batch``) with a
hand-built ``{results, errors}`` — no real IOC, no shared p4p fake. ChannelFinder is disabled by
default (no URL), so source IOC is honestly absent. The pure merge is in ``test_device_lookup.py``.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from epics_pv_mcp.errors import EpicsConnectionError, EpicsError
from epics_pv_mcp.tools.find_device import _find_device

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
@patch("epics_pv_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
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
@patch("epics_pv_mcp.tools.find_device.get_config", return_value=SimpleNamespace(max_batch_size=1))
@patch("epics_pv_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_live_capped(
    mock_batch: AsyncMock, _mock_cfg: object, tmp_path: Path
) -> None:
    """With max_batch_size=1 and 2 matched channels, the live read is capped to one and an honest
    'N of M' note is emitted; the screen list stays complete."""
    mock_batch.return_value = {"results": [{"pv_name": _CMD, "value": 0}], "errors": []}
    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))
    report = result["report"]
    assert isinstance(report, dict)
    assert report["live_capped"] is True
    assert report["total_matched_channels"] == 2
    assert len(report["channels"]) == 1  # only one channel read live
    assert len(report["screens"]) == 1  # screens complete regardless of the live cap
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
async def test_server_find_device_maps_error_to_tool_error(tmp_path: Path) -> None:
    """The server wrapper maps EpicsError to ToolError with the error_code tag."""
    from fastmcp.exceptions import ToolError

    from epics_pv_mcp.server import find_device

    with pytest.raises(ToolError, match="INVALID_INPUT"):
        await find_device("DEV-TEST01", str(tmp_path / "nope"))


@pytest.mark.asyncio
@patch("epics_pv_mcp.tools.find_device._find_channels", new_callable=AsyncMock)
@patch("epics_pv_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
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
@patch("epics_pv_mcp.tools.find_device._find_channels", new_callable=AsyncMock)
@patch("epics_pv_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_find_device_tool_channelfinder_unreachable_degrades(
    mock_batch: AsyncMock, mock_cf: AsyncMock, tmp_path: Path
) -> None:
    """A transient ChannelFinder failure must NOT sink the tool — screens + live still return, with
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
@pytest.mark.parametrize(
    ("match", "query"),
    [("exact", _STATUS), ("prefix", "DEV-TEST01:Ctrl-EVR-01"), ("substring", "EVR")],
)
@patch("epics_pv_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
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
