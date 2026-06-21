"""Tests for the SafetyLayer (write gate, pattern allowlist, rate limiting, audit)."""

import logging

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import PVWriteDeniedError, RateLimitError, SafetyConfigError
from epics_pv_mcp.safety import SafetyLayer, get_safety


class TestWriteGate:
    """Environment gate: allow_pv_write must be True."""

    def test_write_denied_when_disabled(self, safety_locked: SafetyLayer) -> None:
        with pytest.raises(PVWriteDeniedError):
            safety_locked.check_write_allowed("any:pv")

    def test_write_allowed_when_enabled(self, safety: SafetyLayer) -> None:
        # Should not raise
        safety.check_write_allowed("any:pv")


class TestPatternAllowlist:
    """PV name must match the configured regex pattern."""

    def test_pattern_mismatch_raises(self) -> None:
        cfg = EpicsConfig(
            allow_pv_write=True,
            pv_write_pattern=r"^TEST:.*$",
            write_rate_limit=10,
        )
        sl = SafetyLayer(cfg)
        with pytest.raises(PVWriteDeniedError):
            sl.check_write_allowed("OTHER:pv")

    def test_pattern_match_passes(self) -> None:
        cfg = EpicsConfig(
            allow_pv_write=True,
            pv_write_pattern=r"^TEST:.*$",
            write_rate_limit=10,
        )
        sl = SafetyLayer(cfg)
        # Should not raise
        sl.check_write_allowed("TEST:pv")

    def test_empty_pattern_allows_all(self, safety: SafetyLayer) -> None:
        # Default empty pattern means no pattern check
        safety.check_write_allowed("ANYTHING:goes")


class TestRateLimit:
    """Sliding-window rate limit enforcement."""

    def test_rate_limit_exceeded(self) -> None:
        cfg = EpicsConfig(allow_pv_write=True, write_rate_limit=5)
        sl = SafetyLayer(cfg)

        # First 5 calls should succeed
        for i in range(5):
            sl.check_write_allowed(f"TEST:pv{i}")

        # 6th call should raise
        with pytest.raises(RateLimitError):
            sl.check_write_allowed("TEST:pv_overflow")

    def test_rate_limit_error_has_details(self) -> None:
        cfg = EpicsConfig(allow_pv_write=True, write_rate_limit=2)
        sl = SafetyLayer(cfg)
        sl.check_write_allowed("A:pv")
        sl.check_write_allowed("B:pv")

        with pytest.raises(RateLimitError) as exc_info:
            sl.check_write_allowed("C:pv")

        assert exc_info.value.error_code == "RATE_LIMIT_EXCEEDED"
        assert exc_info.value.details["limit"] == 2


class TestAuditWrite:
    """Verify audit_write logs correctly."""

    def test_audit_write_logs_info(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0)

        # Back-compat: line still starts with PV_WRITE and carries the PV name.
        assert any("PV_WRITE" in record.message for record in caplog.records)
        assert any("TEST:pv" in record.message for record in caplog.records)

    def test_audit_write_records_event_and_caller(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0)

        assert "event=ALLOW" in caplog.text
        assert "caller=set_pv_value" in caplog.text
        assert "old=10.0" in caplog.text
        assert "new=20.0" in caplog.text

    def test_audit_write_failed_record(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write_failed("TEST:pv", 1, 2, "PV_TIMEOUT")

        assert "event=FAILED" in caplog.text
        assert "error_code=PV_TIMEOUT" in caplog.text
        assert "TEST:pv" in caplog.text


class TestAuditDeny:
    """Rejected writes must leave a DENY audit record — and consume no rate token."""

    def test_gate_off_emits_deny(
        self, safety_locked: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(PVWriteDeniedError),
        ):
            safety_locked.check_write_allowed("X:pv")

        assert "event=DENY" in caplog.text
        assert "error_code=PV_WRITE_DENIED" in caplog.text
        # Negative: a denied write must never emit an ALLOW record.
        assert "event=ALLOW" not in caplog.text

    def test_pattern_mismatch_emits_deny(self, caplog: pytest.LogCaptureFixture) -> None:
        sl = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r"^TEST:.*$", write_rate_limit=10)
        )
        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(PVWriteDeniedError),
        ):
            sl.check_write_allowed("OTHER:pv")

        assert "event=DENY" in caplog.text
        assert "error_code=PV_WRITE_DENIED" in caplog.text

    def test_rate_limit_emits_deny(self, caplog: pytest.LogCaptureFixture) -> None:
        sl = SafetyLayer(EpicsConfig(allow_pv_write=True, write_rate_limit=1))
        sl.check_write_allowed("TEST:a")  # consumes the single token
        with (
            caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"),
            pytest.raises(RateLimitError),
        ):
            sl.check_write_allowed("TEST:b")

        assert "event=DENY" in caplog.text
        assert "error_code=RATE_LIMIT_EXCEEDED" in caplog.text

    def test_deny_consumes_no_rate_token(self) -> None:
        # 3 pattern-denied attempts must NOT consume tokens: exactly
        # write_rate_limit (=2) real writes still succeed afterwards.
        sl = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r"^TEST:.*$", write_rate_limit=2)
        )
        for _ in range(3):
            with pytest.raises(PVWriteDeniedError):
                sl.check_write_allowed("OTHER:denied")

        sl.check_write_allowed("TEST:1")
        sl.check_write_allowed("TEST:2")
        with pytest.raises(RateLimitError):
            sl.check_write_allowed("TEST:3")


class TestSafetyConfig:
    """Fail-closed Konfig-Validierung + thread-sicherer Singleton."""

    def test_invalid_pattern_raises_safety_config_error(self) -> None:
        # Ein kaputtes Allowlist-Regex darf die Schreib-Sperre nicht still
        # aushebeln, sondern klar scheitern.
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern="[unclosed")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(cfg)

    def test_get_safety_singleton_under_threads(self) -> None:
        import threading

        import epics_pv_mcp.safety as safety_mod

        original = safety_mod._safety
        safety_mod._safety = None
        try:
            barrier = threading.Barrier(8)
            results: list[SafetyLayer] = []
            append_lock = threading.Lock()

            def worker() -> None:
                barrier.wait()
                instance = get_safety()
                with append_lock:
                    results.append(instance)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(results) == 8
            assert all(r is results[0] for r in results)
        finally:
            safety_mod._safety = original
