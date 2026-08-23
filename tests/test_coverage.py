"""Offline tests for the pure coverage join (no network, no opi_navigation)."""

from __future__ import annotations

from epics_mcp.services.coverage import IndexRow, audit_coverage, render_markdown
from epics_mcp.services.crossplane import CFRegistryCapped


def _row(
    pv: str,
    *,
    displays: tuple[str, ...] = ("d.bob",),
    role: str = "read",
    trends: tuple[str, ...] = (),
) -> IndexRow:
    """Build an IndexRow (default: shown on one operator screen, read).

    ``displays`` is the whole; whatever is not named in *trends* is a screen. Defaulted here, in
    one test helper, rather than on the fields themselves: a row whose kinds nobody stated must be
    impossible in ``src``, while a test that is not about kinds should not have to say so at every
    call. GQ-153.
    """
    return IndexRow(
        pv=pv,
        displays=displays,
        screens=tuple(d for d in displays if d not in trends),
        trends=tuple(d for d in displays if d in trends),
        roles=(role,),
    )


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


class _PerPvRaisingAlarm:
    """The Alarm twin of :class:`_PerPvRaisingArchiver`, absent until GB-65.

    Its absence is why ``_coverage_notes``' ``alarm_withheld`` branch had never been executed by
    any test: every alarm double here answers cleanly, so a per-PV alarm failure could not be
    expressed at all, and the note that reports it could have been deleted unnoticed.
    """

    def __init__(self, fail_pv: str) -> None:
        self._fail_pv = fail_pv

    def is_alarm_configured(self, pv: str) -> bool:
        if pv == self._fail_pv:
            raise RuntimeError("timeout")
        return True


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


def test_audit_coverage_merges_same_record_rows_order_independent() -> None:
    """S5-6 / merge-branch lock: two index rows that normalize to the SAME record (DEV:SP and
    DEV:SP.EGU) must merge into ONE row with UNIONED displays and roles, independent of input
    order. Exercises the field-suffix merge branch (coverage.py:187-198), which no prior test
    entered (every other multi-row test used distinct records)."""
    rows_a = [
        _row("DEV:SP", displays=("a.bob",), role="read"),
        _row("DEV:SP.EGU", displays=("b.bob",), role="write"),
    ]
    rows_b = list(reversed(rows_a))
    cf = _FakeCF({"DEV:SP"})
    report_a = audit_coverage(rows_a, scope="DEV:", channelfinder=cf, cf_requested=True)
    report_b = audit_coverage(rows_b, scope="DEV:", channelfinder=cf, cf_requested=True)

    assert report_a == report_b  # order-independent (S5-6)
    merged = [r for r in report_a.rows if r.pv == "DEV:SP"]
    assert len(merged) == 1
    assert merged[0].displays == ("a.bob", "b.bob")  # unioned + sorted
    assert merged[0].roles == ("read", "write")


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
    # GB-65: anchored on the PLANE, not just on "withheld for 1 PV". That substring matches
    # the alarm note word for word, so it could not tell the two apart, and the alarm one had
    # no test of its own to notice.
    assert any("'archived' withheld for 1 PV(s)" in n for n in report.notes), report.notes
    assert not any("'alarmed' withheld" in n for n in report.notes), report.notes


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
    # The full wording, not the bare substring "context cap" this used to accept (GB-72). This
    # note said only "the context cap" while every sibling tool and this tool's own context_cap
    # description said "per-display", so a reader searching for the documented phrase missed it.
    # A substring assertion survives any rewording that keeps two words, which is why the one
    # wording is pinned here as well as in tests/test_diagnostics_tail.py: that file guards
    # against a FIFTH place inventing a name, this line guards what this tool really emits.
    assert any("hit the per-display context cap" in n for n in report.notes), report.notes


def test_glob_capped_is_reported_as_a_lower_bound() -> None:
    """The inventory's SECOND incompleteness signal reaches the caller, not just the first.

    ``context_capped`` has had the test above since this audit shipped; the glob cap has been
    wired the whole time and pinned nowhere, while its twin in the sibling service IS pinned
    (``test_crossplane.py::test_notes_glob_capped_and_needs_msi``). That asymmetry is the defect:
    two services emit the same sentence from the same adapter value, and only one of them would
    have noticed the sentence disappearing.

    Deliberately a note and not a withhold, unlike the context cap above. A capped glob drops
    embedded SCREENS, so it shrinks the display set D as a whole rather than making one named
    display's PV list incomplete; there is no per-PV verdict to withhold, and D is reported as a
    lower bound instead. Provably red: drop the ``glob_capped_count`` block from
    :func:`_coverage_notes`.
    """
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A"}),
        cf_requested=True,
        glob_capped_count=2,
    )
    # The full opening, not the bare substring "glob cap" this used to accept (GB-78). The word
    # "globbed" is load-bearing and has been wrong once: 05b5fc2 had to replace "template <file>
    # reference(s)" here and in two siblings, because the engine fills glob_capped from
    # glob-resolved references and skips template edges. A substring assertion would have passed
    # throughout. The wording across all four is pinned in tests/test_diagnostics_tail.py.
    assert any("globbed <file> reference(s) hit the glob cap" in n for n in report.notes), (
        report.notes
    )
    assert any("lower bound" in n for n in report.notes), report.notes
    # The two signals are independent: no display hit the context cap here, so the count must not
    # leak into the field that names capped displays.
    assert report.displays_incomplete == ()


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


