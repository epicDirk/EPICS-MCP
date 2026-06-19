"""Offline tests for the cross-plane join (injected fake Naming checker)."""

from epics_pv_mcp.services.crossplane import crossplane_check, render_markdown
from epics_pv_mcp.services.e3_db import StCmdInfo
from epics_pv_mcp.services.naming_client import NameStatus


class _FakeNaming:
    """Injectable stand-in for NamingServiceClient (no network)."""

    def __init__(self, status: str) -> None:
        self._status = status

    def validate_name(self, ess_name: str) -> NameStatus:
        return NameStatus(
            registered=self._status == "ACTIVE",
            status=self._status,
            message=f"{ess_name}: {self._status}",
        )


def _st() -> StCmdInfo:
    return StCmdInfo(prefix="DEV-TEST01:Ctrl-EVR-01:")


def test_linked_indeterminate_and_other_prefix() -> None:
    displays = {
        "a.bob": ["DEV-TEST01:Ctrl-EVR-01:status", "$(P)$(R)foo"],
        "b.bob": ["OTHER-SYS:thing"],
    }
    report = crossplane_check(displays, _st(), naming=_FakeNaming("ACTIVE"))
    assert "DEV-TEST01:Ctrl-EVR-01:status" in report.pvs_linked
    assert report.displays_linked == ("a.bob",)
    assert report.pvs_indeterminate == 1
    assert "OTHER-SYS:thing" in report.pvs_other_prefix
    assert report.naming is not None
    assert report.naming.registered is True


def test_offline_no_naming_has_deferred_note() -> None:
    report = crossplane_check({"a.bob": ["DEV-TEST01:Ctrl-EVR-01:x"]}, _st())
    assert report.naming is None
    assert any("module repos deferred" in note for note in report.notes)


def test_broken_only_with_ioc_db() -> None:
    displays = {
        "a.bob": ["DEV-TEST01:Ctrl-EVR-01:status", "DEV-TEST01:Ctrl-EVR-01:missing"]
    }
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, set[str]())
    report = crossplane_check(displays, _st(), ioc_db=ioc_db)
    assert report.broken == ("DEV-TEST01:Ctrl-EVR-01:missing",)
    assert report.ioc_db_resolved == 1


def test_render_markdown_deterministic() -> None:
    report = crossplane_check(
        {"a.bob": ["DEV-TEST01:Ctrl-EVR-01:x"]}, _st(), naming=_FakeNaming("ACTIVE")
    )
    markdown = render_markdown(report)
    assert "Cross-Plane PV Provenance" in markdown
    assert "ACTIVE" in markdown
    assert render_markdown(report) == markdown  # deterministic
