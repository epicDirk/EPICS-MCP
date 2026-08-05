"""Tests for the pure readback-verification core (:mod:`epics_mcp.readback`).

No network, no clock, no env, :func:`verify_readback` is a total function over (written string,
readback dict, tolerance). These cover the four verdict classes (ok / mismatch / not-verifiable /
type-coercion) plus the two tolerance sources (live ``min_step`` vs. the epsilon fallback) and the
magnitude-safety property that motivated ``math.isclose``.

Red-provability (Evidence #5): the mismatch and magnitude tests go red under a mutant that inverts
the comparison or replaces the tolerance with ``inf``, a guard that cannot go red is the defect.

WHY THIS FILE REACHES FOR p4p
-----------------------------
The enum cases below build their readback by round-tripping through ``p4p.nt`` and
``services.epics_client._format_value`` instead of typing the dict out. A test that invents its own
data shape proves nothing about the shape the client actually delivers, and this module carried
exactly that defect: a hand-built ``{"value": "OPEN"}`` looked like it covered the exact-compare
branch for enum labels, while a real enum readback carries an int index and never reaches it.
Locally wrapping and unwrapping was measured (2026-08-05) to produce the same dict as a ``get`` from
a real PVA server, in four configurations including a fresh context that never saw the initial
struct, so the round trip needs no server and no network.
"""

from __future__ import annotations

from collections.abc import Sequence

from p4p.nt import NTEnum, NTScalar

from epics_mcp.readback import ReadbackVerification, verify_readback
from epics_mcp.services.epics_client import _format_value

_EPS = 1e-6


def _enum_readback(index: int, choices: Sequence[str]) -> dict[str, object]:
    """A readback dict in the shape the real client produces for an enum PV (index + enum block)."""
    nt = NTEnum()
    return _format_value("TEST:PV", nt.unwrap(nt.wrap({"index": index, "choices": list(choices)})))


def _string_readback(value: str) -> dict[str, object]:
    """A readback dict in the shape the real client produces for a string PV (a bare str value)."""
    nt = NTScalar("s")
    return _format_value("TEST:PV", nt.unwrap(nt.wrap(value)))


class TestNumericVerified:
    """A numeric readback within tolerance verifies True; a written string is coerced to float."""

    def test_exact_roundtrip_verifies(self) -> None:
        # "81" written, 81.0 read back (the measured sandbox roundtrip: string in, float out).
        v = verify_readback("81", {"pv_name": "X", "value": 81.0}, _EPS)
        assert v.verified is True
        assert v.readback == 81.0
        assert v.tolerance == _EPS

    def test_within_epsilon_verifies(self) -> None:
        v = verify_readback("81.0000001", {"pv_name": "X", "value": 81.0}, _EPS)
        assert v.verified is True


class TestNumericMismatch:
    """A numeric readback outside tolerance is a genuine mismatch (verified False), never None."""

    def test_clear_mismatch(self) -> None:
        # Red-provable: inverting the compare in verify_readback flips this to True.
        v = verify_readback("20", {"pv_name": "X", "value": 10.0}, _EPS)
        assert v.verified is False
        assert v.note is not None and "!=" in v.note

    def test_just_outside_epsilon(self) -> None:
        v = verify_readback("81.5", {"pv_name": "X", "value": 81.0}, _EPS)
        assert v.verified is False


class TestToleranceSource:
    """min_step > 0 is the tolerance; min_step == 0 / absent falls back to the epsilon."""

    def test_min_step_used_when_positive(self) -> None:
        rb = {"pv_name": "X", "value": 80.0, "control": {"min_step": 0.5}}
        assert verify_readback("80.2", rb, _EPS).verified is True  # 0.2 <= 0.5
        assert verify_readback("80.7", rb, _EPS).verified is False  # 0.7 > 0.5

    def test_min_step_zero_falls_back_to_epsilon(self) -> None:
        # The sandbox PV Temp1ThrUpCrt-SP has a real min_step == 0.0 → epsilon is the normal path.
        rb = {"pv_name": "X", "value": 81.0, "control": {"min_step": 0.0}}
        v = verify_readback("81", rb, _EPS)
        assert v.verified is True
        assert v.tolerance == _EPS  # min_step was NOT used

    def test_min_step_absent_falls_back_to_epsilon(self) -> None:
        v = verify_readback("81.5", {"pv_name": "X", "value": 81.0}, _EPS)
        assert v.verified is False  # 0.5 > 1e-6


