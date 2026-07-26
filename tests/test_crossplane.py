"""Offline tests for the cross-plane join (in-test JoinPv rows, injected fake Naming checker).

The join consumes :class:`JoinPv` rows (macro-expanded, operator-facing display-PV instances from
the ``opi_navigation`` inventory); these tests build them by hand for full determinism (no I/O, no
``analyze_pv_inventory``). The adapter that produces JoinPv from a real PvInventory — including the
operator-facing/fragment-seed filter — is covered in ``test_inventory_adapter.py``; the wired
end-to-end path (real .bob → resolved → linked) in ``test_crossplane_tool.py``.
"""

from epics_pv_mcp.services.crossplane import (
    CFRegistryCapped,
    JoinPv,
    crossplane_check,
    render_markdown,
)
from epics_pv_mcp.services.e3_db import StCmdInfo
from epics_pv_mcp.services.naming_client import NameStatus
from epics_pv_mcp.services.naming_exceptions import NamingServiceResponseError


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


class _RaisingNaming:
    """Naming checker whose validate_name fails with a NON-404 service error (DS-2): the report must
    WITHHOLD the naming verdict (leave it None), not abort the whole cross-plane join."""

    def validate_name(self, ess_name: str) -> NameStatus:
        raise NamingServiceResponseError("Naming service 500")


class _FakeCF:
    """Injectable ChannelFinderChecker (no network): registers a fixed set of channel names."""

    def __init__(self, registered: set[str]) -> None:
        self._registered = registered

    def registered_under(self, prefix: str) -> set[str]:
        return {name for name in self._registered if name.startswith(prefix)}


class _RaisingCF:
    """ChannelFinderChecker whose query fails (generic RuntimeError) → cf_unregistered withheld."""

    def registered_under(self, prefix: str) -> set[str]:
        raise RuntimeError("ChannelFinder query failed: boom")


class _CappedCF:
    """ChannelFinderChecker that hit the result cap → cf_unregistered withheld + cf_capped."""

    def registered_under(self, prefix: str) -> set[str]:
        raise CFRegistryCapped("capped")


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


def test_naming_service_error_withholds_verdict_not_crash() -> None:
    """A non-404 naming failure must not abort the join — the report has naming=None (withheld) and
    the rest of the cross-plane buckets are still produced (DS-2 / audit S5)."""
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status")]
    report = crossplane_check(join, _st(), naming=_RaisingNaming())
    assert report.naming is None
    assert "DEV-TEST01:Ctrl-EVR-01:status" in report.pvs_linked


def test_resolved_macro_collapses_to_linked() -> None:
    # Core proof: a PV that used to be macro-templated, now resolved concretely by the inventory
    # (resolution="resolved") and prefix-sharing, lands in pvs_linked, NOT in pvs_indeterminate.
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Cmd", role="write")]
    report = crossplane_check(join, _st())
    assert report.pvs_linked == ("DEV-TEST01:Ctrl-EVR-01:Cmd",)
    assert report.pvs_indeterminate == ()
    assert report.pvs_linked_write == ("DEV-TEST01:Ctrl-EVR-01:Cmd",)


def test_join_is_string_literal_so_raw_protocol_prefix_misbuckets() -> None:
    """Join CONTRACT — the reason normalization belongs at the ADAPTER edge, not here.

    crossplane_check buckets a resolved ca/pva PV by a LITERAL ``jp.pv.startswith(prefix)`` (:162);
    it deliberately does NOT strip protocols (single responsibility — ``crossplane.py`` carries no
    ``opi_navigation`` import). So a RAW ``pva://``-prefixed PV that shares the IOC device prefix
    mis-buckets as ``other_prefix``; only its CHANNEL form (what the adapter produces) reaches
    ``linked``. This pins WHY the adapter must normalize — if the join ever stripped protocols
    itself, the raw case below would flip to ``linked`` and this test fails (a guard for the
    responsibility split). NB: bucketing keys on ``jp.protocol`` (:158), so both rows are pva.
    """
    raw = "pva://DEV-TEST01:Ctrl-EVR-01:X"
    channel = "DEV-TEST01:Ctrl-EVR-01:X"
    raw_report = crossplane_check([_jp("a.bob", raw, protocol="pva")], _st())
    assert raw_report.pvs_other_prefix == (raw,)  # join does NOT strip — by design
    assert raw_report.pvs_linked == ()
    channel_report = crossplane_check([_jp("a.bob", channel, protocol="pva")], _st())
    assert channel_report.pvs_linked == (channel,)  # channel form (adapter output) links correctly
    assert channel_report.pvs_other_prefix == ()


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
    # The same unresolvable PV across TWO displays: distinct 1, but 2 (display, pv) references.
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
    # The same PV read+write across two displays: distinct 1 linked, but carried as writable.
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


