"""Tests for epics_mcp.cli_common (shared CLI plumbing)."""

from __future__ import annotations

import argparse

import pytest

from epics_mcp.cli_common import positive_timeout


def test_positive_timeout_accepts_a_positive_number() -> None:
    assert positive_timeout("5") == 5.0
    assert positive_timeout("0.25") == 0.25


@pytest.mark.parametrize("bad", ["-1", "0", "0.0", "-0.5"])
def test_positive_timeout_rejects_nonpositive(bad: str) -> None:
    """F22: a non-positive timeout must be a usage error, not silently accepted, a <=0 timeout
    flows into a live probe and can make a HEALTHY service look unreachable. RED before the guard
    (a bare ``type=float`` accepts every one of these)."""
    with pytest.raises(argparse.ArgumentTypeError):
        positive_timeout(bad)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "abc"])
def test_positive_timeout_rejects_nan_inf_and_non_number(bad: str) -> None:
    """``nan``/``inf``/``-inf`` all parse as floats but are nonsense timeouts (``inf`` = wait
    forever, defeating the flag); a non-number fails the float parse. All are usage errors, not a
    probe with a nonsense timeout."""
    with pytest.raises(argparse.ArgumentTypeError):
        positive_timeout(bad)
