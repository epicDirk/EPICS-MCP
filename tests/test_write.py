"""Tests for write tool functions (_set_pv_value) with safety checks."""

import asyncio
import logging
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from p4p.nt import NTEnum

import epics_mcp.config as config_module
import epics_mcp.safety as safety_module
import epics_mcp.tools.write as write_module
from epics_mcp.config import EpicsConfig
from epics_mcp.errors import (
    PVTimeoutError,
    PVWriteBoundsError,
    PVWriteDeniedError,
    PVWriteStepError,
    RateLimitError,
    SafetyConfigError,
)
from epics_mcp.safety import SafetyLayer
from epics_mcp.services.epics_client import _format_value
from epics_mcp.tools.write import _set_pv_value


def _enum_readback(index: int, choices: Sequence[str]) -> dict[str, object]:
    """A pv_get result in the shape the real client emits for an enum PV (index + enum block).

    Built through p4p rather than typed out. It matters twice on this path: ``_set_pv_value`` hands
    the PRE-read to ``check_value_in_bounds`` as well, so a hand-built pre-read would decide whether
    the write is bounds-refused before it ever reaches the readback under test.
    """
    nt = NTEnum()
    return _format_value("TEST:PV", nt.unwrap(nt.wrap({"index": index, "choices": list(choices)})))


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


