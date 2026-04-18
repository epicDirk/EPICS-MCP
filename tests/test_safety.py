"""Tests for the SafetyLayer (write gate, pattern allowlist, rate limiting, audit)."""

import logging

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import PVWriteDeniedError, RateLimitError
from epics_pv_mcp.safety import SafetyLayer


class TestWriteGate:
    """Environment gate: allow_pv_write must be True."""

    def test_write_denied_when_disabled(self, safety_locked):
        with pytest.raises(PVWriteDeniedError):
            safety_locked.check_write_allowed("any:pv")

    def test_write_allowed_when_enabled(self, safety):
        # Should not raise
        safety.check_write_allowed("any:pv")


class TestPatternAllowlist:
    """PV name must match the configured regex pattern."""

    def test_pattern_mismatch_raises(self):
        cfg = EpicsConfig(
            allow_pv_write=True,
            pv_write_pattern=r"^TEST:.*$",
            write_rate_limit=10,
        )
        sl = SafetyLayer(cfg)
        with pytest.raises(PVWriteDeniedError):
            sl.check_write_allowed("OTHER:pv")

    def test_pattern_match_passes(self):
        cfg = EpicsConfig(
            allow_pv_write=True,
            pv_write_pattern=r"^TEST:.*$",
            write_rate_limit=10,
        )
        sl = SafetyLayer(cfg)
        # Should not raise
        sl.check_write_allowed("TEST:pv")

    def test_empty_pattern_allows_all(self, safety):
        # Default empty pattern means no pattern check
        safety.check_write_allowed("ANYTHING:goes")


class TestRateLimit:
    """Sliding-window rate limit enforcement."""

    def test_rate_limit_exceeded(self):
        cfg = EpicsConfig(allow_pv_write=True, write_rate_limit=5)
        sl = SafetyLayer(cfg)

        # First 5 calls should succeed
        for i in range(5):
            sl.check_write_allowed(f"TEST:pv{i}")

        # 6th call should raise
        with pytest.raises(RateLimitError):
            sl.check_write_allowed("TEST:pv_overflow")

    def test_rate_limit_error_has_details(self):
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

    def test_audit_write_logs_info(self, safety, caplog):
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0)

        assert any("PV_WRITE" in record.message for record in caplog.records)
        assert any("TEST:pv" in record.message for record in caplog.records)