class TestMagnitudeSafety:
    """The property that motivated math.isclose: tolerance scales with magnitude (relative),
    so a large-magnitude roundtrip is not a false mismatch, a flat absolute epsilon would fail."""

    def test_large_magnitude_relative_tolerance(self) -> None:
        # 1e6 with a 0.05 absolute difference: rel_tol=1e-6 → 1.0 abs allowance → matches.
        # A flat abs_tol of 1e-6 (the naive design) would report a mismatch here.
        v = verify_readback("1000000.05", {"pv_name": "X", "value": 1_000_000.0}, _EPS)
        assert v.verified is True


class TestNotVerifiable:
    """value None (+note) or a non-coercible string → verified None, never a mismatch."""

    def test_value_none_with_note(self) -> None:
        rb = {"pv_name": "X", "value": None, "note": "value extraction failed; value withheld"}
        v = verify_readback("81", rb, _EPS)
        assert v.verified is None
        assert v.readback is None
        assert v.note is not None and "not verifiable" in v.note

    def test_value_none_without_note(self) -> None:
        v = verify_readback("81", {"pv_name": "X", "value": None}, _EPS)
        assert v.verified is None

    def test_noncoercible_string_against_numeric(self) -> None:
        v = verify_readback("abc", {"pv_name": "X", "value": 42.0}, _EPS)
        assert v.verified is None
        assert v.readback == 42.0


class TestNonNumeric:
    """A string readback compares exactly, without a tolerance.

    Deliberately NOT the enum case: an enum readback carries an int index, so an enum label is
    answered by the enum stage and never arrives here. ``TestEnumReadback`` covers that, on a shape
    built by the client rather than by this file.
    """

    def test_string_match(self) -> None:
        v = verify_readback("OPEN", {"pv_name": "X", "value": "OPEN"}, _EPS)
        assert v.verified is True
        assert v.tolerance is None

    def test_string_mismatch(self) -> None:
        v = verify_readback("OPEN", {"pv_name": "X", "value": "SHUT"}, _EPS)
        assert v.verified is False

    def test_a_real_string_pv_reaches_this_branch(self) -> None:
        """The same claim on the shape the client really emits, which is what makes it a claim
        about the system rather than about this file's typing."""
        readback = _string_readback("OPEN")
        assert "enum" not in readback  # no enum block: this is why the exact compare is reached
        assert verify_readback("OPEN", readback, _EPS).verified is True
        assert verify_readback("SHUT", readback, _EPS).verified is False


