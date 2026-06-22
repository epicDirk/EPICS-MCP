"""Offline tests for the cross-plane join (in-test JoinPv rows, injected fake Naming checker).

The join consumes :class:`JoinPv` rows (macro-expanded, operator-facing display-PV instances from
the ``opi_navigation`` inventory); these tests build them by hand for full determinism (no I/O, no
``analyze_pv_inventory``). The adapter that produces JoinPv from a real PvInventory — including the
operator-facing/fragment-seed filter — is covered in ``test_inventory_adapter.py``; the wired
end-to-end path (real .bob → resolved → linked) in ``test_crossplane_tool.py``.
"""

from epics_pv_mcp.services.crossplane import JoinPv, crossplane_check, render_markdown
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


def _jp(
    display: str,
    pv: str,
    *,
    resolution: str = "resolved",
    role: str = "read",
    protocol: str = "ca",
) -> JoinPv:
    """Build a JoinPv row (defaults: resolved, read, ca)."""
    return JoinPv(display=display, pv=pv, resolution=resolution, role=role, protocol=protocol)  # type: ignore[arg-type]


def test_linked_indeterminate_and_other_prefix() -> None:
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
        _jp("a.bob", "SYS:Foo", resolution="dynamic"),
        _jp("b.bob", "OTHER-SYS:thing"),
    ]
    report = crossplane_check(join, _st(), naming=_FakeNaming("ACTIVE"))
    assert "DEV-TEST01:Ctrl-EVR-01:status" in report.pvs_linked
    assert report.displays_linked == ("a.bob",)
    assert report.pvs_indeterminate == ("SYS:Foo",)
    assert report.pvs_indeterminate_occurrences == 1
    assert "OTHER-SYS:thing" in report.pvs_other_prefix
    assert report.naming is not None
    assert report.naming.registered is True


def test_resolved_macro_collapses_to_linked() -> None:
    # WEDGE-KERNBEWEIS: eine vormals makro-templatisierte PV, jetzt vom Inventar konkret aufgelöst
    # (resolution="resolved") und prefix-teilend → landet in pvs_linked, NICHT in pvs_indeterminate.
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Cmd", role="write")]
    report = crossplane_check(join, _st())
    assert report.pvs_linked == ("DEV-TEST01:Ctrl-EVR-01:Cmd",)
    assert report.pvs_indeterminate == ()
    assert report.pvs_linked_write == ("DEV-TEST01:Ctrl-EVR-01:Cmd",)


def test_dynamic_and_unresolved_stay_indeterminate() -> None:
    join = [
        _jp("a.bob", "SYS:Dyn", resolution="dynamic"),
        _jp("a.bob", "SYS:Unres", resolution="unresolved"),
    ]
    report = crossplane_check(join, _st())
    assert report.pvs_dynamic == ("SYS:Dyn",)
    assert report.pvs_unresolved == ("SYS:Unres",)
    assert report.pvs_indeterminate == ("SYS:Dyn", "SYS:Unres")
    assert report.pvs_indeterminate_occurrences == 2


def test_indeterminate_distinct_vs_occurrences() -> None:
    # Dieselbe unauflösbare PV über ZWEI Displays: distinct 1, aber 2 (display, pv)-Referenzen.
    join = [
        _jp("a.bob", "SYS:Status", resolution="dynamic"),
        _jp("b.bob", "SYS:Status", resolution="dynamic"),
    ]
    report = crossplane_check(join, _st())
    assert report.pvs_indeterminate == ("SYS:Status",)
    assert report.pvs_indeterminate_occurrences == 2


