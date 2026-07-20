"""Tests for the SafetyLayer (write gate, pattern allowlist, rate limiting, audit)."""

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import PVWriteDeniedError, RateLimitError, SafetyConfigError
from epics_pv_mcp.safety import SafetyLayer, get_safety

# E8: constructing a writes-ON SafetyLayer now asserts the process EPICS search env is
# loopback-only. The autouse env strip (conftest) leaves *_AUTO_ADDR_LIST unset = broadcast ON,
# which the reach assert (correctly) rejects — so every writes-on construction in this module
# runs under the loopback lane. The reach go-red tests below override a var ON TOP of it.
pytestmark = pytest.mark.usefixtures("loopback_write_env")


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

    def test_empty_pattern_with_writes_on_raises(self) -> None:
        # S22: writes ENABLED with an EMPTY allowlist pattern is a misconfiguration — every PV
        # would be writable. It must fail closed at construction (SafetyConfigError), not
        # warn-and-allow. This replaces the former test_empty_pattern_allows_all, which cemented
        # the allow-all footgun.
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern="", write_rate_limit=10)
        with pytest.raises(SafetyConfigError):
            SafetyLayer(cfg)

    def test_explicit_allow_all_pattern_is_permitted(self) -> None:
        # Positive control: the guard forbids the SILENT empty default, not a DELIBERATE choice.
        # An operator may allow every PV with an explicit '.*' — that must construct fine.
        SafetyLayer(EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10))


class TestWriteReachAssert:
    """E8: writes ENABLED requires a loopback-only EPICS client search reach, else it fails closed.

    The reach assert reads the process env by default (what p4p/libca read); these tests run under
    the module-level loopback lane and override ONE var to a non-loopback value to prove each go-red
    path. The negative (loopback lane accepted) proves the guard is not a blanket refusal.
    """

    def _writes_on(self) -> EpicsConfig:
        return EpicsConfig(allow_pv_write=True, pv_write_pattern=r"^SIM:.*$", write_rate_limit=10)

    def test_loopback_lane_constructs(self) -> None:
        # Positive control: the sandbox loopback lane (set by loopback_write_env) is accepted.
        SafetyLayer(self._writes_on())

    def test_public_pva_addr_list_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "192.0.2.10")  # ESS-like public IP
        with pytest.raises(SafetyConfigError) as exc:
            SafetyLayer(self._writes_on())
        assert "EPICS_PVA_ADDR_LIST" in str(exc.value)

    def test_pva_name_servers_public_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_PVA_NAME_SERVERS", "gateway.example.org:5075")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_auto_addr_list_yes_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Subnet broadcast back ON — the exact combination the hard weiche forbids.
        monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "YES")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_auto_addr_list_unset_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Unset means broadcast ON (EPICS default) — must be rejected, not treated as "no target".
        monkeypatch.delenv("EPICS_PVA_AUTO_ADDR_LIST", raising=False)
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_host_docker_internal_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A hostname is never trusted as loopback even if DNS would resolve it to 127.0.0.1.
        monkeypatch.setenv("EPICS_CA_ADDR_LIST", "host.docker.internal")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_ipv6_non_loopback_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "[2001:db8::1]:5075")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_ipv6_loopback_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Positive control: ::1 IS loopback — a bracketed IPv6 loopback with a port constructs.
        monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "[::1]:5075")
        SafetyLayer(self._writes_on())

    def test_ca_auto_addr_list_yes_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both providers are checked regardless of the configured provider.
        monkeypatch.setenv("EPICS_CA_AUTO_ADDR_LIST", "YES")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_reach_check_skipped_when_writes_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The gate is a WRITE concern only: a read-only deploy with a wide-open reach constructs.
        monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "192.0.2.10")
        monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "YES")
        SafetyLayer(EpicsConfig(allow_pv_write=False))

    def test_injected_environ_overrides_process_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A public process env is IGNORED when a loopback environ is injected (determinism seam).
        monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "192.0.2.10")
        SafetyLayer(
            self._writes_on(),
            environ={
                "EPICS_PVA_ADDR_LIST": "127.0.0.1",
                "EPICS_PVA_AUTO_ADDR_LIST": "NO",
                "EPICS_CA_AUTO_ADDR_LIST": "NO",
            },
        )


class TestSafetyConfigGuard:
    """G5: an out-of-range write_rate_limit fails closed, not as a bare ValueError."""

    def test_negative_rate_limit_raises_safety_config_error(self) -> None:
        # model_construct bypasses G2's ge=1 validation — the SafetyLayer guard
        # must convert deque(maxlen=-1)'s bare ValueError into SafetyConfigError.
        cfg = EpicsConfig.model_construct(write_rate_limit=-1)
        with pytest.raises(SafetyConfigError):
            SafetyLayer(cfg)


