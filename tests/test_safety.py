"""Tests for the SafetyLayer (write gate, pattern allowlist, rate limiting, audit)."""

import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from epics_mcp.config import EpicsConfig
from epics_mcp.errors import PVWriteDeniedError, RateLimitError, SafetyConfigError
from epics_mcp.safety import SafetyLayer, get_safety

# E8: constructing a writes-ON SafetyLayer now asserts the process EPICS search env is
# loopback-only. The autouse env strip (conftest) leaves *_AUTO_ADDR_LIST unset = broadcast ON,
# which the reach assert (correctly) rejects, so every writes-on construction in this module
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
        # S22: writes ENABLED with an EMPTY allowlist pattern is a misconfiguration, every PV
        # would be writable. It must fail closed at construction (SafetyConfigError), not
        # warn-and-allow. This replaces the former test_empty_pattern_allows_all, which cemented
        # the allow-all footgun.
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern="", write_rate_limit=10)
        with pytest.raises(SafetyConfigError):
            SafetyLayer(cfg)

    def test_explicit_allow_all_pattern_is_permitted(self) -> None:
        # Positive control: the guard forbids the SILENT empty default, not a DELIBERATE choice.
        # An operator may allow every PV with an explicit '.*', that must construct fine.
        SafetyLayer(EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10))


