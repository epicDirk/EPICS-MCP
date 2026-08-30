"""Tests for the pure value-safety core (:mod:`epics_mcp.bounds`).

No network, no clock, no env: both functions here are total over (written string, readback dict),
and the step check takes its limit as an argument rather than reading the config, which is what
keeps it deterministic and what keeps a developer's shell out of the result.

:func:`check_value_in_bounds` asks whether the value may BE where it is going, against the record's
own drive limits. :func:`check_step_within_limit` asks whether it may GET there in one write,
against a configured limit that is OFF by default. The two are covered separately, and one test
drives them together on the same readback, because the case that motivates the second is exactly
the one the first admits: a value inside [DRVL, DRVH] that is nonetheless far from where the
process is now.

Red-provability (Evidence #5): the out-of-range and over-the-limit tests go red under a mutant that
inverts the compare or widens the limit to infinity; a guard that cannot go red is the defect.
"""

from __future__ import annotations

from epics_mcp.bounds import (
    BoundsVerdict,
    StepVerdict,
    check_step_within_limit,
    check_value_in_bounds,
)

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


class TestStepOff:
    """No limit configured is OFF, not fail-open, and it is the shipping posture."""

    def test_zero_limit_does_not_check(self) -> None:
        v = check_step_within_limit("0", {"value": 80.0}, 0.0)
        assert v.within_limit is None
        # Silent on purpose: a note on every write of a server that has the limit switched off
        # would be noise a caller cannot act on.
        assert v.note is None
        assert v.limit is None


class TestStepWithinLimit:
    """A distance at or below the limit passes; the boundary is inclusive."""

    def test_small_step(self) -> None:
        v = check_step_within_limit("85", {"value": 80.0}, 10.0)
        assert v.within_limit is True
        assert v.step == 5.0
        assert v.limit == 10.0
        assert v.note is None

    def test_boundary_is_inclusive(self) -> None:
        assert check_step_within_limit("90", {"value": 80.0}, 10.0).within_limit is True

    def test_downward_step_counts_the_same(self) -> None:
        # The distance is absolute: moving down is a step like moving up.
        v = check_step_within_limit("70", {"value": 80.0}, 10.0)
        assert v.within_limit is True
        assert v.step == 10.0


class TestStepExceedsLimit:
    """A distance above the limit is the ONLY deny."""

    def test_over_the_limit(self) -> None:
        # Red-provable: inverting the compare in check_step_within_limit flips this to True, and
        # widening the limit to infinity does the same.
        v = check_step_within_limit("100", {"value": 80.0}, 10.0)
        assert v.within_limit is False
        assert v.step == 20.0
        assert v.note is not None and "exceeds the configured limit" in v.note

    def test_the_value_may_be_in_bounds_and_the_step_still_refused(self) -> None:
        """The whole point of the check: bounds passes, the step does not.

        120 is the record's own DRVH, so the drive-limit check ADMITS it; the distance from 80 is
        40, which the step limit refuses. Without this check a single wrong setpoint could drive
        the value across the full range in one write.
        """
        readback = {"value": 80.0, "control": _LIMITS}
        assert check_value_in_bounds("120", readback).in_bounds is True
        assert check_step_within_limit("120", readback, 10.0).within_limit is False


class TestStepNotCheckable:
    """The fail-open cases, each for a stated reason."""

    def test_enum_record_is_not_step_checkable(self) -> None:
        """An enum's ``value`` is an INDEX, so a compare would succeed and mean nothing.

        Measured on the read path: ``_format_value`` puts the index in ``value`` and sets ``enum``
        beside it, so a bo/mbbo comes back NUMERIC. Without this branch a limit below 1 would block
        a command record from ever being set.
        """
        readback = {"value": 0, "enum": {"index": 0, "choices": ["Idle", "Reset"]}}
        v = check_step_within_limit("1", readback, 0.5)
        assert v.within_limit is None
        assert v.note is not None and "enum" in v.note

    def test_missing_current_value(self) -> None:
        v = check_step_within_limit("85", {}, 10.0)
        assert v.within_limit is None
        assert v.note is not None and "distance not measurable" in v.note

    def test_non_finite_current_value(self) -> None:
        # A record reading nan has no measurable distance; comparing would pass SILENTLY, because
        # every comparison against nan is False.
        assert check_step_within_limit("85", {"value": float("nan")}, 10.0).within_limit is None

    def test_bool_current_value_is_not_a_value(self) -> None:
        assert check_step_within_limit("1", {"value": True}, 10.0).within_limit is None

    def test_non_numeric_written_value(self) -> None:
        v = check_step_within_limit("Reset", {"value": 80.0}, 10.0)
        assert v.within_limit is None
        assert v.note is not None and "not numeric" in v.note


class TestStepNonFiniteWritten:
    """A coercible non-finite written value against a configured limit is refused."""

    def test_inf_written(self) -> None:
        v = check_step_within_limit("inf", {"value": 80.0}, 10.0)
        assert v.within_limit is False
        assert v.note is not None and "not finite" in v.note

    def test_nan_written(self) -> None:
        assert check_step_within_limit("nan", {"value": 80.0}, 10.0).within_limit is False


def test_step_verdict_is_frozen_model() -> None:
    """The step verdict is a validated, immutable Pydantic model too."""
    assert isinstance(check_step_within_limit("85", {"value": 80.0}, 10.0), StepVerdict)