def test_broken_only_with_complete_ioc_db() -> None:
    # broken is emitted ONLY for a provably complete + fully resolved .db set (ioc_db_complete=True,
    # no needs-msi residue): the missing linked PV is then provably absent.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:missing"),
    ]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, set[str]())
    report = crossplane_check(join, _st(), ioc_db=ioc_db, ioc_db_complete=True)
    assert report.broken == ("DEV-TEST01:Ctrl-EVR-01:missing",)
    assert report.ioc_db_resolved == 1


def test_broken_withheld_when_db_not_marked_complete() -> None:
    # Same data, but ioc_db_complete defaults False → absence not proven → broken withheld + note.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:missing"),
    ]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, set[str]())
    report = crossplane_check(join, _st(), ioc_db=ioc_db)  # complete defaults False
    assert report.broken == ()
    assert report.ioc_db_resolved == 1
    assert any("broken verdict withheld" in note.lower() for note in report.notes)


def test_broken_withheld_when_needs_msi_residue() -> None:
    # ioc_db_complete=True but a needs-msi record remains → still withheld (all-or-nothing) + note.
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:missing")]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, {"DEV-TEST01:Ctrl-EVR-01:$(R)x"})
    report = crossplane_check(join, _st(), ioc_db=ioc_db, ioc_db_complete=True)
    assert report.broken == ()
    markdown = render_markdown(report)
    assert any("broken verdict withheld" in note.lower() for note in report.notes)
    assert "**Broken (linked PV absent" not in markdown  # render must not claim a verdict


def test_broken_write_surfaced() -> None:
    # A writable linked PV that is broken is also surfaced as broken_write (dead command target).
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Cmd", role="write"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
    ]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, set[str]())
    report = crossplane_check(join, _st(), ioc_db=ioc_db, ioc_db_complete=True)
    assert report.broken == ("DEV-TEST01:Ctrl-EVR-01:Cmd",)
    assert report.broken_write == ("DEV-TEST01:Ctrl-EVR-01:Cmd",)
    assert "of which writable (dead command target): 1" in render_markdown(report)


def test_broken_write_is_strict_subset_of_broken() -> None:
    # QA C7: broken_write must be exactly the WRITABLE broken PVs, not a copy of broken — a
    # read-only broken PV stays out of broken_write.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:CmdW", role="write"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:ReadR", role="read"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
    ]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:status"}, set[str]())
    report = crossplane_check(join, _st(), ioc_db=ioc_db, ioc_db_complete=True)
    assert set(report.broken) == {"DEV-TEST01:Ctrl-EVR-01:CmdW", "DEV-TEST01:Ctrl-EVR-01:ReadR"}
    assert report.broken_write == (
        "DEV-TEST01:Ctrl-EVR-01:CmdW",
    )  # the read-only broken is excluded


def test_broken_withheld_over_empty_resolved_set() -> None:
    # QA C1 (defense-in-depth): even if a caller marks an EMPTY .db set complete, broken must be
    # withheld — proving absence against zero known PVs would flag every linked PV.
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:anything")]
    report = crossplane_check(join, _st(), ioc_db=(set[str](), set[str]()), ioc_db_complete=True)
    assert report.broken == ()
    assert any("broken verdict withheld" in note.lower() for note in report.notes)


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
        ioc_db_complete=True,
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


# --- cf_unregistered (ChannelFinder plane) -------------------------------------------------------


def test_cf_unregistered_found() -> None:
    # A referenced linked PV NOT registered in ChannelFinder → cf_unregistered; registered one not.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:missing"),
    ]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:status"})
    report = crossplane_check(join, _st(), channelfinder=cf, cf_requested=True)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:missing",)
    assert report.cf_registered == 1
    assert report.cf_capped is False


def test_cf_unregistered_empty_when_all_registered() -> None:
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:status")]
    report = crossplane_check(
        join, _st(), channelfinder=_FakeCF({"DEV-TEST01:Ctrl-EVR-01:status"}), cf_requested=True
    )
    assert report.cf_unregistered == ()
    assert report.cf_registered == 1


