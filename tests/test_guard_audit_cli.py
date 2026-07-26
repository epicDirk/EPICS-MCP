"""S33: the audit tool's ``--check`` mode is exercised, not just described.

``scripts/guard_audit.py`` produces the figures that ``tests/test_client_edge_guards.py`` records.
Its AST half is imported and therefore covered; its CLI half was not run by anything, so a new
mode there would have shipped on the strength of a commit message. ``CONTRIBUTING.md`` asks for a
test with new behaviour, and this is it.

Three of the four run WITHOUT a coverage database, which is the point: recording one costs a full
suite run under ``COVERAGE_CORE=ctrace``, so the cheap half has to be checkable on its own or
nobody will ever check it. The fourth supplies a SYNTHETIC map, because DRIVING the reader costs
nothing even though RECORDING a real map does — conflating those two is how the expensive branch
nearly shipped untested. They also run IN-PROCESS rather than through a subprocess — a second
interpreter would import a second copy of the module and the substitution below would apply to an
object the code under test never reads, which is precisely the sham this audit exists to find.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import guard_audit  # noqa: E402 - needs sys.path above

_ARGV = ["guard_audit.py", "sham", "--check"]


def test_check_without_a_database_agrees_and_names_what_it_could_not_reach(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 0 on agreement — and the unreachable pins are named on the CLEAN run too.

    A checker that prints "OK" while half its pins were out of reach reads as a verdict about all
    of them. Naming the gap on every run, not only on failure, is what keeps the clean case from
    being over-read."""
    assert guard_audit.main(_ARGV) == 0
    reported = capsys.readouterr().err
    assert "NOT checked here" in reported
    for pin in guard_audit.PINNED_COVERAGE:
        assert pin in reported, f"a coverage-dependent pin was silently omitted: {pin}"
    # Without this a checker that compared NOTHING would satisfy every line above.
    assert f"checked {len(guard_audit.PINNED_AST)} pin(s)" in reported
    assert guard_audit.UNPINNED_VERDICT in reported, "the unpinnable verdict must be named too"


def test_check_reports_a_deviating_pin_by_name_and_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 1 means "a pin deviates" — and says WHICH, so the code is never the thing to hunt.

    The exit code alone would be ambiguous: 1 is also what an uncaught exception produces. The
    tool answers that by reserving 9 for a crash; this test pins the other half, that a 1 arrives
    with the deviating pin's name and both figures on stderr."""
    wrong = guard_audit.population()[guard_audit.DOUBLES] + 1
    monkeypatch.setitem(guard_audit.PINNED, guard_audit.DOUBLES, wrong)

    assert guard_audit.main(_ARGV) == 1
    reported = capsys.readouterr().err
    assert "PIN DEVIATION" in reported
    assert guard_audit.DOUBLES in reported
    assert f"pinned {wrong}" in reported, "the failure must show the pinned figure"
    measured = guard_audit.population()[guard_audit.DOUBLES]
    assert f"measured {measured}" in reported, "and the figure it actually measured"
    # The CHEAP recipe. An AST pin is re-measured in a second, so prescribing a full ctrace suite
    # would invert the cost split this whole design rests on.
    assert guard_audit.RERUN_AST in reported
    assert "COVERAGE_CORE=ctrace" not in reported


