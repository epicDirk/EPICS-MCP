"""Live write + readback verification (O3) — the class of correctness a mock CANNOT show.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_ALLOW_PV_WRITE=true``, a loopback EPICS search env, and
``EPICS_MCP_LIVE_WRITE_PV`` pointing at a WRITABLE numeric PV inside the write allowlist. Optional
``EPICS_MCP_LIVE_WRITE_VALUE`` supplies a safe, in-range value DIFFERENT from the current one for
the strong "the readback reflects the NEW value" probe (that probe skips if it is unset). Both
tests restore the PV's original value.

WHY THIS EXISTS
---------------
The readback → compare → ``verified`` path only becomes true against a real IOC that actually
accepts a put and serves the value back; a mock only ever knows what the client SENT. This is the
one place the O3 machinery runs end-to-end against a live record.

RED-PROVABILITY (Evidence #5)
-----------------------------
If the put did not land, or the readback read the wrong PV / a stale value, ``verified`` is not
``True`` and the assertions fail. The unit tests (``test_readback.py`` / ``test_write.py``) carry
the inverted-compare and mismatch mutants; here the live claim is "a real write is verified True
and the readback reflects the value written".

The values are facility-agnostic — every PV name and value arrives from the environment; nothing
site-specific is committed.
"""

from __future__ import annotations

import math
import os

import pytest

from epics_pv_mcp.errors import PVWriteBoundsError
from epics_pv_mcp.services.epics_client import pv_get
from epics_pv_mcp.tools.write import _set_pv_value
from tests.live_gate import assert_live_available, live_demanded

# The autouse conftest fixture ``_isolate_epics_search_env`` strips the EPICS search env so posture
# tests measure the code, not the machine — which also removes the route to the IOC.
# ``loopback_write_env`` re-injects the loopback write lane AFTER that strip; a live WRITE can ONLY
# run loopback anyway (the SafetyLayer reach assert fails closed on a non-loopback reach when writes
# are on), so this is the only lane the probe could use. The PV NAME still comes from the
# environment (facility-agnostic).
pytestmark = [pytest.mark.live, pytest.mark.usefixtures("loopback_write_env")]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _writes_enabled_with_target() -> bool:
    """Master write switch on AND a target PV set. The write path constructs a SafetyLayer that
    ALSO asserts a loopback reach; a non-loopback env fails closed there (its own business)."""
    return bool(
        os.environ.get("EPICS_MCP_ALLOW_PV_WRITE", "").strip().lower() in _TRUTHY
        and os.environ.get("EPICS_MCP_LIVE_WRITE_PV")
    )


@pytest.fixture(autouse=True)
def _require_live_write_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is demanded
    (EPICS_MCP_REQUIRE_LIVE=1) and the write lane is not configured."""
    assert_live_available(
        _writes_enabled_with_target(),
        "live write probe: set EPICS_MCP_ALLOW_PV_WRITE=true, a loopback EPICS search env, and "
        "EPICS_MCP_LIVE_WRITE_PV (a writable numeric PV inside the write allowlist)",
        demanded=live_demanded(os.environ),
    )


@pytest.fixture
def pv() -> str:
    return os.environ["EPICS_MCP_LIVE_WRITE_PV"]


async def _read_numeric(pv_name: str) -> float:
    """The current numeric value of ``pv_name`` (asserts it is a live numeric reading)."""
    raw = await pv_get(pv_name, None)
    value = raw.get("value")
    assert isinstance(value, (int, float)), f"read of {pv_name} is not numeric: {raw!r}"
    return float(value)


class TestLiveWriteReadback:
    """The O3 readback machinery against a real IOC."""

    async def test_write_same_value_verifies(self, pv: str) -> None:
        """Writing the current value back verifies True — the OK path against a real record,
        non-disruptive and always in range."""
        baseline = await _read_numeric(pv)
        result = await _set_pv_value(pv, str(baseline))
        assert result["status"] == "success"
        assert result["verified"] is True, f"same-value write not verified: {result!r}"
        assert result["readback"] is not None

    async def test_write_changed_value_verifies_and_restores(self, pv: str) -> None:
        """Writing a DIFFERENT value: the readback must reflect the NEW value (verified True). This
        is the strong probe — a readback that returned the stale baseline would flip verified to
        False. Needs a safe in-range EPICS_MCP_LIVE_WRITE_VALUE; the original is always restored."""
        target = os.environ.get("EPICS_MCP_LIVE_WRITE_VALUE")
        assert_live_available(
            bool(target),
            "set EPICS_MCP_LIVE_WRITE_VALUE to a safe in-range value (≠ current) for the strong "
            "readback probe",
            demanded=live_demanded(os.environ),
        )
        assert target is not None  # narrowed by the gate above

        baseline = await _read_numeric(pv)
        try:
            result = await _set_pv_value(pv, target)
            assert result["status"] == "success"
            assert result["verified"] is True, f"changed-value write not verified: {result!r}"
            readback = result["readback"]
            assert isinstance(readback, (int, float))
            # The readback must reflect the NEW value, not the stale baseline.
            assert math.isclose(float(readback), float(target), rel_tol=1e-3, abs_tol=1e-3), (
                f"readback {readback!r} did not reflect the written value {target!r}"
            )
        finally:
            # Restore the original value regardless of the assertion outcome.
            await _set_pv_value(pv, str(baseline))


class TestLiveWriteBounds:
    """The O2 value-bounds guard against a real IOC: an out-of-range write is refused BEFORE the
    put, so the live value is unchanged (the value never reached the IOC). The class a mock cannot
    show — a mock only knows what the client sent, never that the record's own limits blocked it."""

    async def test_out_of_range_write_is_refused_and_value_unchanged(self, pv: str) -> None:
        # O2 only bites a record that DECLARES drive limits; a limitless record correctly fails
        # open, so skip rather than silently passing AND landing the value. Probe control first.
        info = await pv_get(pv, None)
        control = info.get("control")
        has_limits = (
            isinstance(control, dict) and "limit_low" in control and "limit_high" in control
        )
        assert_live_available(
            has_limits,
            f"live bounds probe: {pv} declares no control drive limits (DRVL/DRVH) — point "
            "EPICS_MCP_LIVE_WRITE_PV at a record WITH drive limits",
            demanded=live_demanded(os.environ),
        )
        out_of_range = os.environ.get("EPICS_MCP_LIVE_OUT_OF_RANGE_VALUE")
        assert_live_available(
            bool(out_of_range),
            "set EPICS_MCP_LIVE_OUT_OF_RANGE_VALUE to a value OUTSIDE the record's [DRVL, DRVH]",
            demanded=live_demanded(os.environ),
        )
        assert out_of_range is not None  # narrowed by the gate above

        baseline = await _read_numeric(pv)
        try:
            # The out-of-range write must be refused before the put — a real PVWriteBoundsError.
            with pytest.raises(PVWriteBoundsError):
                await _set_pv_value(pv, out_of_range)
            # And the live value must be UNCHANGED: the put never happened. A missing guard would
            # have landed the out-of-range value, so this reads it back and it must equal baseline.
            after = await _read_numeric(pv)
            assert math.isclose(after, baseline, rel_tol=1e-9, abs_tol=1e-9), (
                f"out-of-range value landed at the IOC: {after!r} != baseline {baseline!r}"
            )
        finally:
            # Defensive restore — even though the write should never have landed.
            await _set_pv_value(pv, str(baseline))