def test_cf_unregistered_scoped_to_linked_not_other_prefix() -> None:
    # An other-prefix PV must NEVER be cf_unregistered (it is not this IOC's channel).
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:linkedmiss"),
        _jp("a.bob", "OTHER-SYS:thing"),
    ]
    report = crossplane_check(join, _st(), channelfinder=_FakeCF(set()), cf_requested=True)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:linkedmiss",)
    assert "OTHER-SYS:thing" not in report.cf_unregistered


def test_cf_unregistered_write_is_subset() -> None:
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:CmdMiss", role="write"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:ReadMiss", role="read"),
    ]
    report = crossplane_check(join, _st(), channelfinder=_FakeCF(set()), cf_requested=True)
    assert set(report.cf_unregistered) == {
        "DEV-TEST01:Ctrl-EVR-01:CmdMiss",
        "DEV-TEST01:Ctrl-EVR-01:ReadMiss",
    }
    assert report.cf_unregistered_write == ("DEV-TEST01:Ctrl-EVR-01:CmdMiss",)


def test_cf_withheld_on_query_failure() -> None:
    # A failed CF query withholds cf_unregistered (never false-flag); the rest of the report intact.
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")]
    report = crossplane_check(join, _st(), channelfinder=_RaisingCF(), cf_requested=True)
    assert report.cf_unregistered == ()
    assert report.cf_registered == 0
    assert report.cf_capped is False
    assert report.pvs_linked == ("DEV-TEST01:Ctrl-EVR-01:x",)  # rest intact
    assert any("cf_unregistered withheld" in note.lower() for note in report.notes)


def test_cf_capped_withholds_and_flags() -> None:
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")]
    report = crossplane_check(join, _st(), channelfinder=_CappedCF(), cf_requested=True)
    assert report.cf_unregistered == ()
    assert report.cf_capped is True
    assert any("capped" in note.lower() for note in report.notes)


def test_cf_absent_without_checker() -> None:
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")]
    report = crossplane_check(join, _st())  # no checker, cf_requested defaults False
    assert report.cf_unregistered == ()
    assert report.cf_registered == 0
    assert not any("channelfinder check requested" in note.lower() for note in report.notes)


def test_cf_empty_url_note_when_requested_without_checker() -> None:
    # F5: query_channelfinder=True but no checker wired (URL unset at the edge) → honest note in
    # BOTH report.notes AND render_markdown (markdown renders notes; no silent no-op).
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")]
    report = crossplane_check(join, _st(), cf_requested=True)  # channelfinder None
    assert report.cf_unregistered == ()
    assert any("epics_mcp_channelfinder_url is unset" in note.lower() for note in report.notes)
    assert "EPICS_MCP_CHANNELFINDER_URL is unset" in render_markdown(report)


def test_cf_field_suffix_normalized_before_diff() -> None:
    # F3: a field-suffixed linked PV (.EGU/.VAL) normalizes to its record name before the diff — the
    # .EGU twin of a registered record must NOT be a false cf_unregistered.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Delay-SP.EGU"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Width-SP.VAL"),
    ]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:Delay-SP"})  # only the bare Delay-SP registered
    report = crossplane_check(join, _st(), channelfinder=cf, cf_requested=True)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:Width-SP",)  # record name, not .VAL
    assert "DEV-TEST01:Ctrl-EVR-01:Delay-SP.EGU" not in report.cf_unregistered


def test_cf_record_field_sub_and_no_dot_and_sim_excluded() -> None:
    # F3 grammar: record.FIELD.SUB → record; a bare record (no dot) is unchanged; a dotted sim://
    # value is non_channel and never reaches the cf diff.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Arr-RB.A.B"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:Plain"),
        _jp("a.bob", "ramp(0,1,0.5)", resolution="resolved", protocol="sim"),
    ]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:Plain"})
    report = crossplane_check(join, _st(), channelfinder=cf, cf_requested=True)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:Arr-RB",)  # field.sub stripped
    assert "DEV-TEST01:Ctrl-EVR-01:Plain" not in report.cf_unregistered
    assert "ramp(0,1,0.5)" in report.pvs_non_channel
    assert not any(name.startswith("ramp") for name in report.cf_unregistered)


