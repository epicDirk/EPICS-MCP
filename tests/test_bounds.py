"""Tests for the pure value-bounds core (:mod:`epics_pv_mcp.bounds`).

No network, no clock, no env — :func:`check_value_in_bounds` is a total function over (written
string, readback dict). These cover the three verdict classes (in-range / out-of-range / not
bounds-checkable) plus the fail-open cases (no control, dropped limits, non-numeric written) and the
fail-closed non-finite case.

Red-provability (Evidence #5): the out-of-range tests go red under a mutant that inverts the compare
or widens the limits to infinity — a guard that cannot go red is the defect.
"""

from __future__ import annotations

from epics_pv_mcp.bounds import BoundsVerdict, check_value_in_bounds

_LIMITS = {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0}


class TestInRange:
    """A value within [DRVL, DRVH] verifies True; the written string is coerced to float."""

    def test_mid_range(self) -> None:
        v = check_value_in_bounds("80", {"pv_name": "X", "value": 80.0, "control": _LIMITS})
        assert v.in_bounds is True
        assert v.limit_low == 0.0
        assert v.limit_high == 120.0
        assert v.note is None

    def test_low_boundary_inclusive(self) -> None:
        assert check_value_in_bounds("0", {"control": _LIMITS}).in_bounds is True

    def test_high_boundary_inclusive(self) -> None:
        assert check_value_in_bounds("120", {"control": _LIMITS}).in_bounds is True


class TestOutOfRange:
    """A value outside the limits is the ONLY deny (in_bounds False), never None."""

    def test_over_high(self) -> None:
        # Red-provable: inverting the compare flips this to True (the sandbox 130-vs-0..120 case).
        v = check_value_in_bounds("130", {"control": _LIMITS})
        assert v.in_bounds is False
        assert v.note is not None and "outside" in v.note

    def test_under_low(self) -> None:
        assert check_value_in_bounds("-5", {"control": _LIMITS}).in_bounds is False


class TestNoLimits:
    """No control block / dropped limits / degenerate range → not bounds-checkable (fail-open)."""

    def test_no_control_block(self) -> None:
        # An enum record (e.g. the reset command lane) carries no control block.
        v = check_value_in_bounds("1", {"pv_name": "X", "value": 0})
        assert v.in_bounds is None
        assert v.note is not None

    def test_control_without_limit_pair(self) -> None:
        # DRVL==DRVH is dropped upstream → control carries only min_step.
        v = check_value_in_bounds("999", {"control": {"min_step": 0.0}})
        assert v.in_bounds is None

    def test_degenerate_range_low_ge_high(self) -> None:
        v = check_value_in_bounds("5", {"control": {"limit_low": 10.0, "limit_high": 10.0}})
        assert v.in_bounds is None

    def test_inverted_range(self) -> None:
        v = check_value_in_bounds("5", {"control": {"limit_low": 100.0, "limit_high": 0.0}})
        assert v.in_bounds is None

    def test_non_finite_limit(self) -> None:
        rb = {"control": {"limit_low": float("nan"), "limit_high": 120.0}}
        assert check_value_in_bounds("50", rb).in_bounds is None

    def test_bool_limit_is_not_a_limit(self) -> None:
        # bool is an int subclass; a boolean limit is meaningless → not bounds-checkable.
        rb = {"control": {"limit_low": False, "limit_high": True}}
        assert check_value_in_bounds("0", rb).in_bounds is None


class TestNonNumericWritten:
    """A non-numeric written value cannot be range-checked → fail-open."""

    def test_non_coercible_string(self) -> None:
        v = check_value_in_bounds("abc", {"control": _LIMITS})
        assert v.in_bounds is None
        assert v.limit_low == 0.0  # limits still surfaced for context


class TestNonFiniteWritten:
    """A coercible non-finite written value against a bounded record is refused (fail-closed)."""

    def test_nan_written(self) -> None:
        v = check_value_in_bounds("nan", {"control": _LIMITS})
        assert v.in_bounds is False
        assert v.note is not None and "finite" in v.note

    def test_inf_written(self) -> None:
        assert check_value_in_bounds("inf", {"control": _LIMITS}).in_bounds is False


class TestIntTypedLimits:
    """Limits served as int (not float) still bound correctly."""

    def test_int_limits(self) -> None:
        rb = {"control": {"limit_low": 0, "limit_high": 100}}
        assert check_value_in_bounds("50", rb).in_bounds is True
        assert check_value_in_bounds("150", rb).in_bounds is False


def test_verdict_is_frozen_model() -> None:
    """The verdict is a validated, immutable Pydantic model (not a bare dict)."""
    v = check_value_in_bounds("80", {"control": _LIMITS})
    assert isinstance(v, BoundsVerdict)