def test_check_with_a_database_compares_all_four_pins(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expensive half is exercised too — and it did not need the expensive input to be.

    The first version of this module skipped it, arguing that a coverage map costs a full ctrace
    suite run. That conflates RECORDING a real map with driving the code that CONSUMES one. Left
    untested, deleting the comparison would have stopped 102 and 20 from ever being checked while
    the gate stayed green — the exact shape of sham this tool exists to find.

    THE MAP MUST DISCRIMINATE, and the first version's did not. It carried the context ``"t::a"``,
    which ends with ``::a`` and therefore matches no test name in the estate, so ``executing`` never
    intersected the claiming tests and both figures came out exactly as they do for an EMPTY map.
    Measured: the whole guard-line intersection in ``cmd_sham`` could be deleted, or its ``in``
    inverted to ``not in``, and this test stayed green — a sham guard inside the tool that exists to
    find sham guards. The context below names a test that really is in the population, so the two
    coverage figures must come out one lower than the AST pair, and the assertions say so.

    Asserting the VALUES rather than the pin names is the other half. "Both names appear on stderr"
    is satisfied by a computation that returns constants; "measured 106" is not.

    Honest limit, because the substitution below is easy to over-read: it replaces
    ``load_coverage_map``, so what is exercised is its CONSUMER. The reader itself — which must
    query the ``arc`` table, since ``line_bits`` yields a silently empty map — is still covered by
    no test at all.
    """
    # A real member of the claiming population, and one that carries payload vocabulary, so
    # excluding it must move BOTH coverage figures by exactly one. If it ever leaves the population
    # the arithmetic below is wrong, so that is checked rather than assumed.
    covering = "tests/test_olog.py::test_search_bad_time_is_not_a_connection_error"
    claimed = {
        name: edge for entries in guard_audit.claiming_tests().values() for name, edge in entries
    }
    assert claimed.get(covering.rsplit("::", 1)[1]) is True, (
        f"{covering} is no longer a claiming test with payload vocabulary; this test's -1 "
        "arithmetic assumes it is. Pick another member of the population."
    )

    monkeypatch.setattr(
        guard_audit, "load_coverage_map", lambda _path: {("olog_client.py", 211): {covering}}
    )
    counted = guard_audit.population()
    # The deviation is MANUFACTURED, not hoped for. The first version of this test asserted exit 1
    # and happened to get it because the synthetic figures disagreed with the real pins; correcting
    # the population made them agree, and a test that had proved something became a test that
    # failed for a reason unrelated to its subject. What is under test is what the tool MEASURES,
    # so the pins are moved out of the way and the measured values are read off the report.
    offsets = {
        guard_audit.NOT_EXECUTING: counted[guard_audit.DOUBLES] - 1,
        guard_audit.SHAM_CANDIDATES: counted[guard_audit.EDGE_VOCABULARY] - 1,
    }
    for name, measured in offsets.items():
        monkeypatch.setitem(guard_audit.PINNED, name, measured + 7)

    assert guard_audit.main([*_ARGV, "--coverage-db", "synthetic"]) == 1
    reported = capsys.readouterr().err
    assert "NOT checked here" not in reported, "with a map, nothing should be deferred"
    assert "PIN DEVIATION" in reported
    for name, measured in offsets.items():
        expected = f"{name}: pinned {measured + 7}, measured {measured}"
        assert expected in reported, (
            f"the map excludes exactly one claiming test, so this line must appear: {expected!r}"
        )


def test_a_coverage_context_is_matched_by_file_and_function(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test is identified by the file it lives in and the function it is — not by a suffix.

    Both halves were wrong and both were measured on a real ctrace map. The phase was stripped by
    splitting on the FIRST pipe, which truncates a node id that contains one (six do here, e.g. a
    parametrised case whose id ends in ``[level-'|']``). And the claiming test was looked up with
    ``endswith("::" + name)``, which ignores the file and cannot match a parametrised id at all:
    that credited ``test_olog_write.py::test_unknown_level_refused`` — a candidate carrying payload
    vocabulary — with a SAME-NAMED test's guard-line execution in another file, and so kept it out
    of the sham-candidate list this audit exists to produce.

    Three properties, each driven through ``cmd_sham`` rather than asserted on a helper alone: the
    phase suffix is removed and the embedded pipe survives; a parametrised id still identifies its
    function; and the same function name in a DIFFERENT file is a different test.
    """
    mine = "tests/test_olog.py::test_search_bad_time_is_not_a_connection_error[level-'|']"
    impostor = "tests/test_olog_update.py::TestServiceUpdate::" + mine.rpartition("::")[2]
    assert guard_audit._node_id_of(f"{mine}|run") == mine, "the phase is the LAST segment"
    assert guard_audit.test_identity(mine) == (
        "test_olog.py",
        "test_search_bad_time_is_not_a_connection_error",
    )
    assert guard_audit.test_identity(impostor)[0] == "test_olog_update.py"

    counted = guard_audit.population()[guard_audit.DOUBLES]
    for context, expected in ((mine, counted - 1), (impostor, counted)):
        monkeypatch.setattr(
            guard_audit,
            "load_coverage_map",
            lambda _path, hit=context: {("olog_client.py", 211): {hit}},
        )
        monkeypatch.setitem(guard_audit.PINNED, guard_audit.NOT_EXECUTING, expected + 7)
        assert guard_audit.main([*_ARGV, "--coverage-db", "synthetic"]) == 1
        line = f"{guard_audit.NOT_EXECUTING}: pinned {expected + 7}, measured {expected}"
        assert line in capsys.readouterr().err, (
            f"the context {context!r} must {'' if expected < counted else 'NOT '}exclude its "
            f"same-named claiming test, so this line is required: {line!r}"
        )


def test_a_crash_exits_nine_and_prints_its_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 9 is the whole reason exit 1 can mean "a pin deviates" — and nothing asserted it.

    273c1c5 moved the crash code off 3 (which ``sweep`` uses for a detected foreign write, this
    tool's most safety-critical finding) and announced the contract as delivered. Measured by an
    outside QA: changing ``return 9`` back to ``return 1`` left the whole lane green, so the
    contract was prose. A wrapper reading exit 1 would have reported a fabricated pin deviation
    from a traceback.
    """
    monkeypatch.setattr(
        guard_audit, "population", lambda: (_ for _ in ()).throw(RuntimeError("deliberate crash"))
    )
    assert guard_audit.main(_ARGV) == 9, (
        "a crash must not wear the code that means 'a pin deviates'"
    )
    reported = capsys.readouterr().err
    assert "RuntimeError: deliberate crash" in reported, "and it must say what crashed"
    assert "PIN DEVIATION" not in reported


def test_a_blind_map_still_compares_the_pins_it_already_measured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable map must not silently take the CHEAP pins down with it.

    ``cmd_sham`` computes the AST pair before it opens the database, then used to return 2 without
    comparing anything — so a caller could not tell "your map is unusable" from "your map is
    unusable AND the population moved", and the two figures already in hand were thrown away.
    Exit 2 is still what the map problem decides; what changed is that the cheap half is reported.
    """
    monkeypatch.setattr(guard_audit, "load_coverage_map", lambda _path: {})
    wrong = guard_audit.population()[guard_audit.DOUBLES] + 3
    monkeypatch.setitem(guard_audit.PINNED, guard_audit.DOUBLES, wrong)

    assert guard_audit.main([*_ARGV, "--coverage-db", "unusable"]) == 2
    reported = capsys.readouterr().err
    assert "empty coverage map" in reported
    assert "PIN DEVIATION" in reported, "the pins it had already measured must still be compared"
    assert f"pinned {wrong}" in reported
    for pin in guard_audit.PINNED_COVERAGE:
        assert pin in reported, f"and the pins it could not reach must be named: {pin}"


def test_the_candidate_list_is_pinned_by_its_members_not_only_its_length(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict about a LIST is not guarded by a count of it.

    ``UNPINNED_VERDICT`` has said since S33 that "a list of the same length with different members
    satisfies every pin below", and nothing acted on it. Now a name that was never read is named,
    and the exit code says so — swapping one member for another keeps every figure identical and
    must still stop the run.
    """
    swapped_out = guard_audit.PINNED_CANDIDATES[-1]
    replaced = (*guard_audit.PINNED_CANDIDATES[:-1], "test_zz.py::test_never_read_by_anyone")
    monkeypatch.setattr(guard_audit, "PINNED_CANDIDATES", replaced)
    # An empty covering set: no claiming test executes a guard line, so sham_edge is the whole
    # real candidate list and the COUNTS are unchanged. Only the membership differs.
    monkeypatch.setattr(
        guard_audit, "load_coverage_map", lambda _path: {("olog_client.py", 211): set()}
    )

    assert guard_audit.main([*_ARGV, "--coverage-db", "synthetic"]) == 1
    reported = capsys.readouterr().err
    assert "CANDIDATE LIST CHANGED" in reported
    assert f"+ {swapped_out}" in reported, "the name that was never read must be named"
    assert "- test_zz.py::test_never_read_by_anyone" in reported
    assert "PIN DEVIATION" not in reported, "the counts agree; it is the membership that does not"


def test_the_candidate_list_is_printable_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``RERUN_AST``'s first instruction has to be executable at AST cost, or it is not cheap.

    It says "re-read the candidate list". That list used to be printed only on the --coverage-db
    path, so the recipe for a one-second deviation demanded a full COVERAGE_CORE=ctrace suite run
    — the exact inversion the cheap/expensive split exists to prevent.
    """
    assert guard_audit.main(["guard_audit.py", "sham", "--list-candidates"]) == 0
    reported = capsys.readouterr().err
    assert "COVERAGE_CORE=ctrace" not in reported, "the cheap path must not demand the map"
    listed = [line.strip() for line in reported.splitlines() if "::" in line]
    assert len(listed) == guard_audit.population()[guard_audit.EDGE_VOCABULARY]
    assert guard_audit.RERUN_AST.startswith("re-read the candidate list (sham --list-candidates")


def test_reporting_mode_still_demands_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    """Without ``--check`` the old contract is untouched, message and exit code included.

    ``--coverage-db`` had to lose ``required=True`` so ``--check`` could run without one. That
    silently turns a clean argparse refusal into a crash unless the refusal is restated by hand,
    so the restatement is pinned here rather than assumed."""
    with pytest.raises(SystemExit) as exit_info:
        guard_audit.main(["guard_audit.py", "sham"])
    assert exit_info.value.code == 2
    assert "the following arguments are required: --coverage-db" in capsys.readouterr().err
