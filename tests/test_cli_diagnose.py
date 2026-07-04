"""Tests for the epics-diagnose CLI (cli_diagnose) — the module was 0% covered (M14/C8).

Golden ``_render`` plus ``main()`` driven as an AsyncMock spy: the exit-0-on-disconnect contract,
flag threading, JSON mode, and the argparse usage error. The heavy ``diagnose()`` logic lives in
test_diagnose; here it is stubbed so only the CLI surface (rendering, flags, exit) is exercised.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from epics_pv_mcp import cli_diagnose
from epics_pv_mcp.services.diagnose import (
    AlarmEvidence,
    ArchiverEvidence,
    ChannelFinderEvidence,
    Confidence,
    DiagnoseEvidence,
    DiagnoseReport,
    LikelyCause,
    LiveEvidence,
    NamingEvidence,
    State,
)


def _report(
    *,
    pv_name: str = "SYS:PV",
    state: State = "connected",
    likely_cause: LikelyCause = "healthy",
    confidence: Confidence = "confirmed",
    live: LiveEvidence | None = None,
    cf: ChannelFinderEvidence | None = None,
    naming: NamingEvidence | None = None,
    archiver: ArchiverEvidence | None = None,
    alarm: AlarmEvidence | None = None,
    next_steps: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
    withheld: tuple[str, ...] = (),
) -> DiagnoseReport:
    """Build a DiagnoseReport with sensible defaults (unconsulted planes) for the tests."""
    evidence = DiagnoseEvidence(
        live=live or LiveEvidence(connected=state == "connected"),
        channelfinder=cf or ChannelFinderEvidence(consulted=False),
        naming=naming or NamingEvidence(consulted=False),
        archiver=archiver or ArchiverEvidence(consulted=False),
        alarm=alarm or AlarmEvidence(consulted=False),
    )
    return DiagnoseReport(
        pv_name=pv_name,
        state=state,
        likely_cause=likely_cause,
        confidence=confidence,
        evidence=evidence,
        next_steps=next_steps,
        notes=notes,
        withheld=withheld,
    )


# --- _render (golden) ---


def test_render_connected_shows_value_and_next_steps() -> None:
    report = _report(
        live=LiveEvidence(connected=True, value=42, severity="NO_ALARM"),
        next_steps=("PV answers on PVA.",),
    )
    out = cli_diagnose._render(report)
    assert "PV:           SYS:PV" in out
    assert "State:        connected" in out
    assert "Likely cause: healthy  (confidence: confirmed)" in out
    assert "connected, value=42" in out
    assert "Next steps:" in out
    assert "  - PV answers on PVA." in out


def test_render_disconnected_shows_error_code_planes_and_withheld() -> None:
    report = _report(
        state="disconnected",
        likely_cause="ioc_down",
        confidence="likely",
        live=LiveEvidence(connected=False, error_code="PV_TIMEOUT"),
        cf=ChannelFinderEvidence(
            consulted=True, registered=True, pv_status="Inactive", ioc_name="IOC1"
        ),
        archiver=ArchiverEvidence(consulted=True, archived=True),
        notes=("ChannelFinder last-known pvStatus is stale.",),
        withheld=("naming", "alarm"),
    )
    out = cli_diagnose._render(report)
    assert "disconnected (PV_TIMEOUT)" in out
    assert "channelfinder: registered=True" in out
    assert "pvStatus=Inactive" in out
    assert "ioc=IOC1" in out
    assert "archiver:      archived=True" in out
    assert "Notes:" in out
    assert "Withheld planes (requested but unavailable): naming, alarm" in out


def test_render_consulted_naming_and_alarm_lines() -> None:
    report = _report(
        naming=NamingEvidence(consulted=True, registered=True, status="ACTIVE"),
        alarm=AlarmEvidence(consulted=True, configured=True),
    )
    out = cli_diagnose._render(report)
    assert "naming:        registered=True (ACTIVE)" in out
    assert "alarm:         configured=True" in out


# --- main (AsyncMock spy) ---


def test_main_returns_0_and_threads_the_plane_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """main returns 0 and passes each CLI flag through to diagnose() (spy on the awaited kwargs)."""
    spy = AsyncMock(return_value=_report())
    with patch("epics_pv_mcp.cli_diagnose.diagnose", spy):
        rc = cli_diagnose.main(
            ["SYS:PV", "--naming", "--archiver", "--alarm", "--no-channelfinder"]
        )
    out = capsys.readouterr().out
    assert rc == 0
    assert "State:        connected" in out
    spy.assert_awaited_once()
    assert spy.await_args is not None
    kwargs = spy.await_args.kwargs
    assert kwargs["check_naming"] is True
    assert kwargs["check_archiver"] is True
    assert kwargs["check_alarm"] is True
    assert kwargs["check_channelfinder"] is False  # --no-channelfinder flips the default-on plane


def test_main_exit_0_even_when_pv_disconnected(capsys: pytest.CaptureFixture[str]) -> None:
    """A disconnect is a normal diagnostic result, not a crash → still exit 0."""
    disconnected = _report(
        state="disconnected",
        likely_cause="ioc_down",
        confidence="likely",
        live=LiveEvidence(connected=False, error_code="PV_TIMEOUT"),
    )
    with patch("epics_pv_mcp.cli_diagnose.diagnose", AsyncMock(return_value=disconnected)):
        rc = cli_diagnose.main(["SYS:PV"])
    assert rc == 0
    assert "disconnected" in capsys.readouterr().out


def test_main_json_mode_emits_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("epics_pv_mcp.cli_diagnose.diagnose", AsyncMock(return_value=_report())):
        rc = cli_diagnose.main(["SYS:PV", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["pv_name"] == "SYS:PV"
    assert parsed["state"] == "connected"


def test_main_missing_pv_name_is_argparse_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli_diagnose.main([])  # pv_name is a required positional
    assert exc.value.code == 2  # argparse exits 2 on a usage error (distinct from the 0 contract)