# --- GB-65: three honest-caveat notes that no test held ---


def test_cf_requested_without_a_checker_names_the_unset_url() -> None:
    """The "requested but not configured" note, which no test had ever REACHED.

    Measured before GB-65: every ``cf_requested=True`` call in this file also passes a checker, so
    the branch that fires when ChannelFinder was asked for and no URL is set had never executed.
    It is a different statement from the "disabled" note below it, and conflating the two is the
    exact confusion the split exists to prevent: one says "you asked and it is not configured",
    the other "you did not ask". Provably red: drop the ``cf_requested`` branch from
    :func:`_coverage_notes`.
    """
    report = audit_coverage([_row("DEV:A")], scope="DEV:", cf_requested=True)

    assert any("ChannelFinder check requested" in n for n in report.notes), report.notes
    assert any("EPICS_MCP_CHANNELFINDER_URL is unset" in n for n in report.notes), report.notes
    # NOT the other branch: "requested but unset" and "disabled" must stay distinguishable.
    assert not any("ChannelFinder disabled" in n for n in report.notes), report.notes


def test_per_pv_alarm_failure_gets_its_own_withheld_note() -> None:
    """The alarm half of the per-PV withhold, which had no test and no test DOUBLE.

    Its archiver twin has been pinned since this audit shipped; this one could not even be
    expressed, because every alarm double in this file answers cleanly (see
    :class:`_PerPvRaisingAlarm`). The asymmetry IS the defect, the same shape GB-29 found on the
    glob-cap note. Provably red: drop the ``alarm_withheld`` branch from :func:`_coverage_notes`.
    """
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A"}),
        cf_requested=True,
        alarmed=_PerPvRaisingAlarm(fail_pv="DEV:A"),
        alarm_requested=True,
    )

    assert report.rows[0].alarmed == "withheld"
    assert any("'alarmed' withheld for 1 PV(s)" in n for n in report.notes), report.notes
    # The archiver plane was not asked here, so its twin note must NOT appear.
    assert not any("'archived' withheld" in n for n in report.notes), report.notes


def test_a_withheld_gap_is_named_as_excluded_from_critical_uncovered() -> None:
    """The note behind the H2 rule "a withheld cell is never a gap".

    The rule itself is pinned by ``test_per_pv_archiver_failure_withheld_not_a_gap`` through the
    FIELD (``critical_uncovered``); the sentence that tells a reader WHY the PV is absent from
    that list was not. Deleting it leaves a report whose exclusion is invisible. Provably red:
    drop the ``withheld_gap_excluded`` branch from :func:`_coverage_notes`.
    """
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

    assert "DEV:A" not in report.critical_uncovered
    assert any("have a withheld gap and no proven gap" in n for n in report.notes), report.notes
    assert any("1 delivered PV(s)" in n for n in report.notes), report.notes


def test_the_cap_warning_stands_in_the_head_above_every_gap_figure() -> None:
    """A capped display is announced BEFORE the numbers it makes a lower bound, not after them.

    Position is the whole point of this guard, so it asserts an ORDER, not a substring: the cap
    warning used to sit under the figures, in the Notes and in a bare counting line, and a reader
    who takes ``critical_uncovered`` at face value has drawn the conclusion by the time the caveat
    arrives. The warning names the consequence (``has_display`` withheld) rather than the count
    alone, because the count answers "how many displays", not "what does this report still mean".

    ⚠ NO percentage HERE, and the reason narrowed rather than disappeared. It used to be "no
    denominator exists at all"; since [GQ-16] the engine reports the file universe it walked, and
    with one the head line does carry a share (``test_the_cap_line_reports_the_share_of_the_walked
    _universe``). What stays true is the case this vector builds: no universe reported, so nothing
    to divide by, and inventing one is the defect the old wording was written against. The
    forbidden denominator is still forbidden: ``len(display set D)`` puts a capped PV-less file in
    the numerator and not in the denominator, so that share could exceed 100%.

    RED-PROOF: move the warning back below ``critical_uncovered`` and the order assertion fails;
    drop the consequence clause and the wording assertion fails.
    """
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A", "DEV:B"}),
        cf_requested=True,
        context_capped=("capped.bob",),
    )
    markdown = render_markdown(report)

    assert "hit the per-display context cap" in markdown
    assert "LOWER BOUND" in markdown
    assert "capped.bob" in markdown
    warning_at = markdown.index("hit the per-display context cap")
    figures_at = markdown.index("critical_uncovered (delivered + proven gap)")
    assert warning_at < figures_at, (
        "the cap warning must stand ABOVE the gap figures it qualifies, "
        f"found at {warning_at} against {figures_at}"
    )
    line_end = markdown.index(chr(10), warning_at)
    head_line = markdown[warning_at:line_end]
    assert "%" not in head_line, (
        "no file universe was reported, so the cap line must not claim a share. Inventing a "
        f"denominator is the defect render_markdown's comment records. Line: {head_line!r}"
    )


