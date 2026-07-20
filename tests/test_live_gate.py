"""Offline tests of the live gate (tests/live_gate.py).

The point of this file: CI can NEVER run the live half (no live stack) — but it CAN pin
the DECISION about it. Exactly that half is pinned here, deterministic and offline.

Counter-probe discipline: every positive case has its negative. "Skips when the
prerequisite is missing" alone would also stay green if the gate ALWAYS skipped — so it
is equally measured that it does NOT hold anyone up when the prerequisite is present and
does NOT stay silent when demanded.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.live_gate import REQUIRE_LIVE_ENV, assert_live_available, live_demanded

REASON = "the live stack is not configured"


def _outcome_of(gate_call: Callable[[], None]) -> BaseException | None:
    """CAPTURE the gate's outcome instead of letting it propagate.

    Critical, and learned the expensive way (sister repo, decision IW): a propagated
    ``Skipped`` turns THIS test into a skip — the watchdog vanishes exactly as silently
    as the defect it guards. Measured there: without this capture, the mutant "demanded
    branch removed" stayed green (2 skipped instead of 2 failed), so the watchdog proved
    nothing.

    ``None`` = the gate let the test through.
    """
    try:
        gate_call()
    except BaseException as exc:  # noqa: BLE001 — Skipped/Failed inherit from BaseException
        return exc
    return None


# --- live_demanded: what counts as "demanded"? -------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", "  1  "])
def test_live_demanded_accepts_truthy_spellings(value: str) -> None:
    assert live_demanded({REQUIRE_LIVE_ENV: value}) is True


@pytest.mark.parametrize("value", ["", "   ", "0", "false", "no", "off", "maybe"])
def test_live_demanded_rejects_everything_else(value: str) -> None:
    assert live_demanded({REQUIRE_LIVE_ENV: value}) is False


def test_live_demanded_is_false_when_var_absent() -> None:
    """The default run and CI do not set the variable — that MUST stay silent."""
    assert live_demanded({}) is False


def test_live_demanded_ignores_other_variables() -> None:
    assert live_demanded({"EPICS_MCP_REQUIRE_LIVE_XYZ": "1", "REQUIRE_LIVE": "1"}) is False


# --- assert_live_available: the three branches -------------------------------


@pytest.mark.parametrize("demanded", [False, True])
def test_available_lets_the_test_run(demanded: bool) -> None:
    """Positive control: with the prerequisite present, the gate holds nobody up."""
    outcome = _outcome_of(lambda: assert_live_available(True, REASON, demanded=demanded))
    assert outcome is None, f"the gate held despite a met prerequisite: {outcome!r}"


def test_missing_and_not_demanded_skips() -> None:
    """The default path: silently skipped, so CI stays green."""
    outcome = _outcome_of(lambda: assert_live_available(False, REASON, demanded=False))
    assert isinstance(outcome, pytest.skip.Exception), f"expected Skipped, got {outcome!r}"
    assert REASON in str(outcome)


def test_missing_but_demanded_fails_loudly() -> None:
    """The core of S30: a DEMANDED live run no longer skips — it goes red."""
    outcome = _outcome_of(lambda: assert_live_available(False, REASON, demanded=True))
    assert isinstance(outcome, pytest.fail.Exception), f"expected Failed, got {outcome!r}"
    # No skip — that is the entire difference the order stands on.
    assert not isinstance(outcome, pytest.skip.Exception)
    # The message must name BOTH: that it was demanded and what was missing — otherwise
    # the reader hunts the cause in the test code instead of their environment.
    message = str(outcome)
    assert REQUIRE_LIVE_ENV in message
    assert REASON in message


def test_demand_changes_the_outcome_class() -> None:
    """Demanding must CHANGE the outcome class, not just the text.

    Without this comparison both branches could raise the same class and the tests
    above would be tautologically green.
    """
    silent = _outcome_of(lambda: assert_live_available(False, REASON, demanded=False))
    loud = _outcome_of(lambda: assert_live_available(False, REASON, demanded=True))
    assert type(silent) is not type(loud)