class TestDenyMessageEscalation:
    """BG-DOC(c): what a refused write TELLS the assistant that read it.

    The message is the only surface a denied caller sees, and until now it named a remedy and
    stopped there. An assistant told "not this PV" and nothing else has an obvious next move, and
    it is the wrong one: try a neighbouring name, or a route that is not gated. So EVERY refusal
    raised out of :meth:`SafetyLayer.check_write_allowed` carries the escalation, not only the
    disabled gate the ticket named. The allowlist miss is the more dangerous of the two, because it
    names a PV, and naming one PV is what invites trying its neighbour.

    ⚠️ Deliberately NOT covered, so nobody reads the class name as wider than it is:
    ``PVWriteBoundsError`` (a SUBCLASS of ``PVWriteDeniedError``, raised in ``tools/write.py``) and
    the rate-limit denial, which raises ``RateLimitError``. Both refuse a write and neither carries
    this sentence. The bounds case has the same shape of temptation ("then a value just inside the
    limit"), so it is a gap rather than a decision; it is named here because the first version of
    this docstring said "BOTH paths" and a reader counting raise sites would have found three.

    Each marker is asserted WITH its polarity, which the first version of this test got wrong. It
    checked the neutral phrases "work around" and "operator on duty" alone, and the sentence
    "If you need to work around this, ask the operator on duty." would have satisfied both while
    inverting the instruction. Asserting ``_ESCALATION in msg`` is not the alternative, that is the
    tautology of comparing the constant to itself; the alternative is to pin the parts that carry
    the meaning, which is the posture
    ``test_consent_meta_tools_document_the_client_scope`` takes for the same class of claim.

    Provably red: drop ``+ _ESCALATION`` from either raise in ``safety.check_write_allowed``, let
    the escalation REPLACE the gate remedy rather than stand beside it, or soften the prohibition
    into a permission the way the sentence above does.
    """

    def _denial(self, act: Callable[[], None]) -> str:
        with pytest.raises(PVWriteDeniedError) as excinfo:
            act()
        return str(excinfo.value)

    def _assert_escalates(self, message: str) -> None:
        """The instruction must FORBID the workaround, name both of its routes, and escalate."""
        assert "Do NOT work around this" in message, "the prohibition is not stated as one"
        assert "a different PV" in message, "the neighbouring-PV route is not named"
        assert "another route" in message, "the other-route case is not named"
        assert "Report the refusal to the operator on duty" in message

    def test_the_disabled_gate_tells_the_caller_not_to_route_around_it(
        self, safety_locked: SafetyLayer
    ) -> None:
        self._assert_escalates(self._denial(lambda: safety_locked.check_write_allowed("any:pv")))

    def test_the_allowlist_miss_tells_the_caller_not_to_try_a_neighbour(self) -> None:
        sl = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r"^TEST:.*$", write_rate_limit=10)
        )
        self._assert_escalates(self._denial(lambda: sl.check_write_allowed("OTHER:pv")))

    def test_the_escalation_stands_beside_the_remedy_and_not_instead_of_it(
        self, safety_locked: SafetyLayer
    ) -> None:
        """The gate message must keep naming the variable that arms it.

        Separate from the two above because it is the failure mode a well-meaning edit produces:
        hardening a message reads like a reason to stop advertising the switch, and then a reader
        with every right to enable writes cannot find out how.
        """
        message = self._denial(lambda: safety_locked.check_write_allowed("any:pv"))
        assert "EPICS_MCP_ALLOW_PV_WRITE=true" in message
        assert message.index("EPICS_MCP_ALLOW_PV_WRITE") < message.index("Do NOT work around")


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
        # Subnet broadcast back ON: the exact combination the hard gate forbids.
        monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "YES")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(self._writes_on())

    def test_auto_addr_list_unset_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Unset means broadcast ON (EPICS default): must be rejected, not treated as "no target".
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
        # Positive control: ::1 IS loopback, a bracketed IPv6 loopback with a port constructs.
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
        # model_construct bypasses G2's ge=1 validation, the SafetyLayer guard
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
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0)

        # Back-compat: line still starts with PV_WRITE and carries the PV name.
        assert any("PV_WRITE" in record.message for record in caplog.records)
        assert any("TEST:pv" in record.message for record in caplog.records)

    def test_audit_write_records_event_and_caller(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0)

        assert "event=ALLOW" in caplog.text
        assert "caller=set_pv_value" in caplog.text
        assert "old=10.0" in caplog.text
        assert "new=20.0" in caplog.text

    def test_audit_write_failed_record(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_write_failed("TEST:pv", 1, 2, "PV_TIMEOUT")

        assert "event=FAILED" in caplog.text
        assert "error_code=PV_TIMEOUT" in caplog.text
        assert "TEST:pv" in caplog.text

    def test_audit_write_attempt_record(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S24: the ATTEMPT record (before the I/O) carries the new value + correlating op."""
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_write_attempt("TEST:pv", 20.0, "w7")

        assert "event=ATTEMPT" in caplog.text
        assert "new=20.0" in caplog.text
        assert "op=w7" in caplog.text
        assert "caller=set_pv_value" in caplog.text


class TestAuditReadback:
    """O3: audit_readback maps the tri-state verdict to READBACK_OK/MISMATCH/UNVERIFIED and carries
    the written + readback values plus the op that correlates it to the same write's ALLOW line."""

    def test_readback_ok(self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_readback("TEST:pv", 20.0, 20.0, True, operation_id="w7")
        assert "event=READBACK_OK" in caplog.text
        assert "written=20.0" in caplog.text
        assert "readback=20.0" in caplog.text
        assert "op=w7" in caplog.text
        assert "caller=set_pv_value" in caplog.text

    def test_readback_mismatch(self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_readback("TEST:pv", 20.0, 19.0, False, operation_id="w7")
        assert "event=READBACK_MISMATCH" in caplog.text

    def test_readback_unverified_on_none(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        # verified None (readback not obtained) → UNVERIFIED, never OK, never MISMATCH.
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_readback("TEST:pv", 20.0, None, None, operation_id="w7")
        assert "event=READBACK_UNVERIFIED" in caplog.text
        assert "event=READBACK_OK" not in caplog.text
        assert "event=READBACK_MISMATCH" not in caplog.text

    def test_audit_write_unknown_record(
        self, safety: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S24: a cancelled-mid-put write is recorded UNKNOWN_PENDING, not FAILED (put may land)."""
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
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
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            safety.audit_write("TEST:pv", 10.0, 20.0, operation_id="w3")
            safety.audit_write("TEST:pv", 10.0, 20.0)  # default op

        assert "op=w3" in caplog.text
        assert "op=-" in caplog.text


class TestAuditDeny:
    """Rejected writes must leave a DENY audit record, and consume no rate token."""

    def test_gate_off_emits_deny(
        self, safety_locked: SafetyLayer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
            pytest.raises(PVWriteDeniedError),
        ):
            safety_locked.check_write_allowed("X:pv")

        assert "event=DENY" in caplog.text
        assert "error_code=PV_WRITE_DENIED" in caplog.text
        # Negative: a denied write must never emit an ALLOW record.
        assert "event=ALLOW" not in caplog.text
        # QA-39: the gate refusal carries NO op= token, matching what the operator guide states.
        # The id is issued on dispatch and nothing was dispatched. Red-provable by adding op=%s
        # to _audit_deny.
        assert "op=" not in caplog.text

    def test_pattern_mismatch_emits_deny(self, caplog: pytest.LogCaptureFixture) -> None:
        sl = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r"^TEST:.*$", write_rate_limit=10)
        )
        with (
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
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
            caplog.at_level(logging.INFO, logger="epics_mcp.audit"),
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


class TestAuditBoundsDeny:
    """The O2 value-bounds refusal leaves a BOUNDS_DENY audit record (event + value + limits)."""

    def test_bounds_deny_emits_record(self, caplog: pytest.LogCaptureFixture) -> None:
        sl = SafetyLayer(
            EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=10)
        )
        with caplog.at_level(logging.INFO, logger="epics_mcp.audit"):
            sl.audit_bounds_deny("TEST:pv", "130", 0.0, 120.0)

        assert "event=BOUNDS_DENY" in caplog.text
        assert "pv=TEST:pv" in caplog.text
        assert "value='130'" in caplog.text
        assert "limit_low=0.0" in caplog.text
        assert "limit_high=120.0" in caplog.text


class TestSafetyConfig:
    """Fail-closed config validation plus a thread-safe singleton."""

    def test_invalid_pattern_raises_safety_config_error(self) -> None:
        # A broken allowlist regex must not quietly disable the write lock;
        # it has to fail loudly instead.
        cfg = EpicsConfig(allow_pv_write=True, pv_write_pattern="[unclosed")
        with pytest.raises(SafetyConfigError):
            SafetyLayer(cfg)

    def test_invalid_audit_path_raises_safety_config_error(self, tmp_path: Path) -> None:
        # A broken or unwritable audit path must not crash as a raw FileNotFoundError at the
        # first write; it must fail closed, symmetric to the regex validation. The
        # process-global audit logger is cleared so the FileHandler is built at all
        # (otherwise "if not audit.handlers" short-circuits).
        audit = logging.getLogger("epics_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            cfg = EpicsConfig(audit_log_file=str(tmp_path / "nope" / "audit.log"))
            with pytest.raises(SafetyConfigError):
                SafetyLayer(cfg)
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_an_audit_path_that_is_not_a_path_fails_closed_too(self) -> None:
        """The promise at the ``except`` is "a broken audit path fails HERE as a
        SafetyConfigError", and two broken paths used to escape it.

        A NUL byte raises ``ValueError`` out of the builtin ``open`` and a non-str raises
        ``TypeError`` out of ``os.fspath``; neither is an ``OSError``, so the clause caught neither
        and the process died on a bare traceback instead of the named refusal. Unreachable through
        the environment (an env value cannot carry a NUL) and reachable through a config that
        bypassed validation, which is the same bypass this class already guards against for
        ``write_rate_limit``.

        Red-proof: narrow the clause back to ``except OSError`` and both cases raise the raw
        builtin exception instead.
        """
        audit = logging.getLogger("epics_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            with pytest.raises(SafetyConfigError):  # ValueError out of the builtin open
                SafetyLayer(EpicsConfig.model_construct(audit_log_file="audit\x00.log"))
            with pytest.raises(SafetyConfigError):  # TypeError out of os.fspath
                SafetyLayer(EpicsConfig.model_construct(audit_log_file=3))  # type: ignore[arg-type]
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_audit_path_validated_on_repeated_construction(self, tmp_path: Path) -> None:
        # QA 2026-07-17: the audit-path guard must not be skipped just because an EARLIER
        # SafetyLayer already attached a handler to the process-global audit logger. A later
        # SafetyLayer with a broken audit path must STILL fail closed (regression guard for the
        # `if not audit.handlers` block that used to gate the path validation too).
        audit = logging.getLogger("epics_mcp.audit")
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

        import epics_mcp.safety as safety_mod

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


class TestAuditSink:
    """K1/K2: the audit FileHandler must encode UTF-8 AND stamp UTC.

    Both defects lose data SILENTLY. A micro sign, an ohm sign or an accented letter (real EPICS
    units, non-ASCII names) in an audit line raises a ``UnicodeEncodeError`` under the platform
    locale (Windows cp1252) without ``encoding="utf-8"``, and the stdlib
    ``Handler.handleError`` SWALLOWS it, so the line vanishes without trace. A naive local-time
    stamp is ambiguous when an incident is reconstructed across time zones and daylight saving.
    """

    def test_audit_file_handler_encodes_utf8(self, tmp_path: Path) -> None:
        # K1 (portable red proof): the FileHandler carries an explicit encoding="utf-8". Without
        # it ``.encoding`` is None (the platform locale), which measures red on every platform.
        audit = logging.getLogger("epics_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            sl = SafetyLayer(EpicsConfig(audit_log_file=str(tmp_path / "audit.log")))
            handler = sl._audit_handler
            assert isinstance(handler, logging.FileHandler)  # a real file sink
            assert handler.encoding == "utf-8"
        finally:
            for h in audit.handlers[:]:
                h.close()
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_audit_line_with_unicode_units_survives(self, tmp_path: Path) -> None:
        # K1 (functional evidence): an audit line with non-ASCII characters reaches the file
        # unaltered. Under cp1252 (Windows) it vanishes without encoding="utf-8" (handleError
        # swallows the UnicodeEncodeError). U+03A9 has no cp1252 mapping, so it is a safe trigger.
        audit = logging.getLogger("epics_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            log_path = tmp_path / "audit.log"
            sl = SafetyLayer(EpicsConfig(audit_log_file=str(log_path)))
            probe = "SIM:PS-01:Cur-RB 12 μA 50 Ω probe-äöü"
            sl._emit("UNIT_PROBE pv=%s", probe)
            handler = sl._audit_handler
            assert handler is not None
            handler.flush()
            assert probe in log_path.read_text(encoding="utf-8")
        finally:
            for h in audit.handlers[:]:
                h.close()
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_audit_formatter_stamps_utc(self, tmp_path: Path) -> None:
        # K2: the formatter must convert to time.gmtime (UTC) and end with a literal 'Z'.
        # Framework time stays framework time: no datetime.now() in the logic.
        audit = logging.getLogger("epics_mcp.audit")
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            sl = SafetyLayer(EpicsConfig(audit_log_file=str(tmp_path / "audit.log")))
            handler = sl._audit_handler
            assert isinstance(handler, logging.FileHandler)
            formatter = handler.formatter
            assert formatter is not None
            assert formatter.converter is time.gmtime
            record = logging.LogRecord(
                "epics_mcp.audit", logging.INFO, __file__, 1, "m", None, None
            )
            assert formatter.formatTime(record, formatter.datefmt).endswith("Z")
        finally:
            for h in audit.handlers[:]:
                h.close()
            audit.handlers.clear()
            audit.handlers.extend(saved)
