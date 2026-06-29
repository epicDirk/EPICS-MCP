"""Offline tests for the pure coverage join (no network, no opi_navigation)."""

from __future__ import annotations

from epics_pv_mcp.services.coverage import IndexRow, audit_coverage
from epics_pv_mcp.services.crossplane import CFRegistryCapped


def _row(pv: str, *, displays: tuple[str, ...] = ("d.bob",), role: str = "read") -> IndexRow:
    return IndexRow(pv=pv, protocol="ca", displays=displays, roles=(role,))


class _FakeCF:
    def __init__(self, names: set[str]) -> None:
        self._names = set(names)

    def registered_under(self, prefix: str) -> set[str]:
        return {n for n in self._names if n.startswith(prefix)}


class _CappedCF:
    def registered_under(self, prefix: str) -> set[str]:
        raise CFRegistryCapped("capped")


class _RaisingCF:
    def registered_under(self, prefix: str) -> set[str]:
        raise RuntimeError("boom")


class _FakeArchiver:
    def __init__(self, archived: set[str]) -> None:
        self._archived = set(archived)

    def is_archived(self, pv: str) -> bool:
        return pv in self._archived


class _PerPvRaisingArchiver:
    def __init__(self, fail_pv: str) -> None:
        self._fail_pv = fail_pv

    def is_archived(self, pv: str) -> bool:
        if pv == self._fail_pv:
            raise RuntimeError("timeout")
        return True


class _FakeAlarm:
    def __init__(self, alarmed: set[str]) -> None:
        self._alarmed = set(alarmed)

    def is_alarm_configured(self, pv: str) -> bool:
        return pv in self._alarmed


# --- set-diff matrix ---


def test_set_diffs() -> None:
    index = [_row("DEV:A"), _row("DEV:B")]  # D = {A, B}
    cf = _FakeCF({"DEV:A", "DEV:C"})  # C = {A, C}
    report = audit_coverage(index, scope="DEV:", channelfinder=cf, cf_requested=True)
    assert report.cf_and_display == ("DEV:A",)
    assert report.cf_only == ("DEV:C",)  # registered, no screen = blind-spot
    assert report.display_only == ("DEV:B",)  # shown, not registered
    assert "channelfinder" in report.planes_live


def test_record_name_normalized_both_sides() -> None:
    # D PV carries a field suffix (...SP.EGU); C has the bare record (...SP) → cf_and_display, NOT
    # a false display_only.
    report = audit_coverage(
        [_row("DEV:SP.EGU")], scope="DEV:", channelfinder=_FakeCF({"DEV:SP"}), cf_requested=True
    )
    assert report.cf_and_display == ("DEV:SP",)
    assert report.display_only == ()


# --- critical_uncovered: proven gap vs withheld gap (withheld != no, H2) ---


def test_proven_gap_in_critical() -> None:
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A"}),
        cf_requested=True,
        archived=_FakeArchiver(set()),  # archived=no
        archive_requested=True,
    )
    assert report.rows[0].archived == "no"
    assert "DEV:A" in report.critical_uncovered
    assert "DEV:A" in report.unarchived


def test_per_pv_archiver_failure_withheld_not_a_gap() -> None:
    # A delivered PV whose per-PV Archiver query fails → that cell withheld; the plane STAYS live;
    # the PV is NOT critical_uncovered (a withheld gap is never a gap, H2).
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A"}),
        cf_requested=True,
        archived=_PerPvRaisingArchiver(fail_pv="DEV:A"),
        archive_requested=True,
        alarmed=_FakeAlarm({"DEV:A"}),
        alarm_requested=True,
    )
    row = report.rows[0]
    assert row.registered_cf == "yes"
    assert row.has_display == "yes"
    assert row.archived == "withheld"  # per-PV query failure
    assert row.alarmed == "yes"
    assert "DEV:A" not in report.critical_uncovered  # only withheld gap, no proven gap
    assert "DEV:A" not in report.unarchived
    assert "archiver" in report.planes_live  # plane is still live (partial failure)
    assert any("withheld for 1 PV" in n for n in report.notes)