@pytest.fixture(autouse=True)
def _pin_write_path_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the two config fields ``_set_pv_value`` reads through ``get_config()``.

    Resetting the singleton above is not enough: the next ``get_config()`` rebuilds ``EpicsConfig``
    from ``os.environ``, and ``EpicsConfig`` is a ``BaseSettings`` with ``env_prefix="EPICS_MCP_"``,
    so a developer's shell decides what these tests measure. The autouse strip in ``conftest.py``
    clears ``EPICS_PVA_*`` and ``EPICS_CA_*`` only.

    MEASURED, not feared, and both directions were run on this tree before this fixture existed:
    ``EPICS_MCP_MAX_WRITE_STEP=1`` turned FIVE tests in this module red, and
    ``EPICS_MCP_READBACK_TOLERANCE=1000`` turned ONE red. The second is older than the step limit,
    so this closes a leak that was already here rather than only the one that arrived with it.

    Patched at ``tools.write``'s own name, the seam the code under test actually reads, rather than
    at ``config``: the module binds ``get_config`` at import, so patching the source module would
    leave this call site pointing at the original.
    """
    pinned = EpicsConfig(max_write_step=0.0, readback_tolerance=1e-6)
    monkeypatch.setattr(write_module, "get_config", lambda: pinned)


class TestSetPvValueSuccess:
    """Successful write with safety checks passing."""

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

    @patch("epics_mcp.tools.write.get_safety")
    async def test_set_pv_value_denied(self, mock_get_safety: MagicMock) -> None:
        # Configure safety to deny writes
        cfg = EpicsConfig(allow_pv_write=False)
        sl = SafetyLayer(cfg)
        mock_get_safety.return_value = sl

        with pytest.raises(PVWriteDeniedError):
            await _set_pv_value("TEST:PV", "20.0")


class TestSetPvValueAuditPathRefusal:
    """BG-DPATH, the route half: this tool builds the PV gate, so its refusal is a tool answer."""

    async def test_a_write_tool_refusal_does_not_disclose_the_audit_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measurement that reopened BG-DPATH, pinned so it cannot be re-argued from the code.

        ``safety.py``'s refusal for an unopenable ``EPICS_MCP_AUDIT_LOG_FILE`` carried the full
        path, deliberately, on the argument that this layer is built EAGERLY in ``server.main`` and
        so is only ever read by the operator on the host's stderr. Measured in-process on
        2026-08-20, that argument does not hold at the DEFAULT posture, and this test is the route
        it does not hold on. Four facts, each read off the code rather than recalled:
        ``get_safety`` is a LAZY singleton; ``server.main`` pre-builds it only under
        ``if config.allow_pv_write``; ``set_pv_value`` is registered on the server
        UNCONDITIONALLY; and ``_set_pv_value`` calls ``get_safety()`` BEFORE
        ``check_write_allowed``, so a config error preempts the write refusal.

        The posture that makes it reachable is not exotic, it is the sanctioned one: an Olog-write
        deployment MUST configure a durable audit path (``server.py`` refuses to start otherwise),
        and PV writes stay off. Let that path stop being openable, a permission change or a moved
        directory, and the next ``set_pv_value`` call from any caller answered with the path,
        account name included, twice over.

        ``get_safety`` is deliberately NOT patched here, unlike every other test in this file: the
        subject IS the singleton being built lazily on this route, so mocking it would test nothing.
        The module global is cleared through ``monkeypatch`` rather than left to this file's autouse
        ``_reset_singletons``, which also clears it: the dependency is stated locally so the test
        keeps meaning what it says if that fixture is ever narrowed.

        Red-proof: restore ``safety.py``'s old message and the path assertion fails while the
        variable assertion stays green; make ``_set_pv_value`` check ``allow_pv_write`` before it
        calls ``get_safety`` and this test goes green for the OTHER reason, which is why the message
        is asserted here and not merely the absence of a refusal.
        """
        secret_dir = tmp_path / "operator-account-name"
        cfg = EpicsConfig(allow_pv_write=False, audit_log_file=str(secret_dir / "audit.log"))
        monkeypatch.setattr(safety_module, "_safety", None)
        monkeypatch.setattr(safety_module, "get_config", lambda: cfg)
        audit = logging.getLogger("epics_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            with pytest.raises(SafetyConfigError) as excinfo:
                await _set_pv_value("TEST:PV", "20.0")
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

        message = str(excinfo.value)
        assert "EPICS_MCP_AUDIT_LOG_FILE" in message, (
            "the caller must still learn WHICH variable is broken, or withholding its value leaves "
            f"them nothing to act on: {message!r}"
        )
        assert "operator-account-name" not in message, (
            f"a PV write tool disclosed the server's audit path to its caller: {message!r}"
        )
        assert excinfo.value.details["audit_log_file"] == str(secret_dir / "audit.log"), (
            "the in-process detail keeps the path; tool_errors sends only the code and str(exc)"
        )


class TestSetPvValueRateLimited:
    """Write rejected due to rate limit exhaustion."""

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
            pytest.raises(PVTimeoutError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        assert "event=FAILED" in caplog.text
        assert "error_code=PV_TIMEOUT" in caplog.text
        # The failed write must NOT also emit a success record.
        assert "event=ALLOW" not in caplog.text
        mock_pv_put.assert_awaited_once_with("TEST:PV", "2.0", None)

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
            pytest.raises(ValueError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        assert "event=FAILED" in caplog.text
        assert "error_code=INTERNAL" in caplog.text

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            await _set_pv_value("TEST:PV", "2.0")

        events = _audit_events(caplog)
        # ATTEMPT before the put, ALLOW after it, then the O3 READBACK verdict, all one op.
        assert [e for e, _ in events] == ["ATTEMPT", "ALLOW", "READBACK_OK"]
        ops = {op for _, op in events}
        assert events[0][1] is not None and ops == {events[0][1]}

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
            pytest.raises(PVTimeoutError),
        ):
            await _set_pv_value("TEST:PV", "2.0")

        events = _audit_events(caplog)
        assert [e for e, _ in events] == ["ATTEMPT", "FAILED"]
        assert events[0][1] is not None and events[0][1] == events[1][1]

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
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

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            result = await _set_pv_value("TEST:PV", "20.0")

        # The put succeeded (status success), but verification FAILED, loud via field + audit.
        assert result["status"] == "success"
        assert result["verified"] is False
        assert result["readback"] == 99.0
        assert "event=READBACK_MISMATCH" in caplog.text
        assert "event=ALLOW" in caplog.text  # the write itself was still allowed

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            result = await _set_pv_value("TEST:PV", "20.0")  # must NOT raise

        assert result["status"] == "success"
        assert result["verified"] is None
        assert result["readback"] is None
        assert "event=READBACK_UNVERIFIED" in caplog.text

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_landed_enum_label_verifies_and_audits_ok(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """GB-32, the headline claim: writing a LABEL to an enum PV that lands is verified.

        This lane has no other value safety net (a command record declares no drive limits, so the
        bounds check fails open), which is why the readback verdict is the whole guard here.
        """
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.side_effect = [
            _enum_readback(0, ["Off", "On"]),  # pre-read: the switch is Off
            _enum_readback(1, ["Off", "On"]),  # readback: "On" landed
        ]
        mock_pv_put.return_value = None

        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            result = await _set_pv_value("TEST:PV", "On")

        assert result["status"] == "success"
        assert result["verified"] is True
        assert result["readback"] == 1  # the index, as get_pv_value reports it
        assert result["bounds_note"] is not None  # no drive limits on an enum record: fail-open
        assert "event=READBACK_OK" in caplog.text

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_not_landed_enum_label_is_a_mismatch_and_audits_it(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The other half, and the reason the first one is not enough: the two outcomes have to be
        DISTINGUISHABLE. Both answered verified=None before this change."""
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.side_effect = [
            _enum_readback(0, ["Off", "On"]),  # pre-read: Off
            _enum_readback(0, ["Off", "On"]),  # readback: still Off, the write did not land
        ]
        mock_pv_put.return_value = None

        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            result = await _set_pv_value("TEST:PV", "On")

        assert result["status"] == "success"  # the put itself was executed and ALLOW-audited
        assert result["verified"] is False
        assert result["readback"] == 0
        assert "event=READBACK_MISMATCH" in caplog.text
        assert "event=ALLOW" in caplog.text


class TestSetPvValueBounds:
    """O2: before the put, the written value is checked against the record's drive limits. An
    out-of-range value is REFUSED before the put (never reaches the IOC); a record with no drive
    limits is not bounds-checkable and the write proceeds (fail-open) with an honest note."""

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
            pytest.raises(PVWriteBoundsError),
        ):
            await _set_pv_value("TEST:PV", "130")

        # The put NEVER happened: the value never reached the IOC.
        mock_pv_put.assert_not_awaited()
        assert "event=BOUNDS_DENY" in caplog.text
        # QA-39: a pre-dispatch refusal carries NO op= token, and the operator guide says so. The
        # id is issued when a write is dispatched, and this one never was, so the line stands
        # alone. Pinned here because the guide previously promised an op correlation that the two
        # refusals cannot honour. Red-provable by adding op=%s to audit_bounds_deny.
        assert _audit_events(caplog) == [("BOUNDS_DENY", None)]
        # A refused-before-put write emits no ATTEMPT/ALLOW/READBACK.
        assert "event=ATTEMPT" not in caplog.text
        assert "event=ALLOW" not in caplog.text
        assert "READBACK" not in caplog.text

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
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

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_unbounded_record_proceeds_with_bounds_note(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # A record with NO control block → not bounds-checkable → fail-open. Deliberately a bare
        # numeric shape rather than an enum one: an enum readback also carries an `enum` block (see
        # _enum_readback), and this test is about the missing control block alone.
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


class TestSetPvValueStep:
    """O2b: the SECOND post-admission refusal, off unless ``EPICS_MCP_MAX_WRITE_STEP`` is set.

    Bounds asks whether the value may BE there; this asks whether it may GET there in one write.
    The autouse ``_pin_write_path_config`` fixture pins the limit to 0 for every OTHER test in this
    module, so each test here arms it explicitly and nothing leaks between them.
    """

    @staticmethod
    def _armed(step: float) -> EpicsConfig:
        """A config with the step limit armed, every field it decides written out explicitly."""
        return EpicsConfig(max_write_step=step, readback_tolerance=1e-6)

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_oversized_step_refused_before_put(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The case the whole check exists for: IN BOUNDS, and still refused.

        120 is the record's own DRVH, so the drive-limit check admits it. The distance from 80 is
        40, and the limit is 10. Without this check one wrong setpoint drives the value across the
        full range in a single write, which is what "a mistake moves something on a plant" means.
        """
        monkeypatch.setattr(write_module, "get_config", lambda: self._armed(10.0))
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.return_value = {
            "pv_name": "TEST:PV",
            "value": 80.0,
            "control": {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0},
        }

        with (
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
            pytest.raises(PVWriteStepError) as excinfo,
        ):
            await _set_pv_value("TEST:PV", "120")

        # The put NEVER happened: the value never reached the IOC.
        mock_pv_put.assert_not_awaited()
        # Its OWN code, never the gate's: a post-admission refusal that wore PV_WRITE_DENIED would
        # make an un-audited-looking refusal indistinguishable from an audited gate verdict.
        assert excinfo.value.error_code == "PV_WRITE_STEP_TOO_LARGE"
        assert "event=STEP_DENY" in caplog.text
        # Like BOUNDS_DENY (QA-39): a pre-dispatch refusal carries NO op= token, because the id is
        # issued when a write is dispatched and this one never was.
        assert _audit_events(caplog) == [("STEP_DENY", None)]
        # A refused-before-put write emits no ATTEMPT/ALLOW/READBACK.
        assert "event=ATTEMPT" not in caplog.text
        assert "event=ALLOW" not in caplog.text
        assert "READBACK" not in caplog.text

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_bounds_is_reported_before_step_when_both_fail(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Order pin: out of range is the more fundamental fault and is the one reported.

        130 is both outside [0, 120] and 50 away from 80. A caller told "step too large" would fix
        the step and hit the range next; told "out of range" they fix the real fault. Red-provable
        by moving the step block above the bounds block in ``_set_pv_value``.
        """
        monkeypatch.setattr(write_module, "get_config", lambda: self._armed(10.0))
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.return_value = {
            "pv_name": "TEST:PV",
            "value": 80.0,
            "control": {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0},
        }

        with pytest.raises(PVWriteBoundsError):
            await _set_pv_value("TEST:PV", "130")
        mock_pv_put.assert_not_awaited()

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_small_step_proceeds_with_no_step_note(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(write_module, "get_config", lambda: self._armed(10.0))
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        control = {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0}
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 80.0, "control": control},
            {"pv_name": "TEST:PV", "value": 85.0, "control": control},
        ]
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "85")
        assert result["status"] == "success"
        assert result["step_note"] is None
        mock_pv_put.assert_awaited_once()

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_enum_record_is_not_step_checked_and_says_so(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The sanctioned command lane: an armed limit below 1 must not block a reset.

        Built through the real client shape (``_enum_readback``), because the whole branch keys on
        the ``enum`` block that ``_format_value`` puts there, and a hand-typed dict could assert a
        pass this code never earns. A limit of 0.5 with an index moving 0 to 1 is exactly the
        arithmetic that would refuse without the branch.
        """
        monkeypatch.setattr(write_module, "get_config", lambda: self._armed(0.5))
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        mock_pv_get.side_effect = [
            _enum_readback(0, ["Idle", "Reset"]),
            _enum_readback(1, ["Idle", "Reset"]),
        ]
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "1")
        assert result["status"] == "success"
        mock_pv_put.assert_awaited_once()
        assert isinstance(result["step_note"], str) and "enum" in result["step_note"]

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_non_finite_refusal_reports_no_distance_it_did_not_measure(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refusal must not report a measurement that was never taken.

        On the non-finite branch ``StepVerdict.step`` is None, because a distance from ``nan`` is
        not a number. An earlier draft interpolated the field anyway and the caller was told the
        write "moves it by None". The path is reachable rather than theoretical: the bounds check
        upstream only refuses a non-finite value when the record DECLARES drive limits, so a record
        without a control block falls through to here, which is the ordinary case.

        RED-PROOF: put ``step.step`` back into the message and the assertion below reports "None"
        on the wire.
        """
        monkeypatch.setattr(write_module, "get_config", lambda: self._armed(10.0))
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        # No control block, so the bounds check fails open and the step check is what refuses.
        mock_pv_get.return_value = {"pv_name": "TEST:PV", "value": 80.0}

        with pytest.raises(PVWriteStepError) as excinfo:
            await _set_pv_value("TEST:PV", "nan")

        mock_pv_put.assert_not_awaited()
        message = str(excinfo.value)
        assert "None" not in message, f"the refusal reports a distance it never measured: {message}"
        assert "is not finite" in message
        # The structured detail may carry the honest None; the human-facing message may not.
        assert excinfo.value.details["step"] is None

    @patch("epics_mcp.tools.write.pv_put", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.pv_get", new_callable=AsyncMock)
    @patch("epics_mcp.tools.write.get_safety")
    async def test_unset_limit_checks_nothing_and_says_nothing(
        self,
        mock_get_safety: MagicMock,
        mock_pv_get: AsyncMock,
        mock_pv_put: AsyncMock,
    ) -> None:
        """The shipping posture: no limit configured, so the biggest possible jump goes through.

        This is the pin on "no existing deployment changes behaviour". The autouse fixture already
        pins the limit to 0, so this test states the default rather than arming anything, and it
        drives a 0-to-120 write, the widest the record allows.
        """
        mock_get_safety.return_value = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        control = {"limit_low": 0.0, "limit_high": 120.0, "min_step": 0.0}
        mock_pv_get.side_effect = [
            {"pv_name": "TEST:PV", "value": 0.0, "control": control},
            {"pv_name": "TEST:PV", "value": 120.0, "control": control},
        ]
        mock_pv_put.return_value = None

        result = await _set_pv_value("TEST:PV", "120")
        assert result["status"] == "success"
        mock_pv_put.assert_awaited_once()
        # Silent when the feature is off: no note on every write of a default server.
        assert result["step_note"] is None