def test_the_cap_line_reports_the_share_of_the_walked_universe() -> None:
    """With the universe reported, the cap COUNT becomes a share, against the right denominator.

    [GQ-16]. A bare "23 displays hit the cap" reads very differently against 30 walked files than
    against 3000, and the reader had no way to tell which. The engine half of this point exports
    ``displays_walked``/``trends_walked``; this is the consumer half.

    ⛔ The denominator is the FILE UNIVERSE, never ``len(display set D)``. Measured on the engine
    side over two datasets: ``context_capped`` is a subset of the universe on both, while on one of
    them 6 of 220 capped files sat outside the display list, which is why that share could exceed
    100%. Structural rather than lucky: a target outside ``known`` is discarded before it can be
    capped.

    RED-PROOF: divide by ``len(report.cf_and_display)`` or by any display-derived count and the
    number below changes; drop the share and the assertion fails outright.
    """
    report = audit_coverage(
        [_row("DEV:A")],
        scope="DEV:",
        channelfinder=_FakeCF({"DEV:A"}),
        cf_requested=True,
        context_capped=("capped.bob", "other.bob"),
        files_walked=8,
    )
    markdown = render_markdown(report)
    warning_at = markdown.index("hit the per-display context cap")
    head_line = markdown[
        markdown.rindex(chr(10), 0, warning_at) : markdown.index(chr(10), warning_at)
    ]

    assert "25.0%" in head_line, f"2 of 8 walked files is 25.0%; line was {head_line!r}"
    # ⛔ Both halves of the ratio name the SAME population, and "display(s)" did not. The engine
    # seeds its walk over displays AND trends and resolves a trend edge in `known_trends`, so a
    # `.plt` can be capped: the numerator spans both kinds. Calling it "display(s)" over a
    # denominator of files compared two populations in the label while the arithmetic was right.
    # Found by the post-build review; measured latent (no capped `.plt` on any reachable dataset).
    assert "file(s)" in head_line and "display(s)" not in head_line, head_line
    # The denominator is NAMED, not only applied: a share whose base the reader cannot see is the
    # same unanswerable question as the bare count it replaces.
    assert "8 files this walk visited" in head_line
    assert report.files_walked == 8


# --- GQ-153: the three file tuples, and what happens to a kind nobody taught this report --------


def test_a_rows_two_kind_tuples_never_exceed_its_whole_file_list() -> None:
    """The invariant the split rests on: ``screens | trends`` is a SUBSET of ``displays``.

    Stated in ``IndexRow``'s docstring and worth a guard rather than a promise, because the three
    are carried separately on purpose (a derived union would drop a file of an unknown kind
    entirely, name and all). Separate carriers can disagree, and this is the direction that would
    matter: a name appearing in a kind tuple and not in the whole would be an invented file.
    """
    rows = [
        _row("SIM:A", displays=("s.bob", "t.plt"), trends=("t.plt",)),
        _row("SIM:B", displays=("s.bob",)),
    ]
    report = audit_coverage(rows, scope="SIM:")

    for row in report.rows:
        assert set(row.screens) | set(row.trends) <= set(row.displays), (
            f"{row.pv} names a file in a kind tuple that is not in its own displays list"
        )


def test_a_file_of_an_unknown_kind_is_named_rather_than_counted_as_a_screen() -> None:
    """A third ``NodeKind`` must fall out of BOTH tuples and be reported, never absorbed.

    ``NodeKind`` has exactly two members today, so this is the branch that has no natural fixture
    and would otherwise ship unexercised: the row below carries a file in ``displays`` and in
    neither projection, which is exactly what the adapter produces the day the engine grows a kind
    this server has not been taught. The wrong behaviour is silent: counting it as a screen would
    make ``has_display`` say yes about a file nobody here can name.
    """
    unknown = IndexRow(
        pv="SIM:A", displays=("mystery.xyz",), screens=(), trends=(), roles=("read",)
    )
    report = audit_coverage([unknown], scope="SIM:")

    row = next(r for r in report.rows if r.pv == "SIM:A")
    assert row.has_display == "no", "an unnameable file must not answer for a screen"
    assert row.on_trend == "no"
    assert row.displays == ("mystery.xyz",), "the file must not be dropped from the answer"
    assert any("mystery.xyz" in note for note in report.notes), (
        "a file kind this report cannot name has to be SAID, not silently ignored"
    )
    # And it is not a trend either: trend_only is built positively, so it stays out of both.
    assert report.trend_only == ()
