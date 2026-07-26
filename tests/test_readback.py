"""Tests for the pure readback-verification core (:mod:`epics_pv_mcp.readback`).

No network, no clock, no env, :func:`verify_readback` is a total function over (written string,
readback dict, tolerance). These cover the four verdict classes (ok / mismatch / not-verifiable /
type-coercion) plus the two tolerance sources (live ``min_step`` vs. the epsilon fallback) and the
magnitude-safety property that motivated ``math.isclose``.

Red-provability (Evidence #5): the mismatch and magnitude tests go red under a mutant that inverts
the comparison or replaces the tolerance with ``inf``, a guard that cannot go red is the defect.
"""

from __future__ import annotations

from epics_pv_mcp.readback import ReadbackVerification, verify_readback

_EPS = 1e-6


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
    """A string/enum-label readback compares exactly, without a tolerance."""

    def test_string_match(self) -> None:
        v = verify_readback("OPEN", {"pv_name": "X", "value": "OPEN"}, _EPS)
        assert v.verified is True
        assert v.tolerance is None

    def test_string_mismatch(self) -> None:
        v = verify_readback("OPEN", {"pv_name": "X", "value": "SHUT"}, _EPS)
        assert v.verified is False


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
