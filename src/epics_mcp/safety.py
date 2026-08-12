"""Safety layer for PV write operations, gate, allowlist, rate-limit, audit."""

import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping

from epics_mcp.config import EpicsConfig, get_config
from epics_mcp.epics_address import write_reach_violations
from epics_mcp.errors import PVWriteDeniedError, RateLimitError, SafetyConfigError

logger = logging.getLogger(__name__)

_safety: "SafetyLayer | None" = None
_safety_lock = threading.Lock()


def get_safety() -> "SafetyLayer":
    """Return singleton SafetyLayer instance (thread-safe)."""
    global _safety
    with _safety_lock:
        if _safety is None:
            _safety = SafetyLayer(get_config())
    return _safety


class SafetyLayer:
    """Guards all PV write operations with three checks:

    1. Environment gate: ``allow_pv_write`` must be True.
    2. Pattern allowlist: PV name must match ``pv_write_pattern`` regex. REQUIRED when writes are
       enabled: an empty pattern with ``allow_pv_write`` on raises ``SafetyConfigError`` at
       construction (fail-closed), never a silent allow-all.
    3. Rate limit: at most ``write_rate_limit`` writes per 60 s window.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, config: EpicsConfig, environ: Mapping[str, str] | None = None) -> None:
        self._config = config
        # Fail-closed: a broken allowlist pattern must NOT quietly disable the write lock.
        # Failing loudly is better than writing unguarded.
        try:
            self._pattern: re.Pattern[str] | None = (
                re.compile(config.pv_write_pattern) if config.pv_write_pattern else None
            )
        except re.error as exc:
            raise SafetyConfigError(
                f"Invalid EPICS_MCP_PV_WRITE_PATTERN regex {config.pv_write_pattern!r}: {exc}",
                details={"pattern": config.pv_write_pattern},
            ) from exc
        # Fail-closed: writes ENABLED without a PV-name allowlist would leave only the on/off env
        # gate, every PV becomes writable. Refuse at construction rather than warn-and-allow: an
        # empty pattern with writes on is a misconfiguration (a forgotten pattern env var), not a
        # valid "allow all". An operator who genuinely wants every PV writable must say so
        # explicitly (e.g. '.*'), this turns the silent footgun into a loud SafetyConfigError.
        if config.allow_pv_write and self._pattern is None:
            raise SafetyConfigError(
                "PV writes are ENABLED (EPICS_MCP_ALLOW_PV_WRITE=true) but "
                "EPICS_MCP_PV_WRITE_PATTERN is empty, refusing to start with every PV writable. "
                "Set a pattern (e.g. '^MPS:.*$', or '.*' to deliberately allow all).",
                details={"allow_pv_write": True, "pv_write_pattern": ""},
            )
        # Fail-closed (E8): writes ENABLED while the EPICS client search env can reach beyond
        # loopback means a mis-scoped allowlist could hit a production IOC. The name-pattern
        # gate above scopes WHAT may be written; this gate scopes WHERE a write can physically
        # go, both must hold. The check is parser-faithful (an *_AUTO_ADDR_LIST spelling the
        # real client rejects keeps broadcasting) and resolution-free (a hostname is never
        # trusted as loopback). ``environ`` is injectable for determinism; the singleton path
        # reads the process env, which is exactly what p4p/libca will read. Server-side
        # EPICS_CAS_*/EPICS_PVAS_* beacon vars do not exist in this client process and are
        # deliberately not part of the check (see epics_address module docstring).
        if config.allow_pv_write:
            violations = write_reach_violations(os.environ if environ is None else environ)
            if violations:
                raise SafetyConfigError(
                    "PV writes are ENABLED (EPICS_MCP_ALLOW_PV_WRITE=true) but the EPICS client "
                    "search reach is not loopback-only, refusing to start a write-enabled "
                    "process that could reach a real facility network. Violations: "
                    + "; ".join(violations)
                    + ". Fix: set EPICS_PVA/CA_ADDR_LIST and *_NAME_SERVERS to loopback hosts "
                    "only, set EPICS_PVA_AUTO_ADDR_LIST=NO and EPICS_CA_AUTO_ADDR_LIST=NO, "
                    "or disable writes.",
                    details={"allow_pv_write": True, "reach_violations": violations},
                )
        # Sliding-window timestamps of recent writes. G2 constrains
        # write_rate_limit to ge=1, so a *validated* config never produces a
        # negative maxlen. This fail-closed guard catches a config that bypassed
        # validation (e.g. EpicsConfig.model_construct): a bare ValueError from
        # deque(maxlen<0) would otherwise escape the fail-closed contract here.
        try:
            self._timestamps: deque[float] = deque(maxlen=config.write_rate_limit)
        except ValueError as exc:
            raise SafetyConfigError(
                f"Invalid write_rate_limit {config.write_rate_limit!r}: must be >= 0",
                details={"write_rate_limit": config.write_rate_limit},
            ) from exc
        self._audit_handler: logging.Handler | None = None
        self._audit_logger = self._setup_audit_logger()
        # S28: the rate-limit token acquisition (purge -> len-check -> append) must be ATOMIC:
        # symmetric with OlogWriteGate. The PV write path runs the gate inline on the event loop
        # today (tools/write.py, before the first await), so it is not racy YET; the lock is
        # defensive symmetry that also holds if PV write ever moves to a thread (O5). Per-instance
        # lock; the module-level _safety_lock guards the singleton getter, a separate concern.
        self._rate_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_write_allowed(self, pv_name: str) -> None:
        """Raise if the write must not proceed.

        Raises:
            PVWriteDeniedError: env gate off or PV not in allowlist.
            RateLimitError: write rate limit exceeded.
        """
        # 1. Environment gate
        if not self._config.allow_pv_write:
            self._audit_deny(pv_name, "PV_WRITE_DENIED")
            raise PVWriteDeniedError(
                "PV writes are disabled. Set EPICS_MCP_ALLOW_PV_WRITE=true to enable.",
                details={"pv_name": pv_name},
            )

        # 2. Pattern allowlist
        if self._pattern is not None and not self._pattern.fullmatch(pv_name):
            self._audit_deny(pv_name, "PV_WRITE_DENIED")
            raise PVWriteDeniedError(
                f"PV '{pv_name}' does not match the write allowlist pattern "
                f"'{self._config.pv_write_pattern}'.",
                details={"pv_name": pv_name, "pattern": self._config.pv_write_pattern},
            )

        # 3. Rate limit (sliding window). S28: purge + len-check + append are ONE atomic step under
        # _rate_lock (symmetric with OlogWriteGate), so concurrent writes can never both pass the
        # check and exceed the limit. `now` is sampled inside the lock; the audit + raise for a rate
        # denial run OUTSIDE the lock (I/O; the deny path never appends a token, invariant holds).
        with self._rate_lock:
            now = time.monotonic()
            self._purge_old(now)
            over_limit = len(self._timestamps) >= self._config.write_rate_limit
            if not over_limit:
                self._timestamps.append(now)  # record this write (success path only)
        if over_limit:
            self._audit_deny(pv_name, "RATE_LIMIT_EXCEEDED")
            raise RateLimitError(
                f"Write rate limit exceeded ({self._config.write_rate_limit} "
                f"writes per {self._WINDOW_SECONDS:.0f}s). Try again later.",
                details={
                    "pv_name": pv_name,
                    "limit": self._config.write_rate_limit,
                    "window_seconds": self._WINDOW_SECONDS,
                },
            )

    def audit_write_attempt(
        self, pv_name: str, new_value: object, operation_id: str, caller: str = "set_pv_value"
    ) -> None:
        """Log a write ATTEMPT (``event=ATTEMPT``) emitted BEFORE the I/O.

        Durable evidence that a write was dispatched, so a put that lands at the IOC after a
        mid-flight cancellation (see :meth:`audit_write_unknown`) is never wholly un-recorded. The
        ``operation_id`` correlates this line with the terminal ALLOW/FAILED/UNKNOWN_PENDING record.
        """
        self._emit(
            "PV_WRITE event=ATTEMPT pv=%s new=%r op=%s caller=%s",
            pv_name,
            new_value,
            operation_id,
            caller,
        )

    def audit_write(
        self,
        pv_name: str,
        old_value: object,
        new_value: object,
        operation_id: str = "-",
        caller: str = "set_pv_value",
    ) -> None:
        """Log a completed (ALLOW) write for audit purposes.

        ``operation_id`` ties this terminal record to the ``event=ATTEMPT`` line emitted before the
        I/O (:meth:`audit_write_attempt`); ``"-"`` means uncorrelated (a direct, non-tool call).
        """
        self._emit(
            "PV_WRITE event=ALLOW pv=%s old=%r new=%r op=%s caller=%s",
            pv_name,
            old_value,
            new_value,
            operation_id,
            caller,
        )

    def audit_write_failed(
        self,
        pv_name: str,
        old_value: object,
        new_value: object,
        error_code: str,
        operation_id: str = "-",
        caller: str = "set_pv_value",
    ) -> None:
        """Log a write that passed the safety gate but FAILED during ``pv_put``.

        The README promises *every* write is logged; this closes the gap where a
        failed put (or any non-:class:`EpicsError` raised below the tool layer)
        left no forensic trace. ``operation_id`` ties it to the ``event=ATTEMPT`` line.
        """
        self._emit(
            "PV_WRITE event=FAILED pv=%s old=%r new=%r error_code=%s op=%s caller=%s",
            pv_name,
            old_value,
            new_value,
            error_code,
            operation_id,
            caller,
        )

    def audit_write_unknown(
        self,
        pv_name: str,
        old_value: object,
        new_value: object,
        operation_id: str,
        caller: str = "set_pv_value",
    ) -> None:
        """Log a write whose outcome is UNKNOWN (``event=UNKNOWN_PENDING``).

        The write coroutine was cancelled mid-``pv_put``; the ``asyncio.to_thread`` worker running
        the p4p put is NOT stopped by that cancellation, so the value may still reach the IOC. This
        is never a FAILED write (which would imply nothing was written) and must never be blindly
        retried (which could double-write), the operator verifies by read-back.
        """
        self._emit(
            "PV_WRITE event=UNKNOWN_PENDING pv=%s old=%r new=%r op=%s caller=%s",
            pv_name,
            old_value,
            new_value,
            operation_id,
            caller,
        )

    def audit_readback(
        self,
        pv_name: str,
        written: object,
        readback_value: object,
        verified: bool | None,
        operation_id: str = "-",
        caller: str = "set_pv_value",
    ) -> None:
        """Log the O3 readback verdict of a completed write (``event=READBACK_*``).

        Emitted AFTER the ALLOW record (:meth:`audit_write`): the write already succeeded, and this
        is the independent verdict on whether the value read back matches what was written.

        * ``READBACK_OK``: the readback is within tolerance of the written value.
        * ``READBACK_MISMATCH``: a genuine mismatch. The loud, forensic signal for a wrong write;
          the write is NOT reverted (a wrong value may now sit at the IOC).
        * ``READBACK_UNVERIFIED``: the readback could not be obtained (timeout / value withheld).
          An absence of evidence, never a mismatch.

        ``operation_id`` ties this line to the ATTEMPT/ALLOW records of the same write.
        """
        if verified is True:
            event = "READBACK_OK"
        elif verified is False:
            event = "READBACK_MISMATCH"
        else:
            event = "READBACK_UNVERIFIED"
        self._emit(
            "PV_WRITE event=%s pv=%s written=%r readback=%r op=%s caller=%s",
            event,
            pv_name,
            written,
            readback_value,
            operation_id,
            caller,
        )

    def audit_bounds_deny(
        self,
        pv_name: str,
        value: object,
        limit_low: object,
        limit_high: object,
        caller: str = "set_pv_value",
    ) -> None:
        """Log a write REFUSED because its value is outside the record's drive limits (O2).

        ``event=BOUNDS_DENY``: the PV passed the name/rate gate but the written value lies outside
        ``[limit_low, limit_high]`` (control_t DRVL/DRVH), so the put is refused BEFORE the I/O and
        nothing reaches the IOC. Deliberately NOT reusing :meth:`_audit_deny`: this refusal happens
        AFTER the pre-read, so it consumed its rate token (like a FAILED put), unlike the gate
        denies, whose docstring pins that a denial never consumes a token. Values/limits are numeric
        metadata, never free text.
        """
        self._emit(
            "PV_WRITE event=BOUNDS_DENY pv=%s value=%r limit_low=%r limit_high=%r caller=%s",
            pv_name,
            value,
            limit_low,
            limit_high,
            caller,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _audit_deny(self, pv_name: str, error_code: str, caller: str = "set_pv_value") -> None:
        """Log a REJECTED write (gate off / pattern mismatch / rate limit).

        Called *before* the ``raise`` in :meth:`check_write_allowed`, i.e. before
        the rate-limit token is appended, so a denial never consumes a token.
        """
        self._emit(
            "PV_WRITE event=DENY pv=%s error_code=%s caller=%s",
            pv_name,
            error_code,
            caller,
        )

    def _emit(self, message: str, *args: object) -> None:
        """Single audit sink. Total function: the stdlib ``logging`` layer absorbs
        handler I/O/formatting errors via ``Handler.handleError``, so an audit
        emission never turns a denial/failure into a crash nor hides the original
        raise, hence no ``try/except`` guard is needed here.
        """
        self._audit_logger.info(message, *args)

    def _purge_old(self, now: float) -> None:
        """Remove timestamps older than the sliding window."""
        cutoff = now - self._WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _setup_audit_logger(self) -> logging.Logger:
        """Create a dedicated logger for audit records.

        The audit sink is VALIDATED on every construction, not only the first, so a broken
        audit path fails closed even when an earlier SafetyLayer already attached a handler to
        the process-global ``epics_mcp.audit`` logger (QA 2026-07-17: gating the whole block
        on ``if not audit.handlers`` used to skip the path check on repeat construction). At most
        ONE handler is attached; a duplicate built only to validate the path is discarded.
        """
        audit = logging.getLogger("epics_mcp.audit")
        audit.setLevel(logging.INFO)
        handler: logging.Handler
        if self._config.audit_log_file:
            # Fail-closed: a broken or unwritable audit path must not crash as a raw
            # FileNotFoundError at the first write. It fails here as a SafetyConfigError,
            # symmetric to the regex validation in __init__, and on EVERY construction.
            try:
                # encoding="utf-8": without it FileHandler takes the platform locale
                # (Windows cp1252), and one micro sign, ohm sign or accented letter in an audit
                # line (real EPICS units, non-ASCII names) raises a UnicodeEncodeError that the
                # stdlib ``Handler.handleError`` swallows SILENTLY (see the _emit docstring): the
                # line disappears without trace. UTF-8 fixes the encoding across platforms.
                handler = logging.FileHandler(self._config.audit_log_file, encoding="utf-8")
            # ValueError and TypeError alongside OSError: the promise above is that a broken path
            # fails HERE as a SafetyConfigError, and those two escaped it. A NUL byte in the path
            # raises ValueError out of the builtin open, a non-str raises TypeError out of
            # os.fspath, and neither is an OSError, so the process died on a bare traceback instead
            # of the named refusal (measured on both gates). Symmetric with OlogWriteGate.
            except (OSError, ValueError, TypeError) as exc:
                raise SafetyConfigError(
                    f"Invalid EPICS_MCP_AUDIT_LOG_FILE {self._config.audit_log_file!r}: {exc}",
                    details={"audit_log_file": self._config.audit_log_file},
                ) from exc
        else:
            handler = logging.StreamHandler(sys.stderr)
        # UTC timestamps: the formatter stays on framework time (no datetime.now() in the
        # logic); only the converter is switched to gmtime. The literal 'Z' in datefmt marks
        # UTC, because %z would be empty under gmtime, so it is written out instead.
        formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        # Attach at most one handler (dedup on repeated init). The handler above was built
        # unconditionally, so its path validation already ran; if the logger is already
        # configured, close the extra one (frees the duplicate file descriptor, a
        # StreamHandler over sys.stderr does not own the stream, so close() won't close stderr).
        if not audit.handlers:
            audit.addHandler(handler)
            self._audit_handler = handler
        else:
            handler.close()
        return audit