def test_non_channel_protocol_excluded() -> None:
    join = [
        _jp("a.bob", "x", resolution="resolved", protocol="loc"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:y"),
    ]
    report = crossplane_check(join, _st())
    assert report.pvs_non_channel == ("x",)
    assert "x" not in report.pvs_linked
    assert "x" not in report.pvs_other_prefix
    assert "x" not in report.pvs_indeterminate
    assert any("non-channel" in note for note in report.notes)


def test_linked_write_split() -> None:
    # Dieselbe PV read+write über zwei Displays: distinct 1 linked, aber als writable geführt.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:SP", role="read"),
        _jp("b.bob", "DEV-TEST01:Ctrl-EVR-01:SP", role="write"),
    ]
    report = crossplane_check(join, _st())
    assert report.pvs_linked == ("DEV-TEST01:Ctrl-EVR-01:SP",)
    assert report.pvs_linked_write == ("DEV-TEST01:Ctrl-EVR-01:SP",)
    assert set(report.displays_linked) == {"a.bob", "b.bob"}


def test_context_capped_surfaces_lower_bound() -> None:
    report = crossplane_check(
        [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")], _st(), context_capped=("big.bob",)
    )
    assert report.displays_incomplete == ("big.bob",)
    assert any("lower bound" in note.lower() for note in report.notes)


def test_prefix_none_all_other_prefix() -> None:
    report = crossplane_check([_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")], StCmdInfo())
    assert report.pvs_other_prefix == ("DEV-TEST01:Ctrl-EVR-01:x",)
    assert report.pvs_linked == ()
    assert any("no ioc device prefix" in note.lower() for note in report.notes)


def test_offline_no_naming_has_deferred_note() -> None:
    report = crossplane_check([_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")], _st())
    assert report.naming is None
    assert any("module repos deferred" in note for note in report.notes)


def test_broken_only_with_ioc_db() -> None:
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:missing"),
    ]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, set[str]())
    report = crossplane_check(join, _st(), ioc_db=ioc_db)
    assert report.broken == ("DEV-TEST01:Ctrl-EVR-01:missing",)
    assert report.ioc_db_resolved == 1


def test_render_markdown_deterministic_and_new_branches() -> None:
    report = crossplane_check(
        [
            _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x", role="write"),
            _jp("a.bob", "SYS:Dyn", resolution="dynamic"),
            _jp("a.bob", "sig", resolution="resolved", protocol="loc"),
        ],
        _st(),
        naming=_FakeNaming("ACTIVE"),
        context_capped=("big.bob",),
    )
    markdown = render_markdown(report)
    assert "Cross-Plane PV Provenance" in markdown
    assert "ACTIVE" in markdown
    assert "**Indeterminate (dynamic+unresolved):** 1 (1 references)" in markdown
    assert "of which writable: 1" in markdown
    assert "**Non-channel refs (loc/sim/sys/other, excluded):** 1" in markdown
    assert "**Displays with incomplete inventory (lower bound):** 1" in markdown
    assert render_markdown(report) == markdown  # deterministic


def test_render_markdown_broken_db_and_unregistered_naming() -> None:
    # Deckt die render-Zweige broken / IOC-.db-Block / naming-not-registered (⚠️) ab.
    report = crossplane_check(
        [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:gone")],
        _st(),
        naming=_FakeNaming("RESERVED"),
        ioc_db=({"DEV-TEST01:Ctrl-EVR-01:other"}, set[str]()),
    )
    assert report.broken == ("DEV-TEST01:Ctrl-EVR-01:gone",)
    markdown = render_markdown(report)
    assert "⚠️ RESERVED" in markdown
    assert "**IOC .db PVs:** 1 resolved" in markdown
    assert "**Broken (linked PV absent from IOC .db):** 1" in markdown


def test_notes_glob_capped_and_needs_msi() -> None:
    # Die ehrlichen Untergrenzen-Notes: glob-cap (Adapter) und needs-msi (.db-Substitution).
    glob = crossplane_check([_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")], _st(), glob_capped_count=2)
    assert any("glob cap" in note for note in glob.notes)
    msi = crossplane_check([], _st(), ioc_db=(set[str](), {"DEV-TEST01:Ctrl-EVR-01:$(R)"}))
    assert msi.ioc_db_needs_msi == 1
    assert any("needs msi" in note for note in msi.notes)