def test_cf_distinct_from_broken_both_at_once() -> None:
    # A linked PV absent from BOTH the .db AND ChannelFinder appears in BOTH buckets — two planes,
    # both true, not mutually exclusive (no contradiction).
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:served"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:ghost"),
    ]
    ioc_db = ({"DEV-TEST01:Ctrl-EVR-01:served"}, set[str]())
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:served"})
    report = crossplane_check(
        join, _st(), ioc_db=ioc_db, ioc_db_complete=True, channelfinder=cf, cf_requested=True
    )
    assert report.broken == ("DEV-TEST01:Ctrl-EVR-01:ghost",)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:ghost",)


def test_cf_ratio_caveat_fires_at_threshold() -> None:
    # F6 ratio: cf_unregistered/linked >= 0.5 → caveat note with the literal ratio.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:a"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:b"),
    ]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:a"})  # 1 of 2 unregistered = 0.5
    report = crossplane_check(join, _st(), channelfinder=cf, cf_requested=True)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:b",)
    assert any("1/2 linked" in note and ">= 50%" in note for note in report.notes)


def test_cf_ratio_caveat_silent_below_threshold() -> None:
    # 1 of 3 unregistered = 0.33 < 0.5 → no ratio caveat (deterministic threshold).
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:a"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:b"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:c"),
    ]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:a", "DEV-TEST01:Ctrl-EVR-01:b"})
    report = crossplane_check(join, _st(), channelfinder=cf, cf_requested=True)
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:c",)
    assert not any(">= 50%" in note for note in report.notes)


def test_cf_lower_bound_caveat_when_context_capped() -> None:
    # F6 lower-bound: context_capped + cf_unregistered non-empty → undercount caveat (independent of
    # the ratio caveat — different trigger).
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:miss")]
    report = crossplane_check(
        join, _st(), channelfinder=_FakeCF(set()), cf_requested=True, context_capped=("big.bob",)
    )
    assert report.cf_unregistered == ("DEV-TEST01:Ctrl-EVR-01:miss",)
    assert any("cf_unregistered is a lower bound" in note.lower() for note in report.notes)


def test_cf_registered_zero_on_withhold_nonzero_on_success() -> None:
    # F7: cf_registered reflects the registry only on the success path; 0 on withhold (meaning the
    # note carries the withhold, not a misleading 0).
    join = [_jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:x")]
    ok = crossplane_check(
        join, _st(), channelfinder=_FakeCF({"DEV-TEST01:Ctrl-EVR-01:y"}), cf_requested=True
    )
    assert ok.cf_registered == 1
    withheld = crossplane_check(join, _st(), channelfinder=_RaisingCF(), cf_requested=True)
    assert withheld.cf_registered == 0


def test_cf_empty_linked_no_unregistered() -> None:
    # No linked PVs (all other-prefix) → cf_unregistered empty even though CF has channels.
    join = [_jp("a.bob", "OTHER-SYS:z")]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:something"})
    report = crossplane_check(join, _st(), channelfinder=cf, cf_requested=True)
    assert report.cf_unregistered == ()


def test_cf_prefix_with_no_cf_channels_all_unregistered() -> None:
    # CF returns zero channels under the prefix → every linked record is cf_unregistered; this is
    # DISTINCT from a withhold: cf_registered=0 but NO withhold note.
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:a"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:b"),
    ]
    report = crossplane_check(join, _st(), channelfinder=_FakeCF(set()), cf_requested=True)
    assert set(report.cf_unregistered) == {
        "DEV-TEST01:Ctrl-EVR-01:a",
        "DEV-TEST01:Ctrl-EVR-01:b",
    }
    assert report.cf_registered == 0
    assert not any("withheld" in note.lower() for note in report.notes)


def test_cf_render_markdown_block() -> None:
    join = [
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:CmdMiss", role="write"),
        _jp("a.bob", "DEV-TEST01:Ctrl-EVR-01:served"),
    ]
    cf = _FakeCF({"DEV-TEST01:Ctrl-EVR-01:served"})
    markdown = render_markdown(crossplane_check(join, _st(), channelfinder=cf, cf_requested=True))
    assert "**Unregistered in ChannelFinder (linked PV, not in CF):** 1 of 1 registered" in markdown
    assert "of which writable: 1" in markdown
    assert "DEV-TEST01:Ctrl-EVR-01:CmdMiss" in markdown
