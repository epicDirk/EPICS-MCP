"""Tests for write tool functions (_set_pv_value) with safety checks."""

import asyncio
import logging
import re
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import epics_pv_mcp.config as config_module
import epics_pv_mcp.safety as safety_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import (
    PVTimeoutError,
    PVWriteBoundsError,
    PVWriteDeniedError,
    RateLimitError,
)
from epics_pv_mcp.safety import SafetyLayer
from epics_pv_mcp.tools.write import _set_pv_value

# E8: writes-ON SafetyLayer construction asserts the process EPICS search env is loopback-only.
# The autouse strip leaves *_AUTO_ADDR_LIST unset = broadcast ON, so pin the loopback lane here.
pytestmark = pytest.mark.usefixtures("loopback_write_env")


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset config and safety singletons for each test."""
    config_module._config = None
    safety_module._safety = None
    yield
    config_module._config = None
    safety_module._safety = None


class TestSetPvValueSuccess:
    """Successful write with safety checks passing."""

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_set_pv_value_success(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        # Configure safety to allow writes
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        # O3: pv_get is now called TWICE, once for the old value (audit), once for the readback.
        # side_effect gives the old read then the readback (20.0 == written "20.0" → verified).
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 10.0},
            {"pv_name": "TEST:PV", "value": 20.0},
        ]

        # Mock the put (returns None)
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "20.0")

        assert result["status"] == "success"
        assert result["pv_name"] == "TEST:PV"
        assert result["old_value"] == 10.0
        assert result["new_value"] == "20.0"
        # O3: the structured readback verdict rides along in the same result dict.
        assert result["verified"] is True
        assert result["readback"] == 20.0

        # M1/C1: no explicit timeout → the wrapper passes None, so pv_get/pv_put apply the
        # server's default_timeout (not a hardcoded 5.0). pv_get is awaited twice (old + readback).
        assert mock_pv_get.await_count == 2
        mock_pv_get.assert_awaited_with("TEST:PV", None)
        mock_pv_put.assert_awaited_once_with("TEST:PV", "20.0", None)


class TestSetPvValueDenied:
    """Write denied by safety layer (writes disabled)."""

    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_set_pv_value_denied(self, mock_get_safety: MagicMock) -> None:
        # Configure safety to deny writes
        cfg = EpicsConfig(allow_pv_write=False)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        with pytest.raises(PVWriteDeniedError):
            await _set_pv_value("TEST:PV", "20.0")


class TestSetPvValueRateLimited:
    """Write rejected due to rate limit exhaustion."""

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_set_pv_value_rate_limited(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        # Configure safety with rate_limit=2
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=2)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 0.0}
        mock_pv_put.return_value = None

        # Exhaust the rate limit
        await _set_pv_value("TEST:PV", "1.0")
        await _set_pv_value("TEST:PV", "2.0")

        # Third call should be rate-limited
        with pytest.raises(RateLimitError):
            await _set_pv_value("TEST:PV", "3.0")


class TestSetPvValueFailed:
    """A write that passes the gate but fails at pv_put: audited (FAILED) + re-raised.

    No real PV is touched: pv_put is mocked to raise (AsyncMock side_effect).
    """

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_pv_put_failure_audits_and_reraises(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = PVTimeoutError("put timed out")

        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(PVTimeoutError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        assert "event=FAILED" in caplog.text
        assert "error_code=PV_TIMEOUT" in caplog.text
        # The failed write must NOT also emit a success record.
        assert "event=ALLOW" not in caplog.text
        mock_pv_put.assert_awaited_once_with("TEST:PV", "2.0", None)

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_non_epics_error_audited_as_internal(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A non-EpicsError (a bug below the tool layer) must still be audited,
        # tagged INTERNAL, and re-raised unchanged.
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = ValueError("unexpected boom")

        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(ValueError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        assert "event=FAILED" in caplog.text
        assert "error_code=INTERNAL" in caplog.text

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_failed_write_consumes_rate_token(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        # Contract pin: a write that passes the gate but fails at pv_put STILL
        # consumed its rate-limit token (append happens in check_write_allowed,
        # before pv_put), so the next attempt is rejected before reaching pv_put.
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=1)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = PVTimeoutError("put timed out")

        with pytest.raises(PVTimeoutError):
            await _set_pv_value("TEST:PV", "1.0")
        with pytest.raises(RateLimitError):
            await _set_pv_value("TEST:PV", "2.0")
        mock_pv_put.assert_awaited_once()  # second attempt never reached pv_put


def _audit_events(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str | None]]:
    """The ``(event, op)`` of every ``PV_WRITE`` line in emission order (records preserve order)."""
    out: list[tuple[str, str | None]] = []
    for record in caplog.records:
        msg = record.getMessage()
        if "PV_WRITE" not in msg:
            continue
        event = msg.split("event=", 1)[1].split(" ", 1)[0]
        match = re.search(r"\bop=(\S+)", msg)
        out.append((event, match.group(1) if match else None))
    return out


class TestSetPvValueAuditTrail:
    """S24/N01: every write emits an ATTEMPT record BEFORE the I/O, correlated by ``op`` with its
    terminal ALLOW/FAILED/UNKNOWN_PENDING record; a write cancelled mid-``pv_put`` is recorded as
    UNKNOWN_PENDING (never lost, never mislabelled FAILED); the cancellation always propagates."""

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_success_emits_attempt_then_allow_same_op(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # O3: old read, then the readback (2.0 == written "2.0" → READBACK_OK after ALLOW).
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 1.0},
            {"pv_name": "TEST:PV", "value": 2.0},
        ]
        mock_pv_put.return_value = None

        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            await _set_pv_value("TEST:PV", "2.0")

        events = _audit_events(caplog)
        # ATTEMPT before the put, ALLOW after it, then the O3 READBACK verdict, all one op.
        assert [e for e, _ in events] == ["ATTEMPT", "ALLOW", "READBACK_OK"]
        ops = {op for _, op in events}
        assert events[0][1] is not None and ops == {events[0][1]}

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_failed_put_emits_attempt_then_failed_same_op(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}
        mock_pv_put.side_effect = PVTimeoutError("put timed out")

        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(PVTimeoutError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        events = _audit_events(caplog)
        assert [e for e, _ in events] == ["ATTEMPT", "FAILED"]
        assert events[0][1] is not None and events[0][1] == events[1][1]

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_cancel_mid_put_emits_unknown_pending_and_reraises(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The put "hangs" (a rendezvous Event marks that we are inside pv_put), then the task is
        # cancelled, mirroring a client disconnect / wait_for timeout while the to_thread put is
        # in flight. No real PV is touched.
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 1.0}

        put_started = asyncio.Event()

        async def _hang(*_args: object, **_kwargs: object) -> None:
            put_started.set()
            await asyncio.Event().wait()  # blocks forever → only leaves via cancellation

        mock_pv_put.side_effect = _hang

        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            task = asyncio.create_task(_set_pv_value("TEST:PV", "2.0"))
            await asyncio.wait_for(put_started.wait(), timeout=2.0)  # ensure we are inside pv_put
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        events = _audit_events(caplog)
        # ATTEMPT (before the put) then UNKNOWN_PENDING (on cancel), correlated by op.
        assert [e for e, _ in events] == ["ATTEMPT", "UNKNOWN_PENDING"]
        assert events[0][1] is not None and events[0][1] == events[1][1]
        # A cancelled write is NEVER labelled ALLOW or FAILED, and never reaches the readback.
        assert "event=ALLOW" not in caplog.text
        assert "event=FAILED" not in caplog.text
        assert "READBACK" not in caplog.text


class TestSetPvValueReadback:
    """O3: after the ALLOW, the write reads the value back and returns a structured verdict.

    A mismatch or an unreadable readback is NEVER a tool error, the write already happened; the
    loudness is the ``verified`` field plus the ``READBACK_*`` audit event, not an exception.
    """

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_mismatch_is_success_with_verified_false(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # Written "20.0" but the IOC holds 99.0 → a genuine mismatch.
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 10.0},
            {"pv_name": "TEST:PV", "value": 99.0},
        ]
        mock_pv_put.return_value = None

        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            result = await _set_pv_value("TEST:PV", "20.0")

        # The put succeeded (status success), but verification FAILED, loud via field + audit.
        assert result["status"] == "success"
        assert result["verified"] is False
        assert result["readback"] == 99.0
        assert "event=READBACK_MISMATCH" in caplog.text
        assert "event=ALLOW" in caplog.text  # the write itself was still allowed

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_readback_timeout_is_not_verifiable_not_an_error(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # Old read succeeds; the readback pv_get times out, "not verifiable", NOT a failure.
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 10.0},
            PVTimeoutError("readback timed out"),
        ]
        mock_pv_put.return_value = None

        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            result = await _set_pv_value("TEST:PV", "20.0")  # must NOT raise

        assert result["status"] == "success"
        assert result["verified"] is None
        assert result["readback"] is None
        assert "event=READBACK_UNVERIFIED" in caplog.text

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_readback_value_none_is_not_verifiable(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # The readback carried value None + a note (the p4p extraction fallback), not a reading.
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 10.0},
            {"pv_name": "TEST:PV", "value": None, "note": "value extraction failed"},
        ]
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "20.0")
        assert result["status"] == "success"
        assert result["verified"] is None


class TestSetPvValueBounds:
    """O2: before the put, the written value is checked against the record's drive limits. An
    out-of-range value is REFUSED before the put (never reaches the IOC); a record with no drive
    limits is not bounds-checkable and the write proceeds (fail-open) with an honest note."""

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_out_of_range_refused_before_put(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # The pre-read carries real drive limits [0, 120]; 130 is out of range.
        mock_pv_get.return_value = {
            "pv_name": "TEST:PV",
            "value": 80.0,
            "control": {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0},
        }

        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(PVWriteBoundsError),
        ):
            await _set_pv_value("TEST:PV", "130")

        # The put NEVER happened: the value never reached the IOC.
        mock_pv_put.assert_not_awaited()
        assert "event=BOUNDS_DENY" in caplog.text
        # A refused-before-put write emits no ATTEMPT/ALLOW/READBACK.
        assert "event=ATTEMPT" not in caplog.text
        assert "event=ALLOW" not in caplog.text
        assert "READBACK" not in caplog.text

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_in_range_proceeds_no_bounds_note(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        control = {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0}
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 80.0, "control": control},
            {"pv_name": "TEST:PV", "value": 81.0, "control": control},
        ]
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "81")
        assert result["status"] == "success"
        # In-range: checked and fine → no bounds note; the write went through.
        assert result["bounds_note"] is None
        mock_pv_put.assert_awaited_once()

    @patch("epics_pv_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_pv_mcp.tools.write.get_safety")
    async def test_unbounded_record_proceeds_with_bounds_note(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # An enum-like record with NO control block → not bounds-checkable → fail-open.
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 0},
            {"pv_name": "TEST:PV", "value": 1},
        ]
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "1")
        assert result["status"] == "success"
        # Fail-open carries an honest note so the un-checked write is visible.
        assert result["bounds_note"] is not None
        mock_pv_put.assert_awaited_once()