class TestEnumReadback:
    """An enum write is verified against the LABEL the operator wrote, resolved to its index.

    Every readback here comes from the client (see the module docstring). Before this stage
    existed, a landed and a not-landed enum write both answered ``verified=None``, so the tool
    gave one answer for opposite facts, on the very lane that has no bounds check either (a
    command record declares no drive limits).
    """

    def test_landed_label_verifies(self) -> None:
        v = verify_readback("On", _enum_readback(1, ["Off", "On"]), _EPS)
        assert v.verified is True
        assert v.readback == 1  # the wire form stays the index, as get_pv_value reports it
        assert v.tolerance is None  # an index is exact; a tolerance would be meaningless
        # The note is pinned WHOLE, not by substring: it is the only place the label and the index
        # meet, and a substring check passes a note with the two sides swapped.
        assert v.note == "written label 'On' resolves to index 1 (exact enum compare)"

    def test_not_landed_label_is_a_mismatch(self) -> None:
        """The verdict that did not exist before: the write did not land, and it says so."""
        v = verify_readback("On", _enum_readback(0, ["Off", "On"]), _EPS)
        assert v.verified is False
        assert v.readback == 0
        # Whole-note again, and here it earns it: swapping the two sides would tell the operator
        # the switch reads On when it reads Off, on the very path this change exists for.
        assert v.note == "readback 'Off' (index 0) != written 'On' (index 1)"

    def test_index_spelling_still_takes_the_numeric_track(self) -> None:
        """Writing the index instead of the label worked before this change and still does."""
        landed = verify_readback("1", _enum_readback(1, ["Off", "On"]), _EPS)
        assert landed.verified is True
        assert landed.tolerance == _EPS  # the numeric track, not the enum stage
        assert verify_readback("1", _enum_readback(0, ["Off", "On"]), _EPS).verified is False

    def test_a_numeric_looking_label_resolves_as_a_label(self) -> None:
        """With choices ["1", "2"], p4p writes the LABEL "1", which is index 0, not index 1
        (measured). Resolving by integer parse instead would invert both verdicts here.

        The second pair covers the case the first cannot: a label that equals the decimal spelling
        of a DIFFERENT index. "1" is index 1 of ["0", "1"], so a verdict that ALSO accepted "the
        written text equals the index" would pass a write that did not land.
        """
        assert verify_readback("1", _enum_readback(0, ["1", "2"]), _EPS).verified is True
        assert verify_readback("1", _enum_readback(1, ["1", "2"]), _EPS).verified is False
        assert verify_readback("1", _enum_readback(1, ["0", "1"]), _EPS).verified is True
        assert verify_readback("1", _enum_readback(0, ["0", "1"]), _EPS).verified is False

    def test_a_choice_sharing_a_prefix_does_not_capture_the_write(self) -> None:
        """The label "OPEN" is a prefix of "OPENING", and p4p lands index 1 (measured). A startswith
        or substring resolver would answer index 0 and report a mismatch on a write that landed."""
        readback = _enum_readback(1, ["OPENING", "OPEN"])
        assert verify_readback("OPEN", readback, _EPS).verified is True

    def test_choices_differing_only_in_case_stay_distinct(self) -> None:
        """p4p compares labels case-sensitively and lands index 1 (measured). A case-folding
        resolver would answer index 0 and report a mismatch on a write that landed."""
        assert verify_readback("On", _enum_readback(1, ["ON", "On"]), _EPS).verified is True

    def test_an_index_outside_the_choices_is_a_mismatch_not_unverifiable(self) -> None:
        """An out-of-range index really does land (measured over PVA) and the readback then carries
        label=None. Comparing label to label would call that "not verifiable"; it is a mismatch."""
        v = verify_readback("On", _enum_readback(5, ["Off", "On"]), _EPS)
        assert v.verified is False
        assert v.readback == 5

    def test_a_repeated_label_resolves_to_the_first_of_them(self) -> None:
        """p4p stops at the FIRST matching choice, so writing "A" against ["A", "B", "A"] lands
        index 0 (measured). A readback of the later duplicate is a genuine mismatch, and this is the
        one shape where comparing the readback's LABEL instead of its index would call it a match.
        """
        assert verify_readback("A", _enum_readback(0, ["A", "B", "A"]), _EPS).verified is True
        assert verify_readback("A", _enum_readback(2, ["A", "B", "A"]), _EPS).verified is False

    def test_the_index_one_past_the_last_choice_is_a_mismatch(self) -> None:
        """The BOUNDARY of the range guard, which the out-of-range test above jumps clean over.

        It is also the index an mbbo reports for its first undefined state, so it is not exotic.
        Two mutations live here and nowhere else: a range guard written ``<=`` raises IndexError
        inside a function that promises never to raise, and a verdict that also accepted
        ``index == len(choices)`` would verify a write that did not land.
        """
        v = verify_readback("On", _enum_readback(2, ["Off", "On"]), _EPS)
        assert v.verified is False
        assert v.readback == 2
        assert v.note == "readback None (index 2) != written 'On' (index 1)"

    def test_a_label_against_an_empty_choice_list_is_not_verifiable(self) -> None:
        """An enum record whose choices did not arrive: nothing to resolve the label against, so
        this is an absence of evidence and never a mismatch.

        Honest scope, because the name could promise more than it delivers: an empty list IS a
        usable choices list, so the enum stage is entered and falls through, and the verdict comes
        from the pre-existing numeric track. This is therefore the one case in this class that
        would also pass with the enum stage removed. The stage's own type guards are pinned in
        TestEnumBlockContract below.
        """
        v = verify_readback("On", _enum_readback(0, []), _EPS)
        assert v.verified is None
        assert v.note is not None and "not verifiable" in v.note