def test_plane_disabled_all_withheld_not_a_gap() -> None:
    report = audit_coverage(
        [_row("DEV:A")], scope="DEV:", channelfinder=_FakeCF({"DEV:A"}), cf_requested=True
    )  # archived/alarmed disabled
    row = report.rows[0]
    assert row.archived == "withheld"
    assert row.alarmed == "withheld"
    assert "archiver" not in report.planes_live
    assert "alarm" not in report.planes_live
    assert "DEV:A" not in report.critical_uncovered  # withheld gaps never count


def test_requested_but_url_unset_notes() -> None:
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A"}),
        cf_requested=True,
        archive_requested=True,  # but no archived checker
        alarm_requested=True,  # but no alarmed checker
    )
    assert any("Archiver check requested" in n for n in report.notes)
    assert any("Alarm check requested" in n for n in report.notes)


# --- ChannelFinder is the anchor: disabled / capped / failed → no cf verdicts ---


def test_cf_disabled_only_display_set() -> None:
    report = audit_coverage([_row("DEV:A")], scope="DEV:")  # no CF
    assert report.cf_and_display == ()
    assert report.cf_only == ()
    assert report.display_only == ()
    assert report.critical_uncovered == ()
    assert len(report.rows) == 1  # D remains
    assert report.rows[0].registered_cf == "withheld"
    assert report.rows[0].has_display == "yes"
    assert "channelfinder" not in report.planes_live
    assert any("ChannelFinder disabled" in n for n in report.notes)


def test_cf_capped_withheld() -> None:
    report = audit_coverage(
        [_row("DEV:A")], scope="DEV:", channelfinder=_CappedCF(), cf_requested=True
    )
    assert report.cf_capped is True
    assert report.cf_and_display == ()
    assert report.rows[0].registered_cf == "withheld"
    assert any("capped" in n for n in report.notes)


def test_cf_query_failure_withheld() -> None:
    report = audit_coverage(
        [_row("DEV:A")], scope="DEV:", channelfinder=_RaisingCF(), cf_requested=True
    )
    assert report.cf_capped is False
    assert report.rows[0].registered_cf == "withheld"
    assert any("query failed" in n for n in report.notes)


# --- displays incomplete: a not-in-D delivered PV is withheld, never a false blind-spot ---


def test_blind_spot_when_displays_complete() -> None:
    # DEV:B registered but on no screen, displays complete → has_display=no = a real blind-spot.
    report = audit_coverage(
        [_row("DEV:A")], scope="DEV:", channelfinder=_FakeCF({"DEV:A", "DEV:B"}), cf_requested=True
    )
    b = next(r for r in report.rows if r.pv == "DEV:B")
    assert b.has_display == "no"
    assert "DEV:B" in report.blind_spots
    assert "DEV:B" in report.critical_uncovered  # delivered + proven display gap


def test_context_capped_withholds_has_display() -> None:
    # Same as above but a display hit the context cap → DEV:B's absence is unprovable → withheld
    # (never a false blind-spot).
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A", "DEV:B"}),
        cf_requested=True,
        context_capped=("capped.bob",),
    )
    b = next(r for r in report.rows if r.pv == "DEV:B")
    assert b.has_display == "withheld"
    assert "DEV:B" not in report.blind_spots
    assert "DEV:B" not in report.critical_uncovered
    assert report.displays_incomplete == ("capped.bob",)
    assert any("context cap" in n for n in report.notes)


def test_ratio_caveat_fires_at_half() -> None:
    index = [_row("DEV:A"), _row("DEV:B"), _row("DEV:C"), _row("DEV:D")]  # D = 4
    report = audit_coverage(
        index, scope="DEV:", channelfinder=_FakeCF({"DEV:A"}), cf_requested=True
    )  # display_only = 3/4 >= 0.5
    assert len(report.display_only) == 3
    assert any(">= 50%" in n for n in report.notes)


def test_scope_filters_display_set() -> None:
    index = [_row("DEV:A"), _row("OTHER:X")]
    report = audit_coverage(
        index, scope="DEV:", channelfinder=_FakeCF({"DEV:A"}), cf_requested=True
    )
    assert [r.pv for r in report.rows] == ["DEV:A"]  # OTHER:X filtered out by scope
    assert report.cf_and_display == ("DEV:A",)


def test_unscoped_warns_cap_risk() -> None:
    report = audit_coverage(
        [_row("DEV:A")], scope="", channelfinder=_FakeCF({"DEV:A"}), cf_requested=True
    )
    assert any("Unscoped audit" in n for n in report.notes)