class TestRateLimit:
    """Sliding-window rate limit enforcement."""

    def test_rate_limit_exceeded(self) -> None:
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=5)
        sl = SafetyLayer(cfg)

        # First 5 calls should succeed
        for i in range(5):
            sl.check_write_allowed(f"TEST:pv{i}")

        # 6th call should raise
        with pytest.raises(RateLimitError):
            sl.check_write_allowed("TEST:pv_overflow")

    def test_rate_limit_error_has_details(self) -> None:
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=2)
        sl = SafetyLayer(cfg)
        sl.check_write_allowed("A:pv")
        sl.check_write_allowed("B:pv")

        with pytest.raises(RateLimitError) as exc_info:
            sl.check_write_allowed("C:pv")

        assert exc_info.value.error_code == "RATE_LIMIT_EXCEEDED"
        assert exc_info.value.details["limit"] == 2

    def test_rate_limit_token_acquisition_is_atomic(
        self, concurrent_admit_count: Callable[..., int]
    ) -> None:
        """S28: the rate token acquisition (purge->len-check->append) is atomic under _rate_lock,
        symmetric with OlogWriteGate. Two concurrent writes at limit=1 admit exactly one. The PV
        path is inline-on-the-event-loop today (not racy yet), so this guards the invariant against
        a future move to threads (O5); it goes RED (admits==2) if the lock is removed (mutant)."""
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=1)
        sl = SafetyLayer(cfg)
        admits = concurrent_admit_count(sl, lambda: sl.check_write_allowed("TEST:pv"))
        assert admits == 1


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

    def test_audit_write_attempt_record(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S24: the ATTEMPT record (before the I/O) carries the new value + correlating op."""
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write_attempt("TEST:pv", 20.0, "w7")

        assert "event=ATTEMPT" in caplog.text
        assert "new=20.0" in caplog.text
        assert "op=w7" in caplog.text
        assert "caller=set_pv_value" in caplog.text

    def test_audit_write_unknown_record(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S24: a cancelled-mid-put write is recorded UNKNOWN_PENDING, not FAILED (put may land)."""
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write_unknown("TEST:pv", 10.0, 20.0, "w7")

        assert "event=UNKNOWN_PENDING" in caplog.text
        assert "old=10.0" in caplog.text
        assert "new=20.0" in caplog.text
        assert "op=w7" in caplog.text

    def test_audit_write_carries_operation_id_when_given(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S24: ALLOW/FAILED accept an op that correlates them with their ATTEMPT line; the default
        ``"-"`` (a direct, non-tool call) keeps the pre-S24 positional call sites green."""
        with caplog.at_level(logging.INFO, logger="epics_pv_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0, operation_id="w3")
            safety.audit_write("TEST:pv", 10.0, 20.0)  # default op

        assert "op=w3" in caplog.text
        assert "op=-" in caplog.text


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
        sl = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=1)
        )
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

    def test_invalid_audit_path_raises_safety_config_error(self, tmp_path: Path) -> None:
        # Ein kaputter/nicht schreibbarer Audit-Pfad darf nicht erst beim ersten Write
        # als roher FileNotFoundError crashen, sondern fail-closed scheitern (symmetrisch
        # zur Regex-Validierung). Den prozess-globalen Audit-Logger leeren, damit der
        # FileHandler überhaupt erzeugt wird (sonst greift "if not audit.handlers").
        audit = logging.getLogger("epics_pv_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            cfg = EpicsConfig(audit_log_file=str(tmp_path / "nope" / "audit.log"))
            with pytest.raises(SafetyConfigError):
                SafetyLayer(cfg)
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_audit_path_validated_on_repeated_construction(self, tmp_path: Path) -> None:
        # QA 2026-07-17: the audit-path guard must not be skipped just because an EARLIER
        # SafetyLayer already attached a handler to the process-global audit logger. A later
        # SafetyLayer with a broken audit path must STILL fail closed (regression guard for the
        # `if not audit.handlers` block that used to gate the path validation too).
        audit = logging.getLogger("epics_pv_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            SafetyLayer(EpicsConfig())  # first construction registers a stderr handler
            with pytest.raises(SafetyConfigError):
                SafetyLayer(EpicsConfig(audit_log_file=str(tmp_path / "nope" / "audit.log")))
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

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