class TestEnumBlockContract:
    """The type guards of the enum stage, on shapes the CLIENT cannot produce.

    Hand-built on purpose, and the reason is the mirror image of the one that makes a hand-built
    ENUM readback worthless: these guards exist for the FUNCTION's contract, not for the client's
    data. ``verify_readback`` is documented as total over a ``Mapping`` and is called bare in
    ``tools.write``, AFTER the write's ALLOW audit is already out, so a raise there would tear the
    terminal ``READBACK_*`` line out of a completed write's trail. What has to be pinned is
    therefore exactly what the client never sends.
    """

    def test_an_enum_key_that_is_not_a_mapping_is_ignored(self) -> None:
        readback = {"pv_name": "X", "value": 0, "enum": ["Off", "On"]}
        assert verify_readback("On", readback, _EPS).verified is None  # and no AttributeError

    def test_choices_that_are_not_a_list_are_ignored(self) -> None:
        """A bare string would be iterated CHARACTER by character, and a one-character label would
        then resolve against a record description that does not exist. The written value here is
        deliberately one character: a longer one falls through whether the guard is there or not,
        so it would prove nothing.
        """
        readback = {"pv_name": "X", "value": 0, "enum": {"index": 0, "choices": "AB"}}
        assert verify_readback("A", readback, _EPS).verified is None  # not an invented True

    def test_choices_carrying_a_non_string_entry_are_ignored(self) -> None:
        """Dropping the bad entry instead of refusing the block would SHIFT every label after it:
        "On" would move from index 1 to index 0 and a landed write would read as a mismatch. The
        non-string entry is therefore placed BEFORE the label, which is where the shift shows.
        """
        readback = {"pv_name": "X", "value": 1, "enum": {"index": 1, "choices": [7, "On"]}}
        assert verify_readback("On", readback, _EPS).verified is None  # not an invented False

    def test_a_float_value_beside_an_enum_block_stays_on_the_numeric_track(self) -> None:
        """``labels[1.0]`` raises TypeError, so the int guard at the call site is load-bearing."""
        readback = {"value": 1.0, "enum": {"index": 1, "label": "On", "choices": ["Off", "On"]}}
        assert verify_readback("On", readback, _EPS).verified is None  # and no TypeError
        assert verify_readback("1", readback, _EPS).verified is True  # numeric track, unchanged

    def test_a_negative_index_is_a_mismatch_rather_than_a_wrapped_label(self) -> None:
        """Python would happily read ``labels[-1]`` as the LAST label and name it in the note. The
        lower half of the range guard is what stops the note from naming a label that is not there.
        """
        readback = {"value": -1, "enum": {"index": -1, "choices": ["Off", "On"]}}
        v = verify_readback("On", readback, _EPS)
        assert v.verified is False
        assert v.note == "readback None (index -1) != written 'On' (index 1)"


def test_p4p_resolves_a_label_before_an_index() -> None:
    """Change detector for the third-party behaviour this module mirrors, not a red-provable guard.

    ``verify_readback`` resolves a written label the way ``p4p.nt.NTEnum.assign`` resolves it on the
    put, because verifying by a different rule would verify something the IOC never saw. That rule
    lives in a library nobody here mutates, so this cannot be red-proved by sabotaging our own code;
    inverting its expectation would only show that the assertion runs. What it DOES do is go red the
    day p4p changes the rule, which is the moment the mirroring silently stops being true.
    """
    assert NTEnum().wrap("1", choices=["1", "2"])["value.index"] == 0  # label before integer parse
    assert NTEnum().wrap("OPEN", choices=["OPENING", "OPEN"])["value.index"] == 1  # exact match
    assert NTEnum().wrap("On", choices=["ON", "On"])["value.index"] == 1  # case-sensitive


class TestBooleanReadback:
    """bool is an int subclass → compared by value (True==1.0), the robust numeric path."""

    def test_bool_true_matches_one(self) -> None:
        assert verify_readback("1", {"pv_name": "X", "value": True}, _EPS).verified is True

    def test_bool_false_matches_zero(self) -> None:
        assert verify_readback("0", {"pv_name": "X", "value": False}, _EPS).verified is True


def test_result_is_frozen_model() -> None:
    """The verdict is a validated, immutable Pydantic model (not a bare dict)."""
    v = verify_readback("81", {"pv_name": "X", "value": 81.0}, _EPS)
    assert isinstance(v, ReadbackVerification)
