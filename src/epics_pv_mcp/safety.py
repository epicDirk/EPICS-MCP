"""Safety layer for PV write operations — gate, allowlist, rate-limit, audit."""

import logging
import re
import sys
import threading
import time
from collections import deque

from epics_pv_mcp.config import EpicsConfig, get_config
from epics_pv_mcp.errors import PVWriteDeniedError, RateLimitError, SafetyConfigError

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

    1. Environment gate  — ``allow_pv_write`` must be True.
    2. Pattern allowlist  — PV name must match ``pv_write_pattern`` regex.
    3. Rate limit         — at most ``write_rate_limit`` writes per 60 s window.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, config: EpicsConfig) -> None:
        self._config = config
        # Fail-closed: ein kaputtes Allowlist-Pattern darf die Schreib-Sperre
        # NICHT still aushebeln — lieber klar scheitern als ungeschützt schreiben.
        try:
            self._pattern: re.Pattern[str] | None = (
                re.compile(config.pv_write_pattern) if config.pv_write_pattern else None
            )
        except re.error as exc:
            raise SafetyConfigError(
                f"Invalid EPICS_MCP_PV_WRITE_PATTERN regex {config.pv_write_pattern!r}: {exc}",
                details={"pattern": config.pv_write_pattern},
            ) from exc
        # Sliding-window timestamps of recent writes
        self._timestamps: deque[float] = deque(maxlen=config.write_rate_limit)
        self._audit_handler: logging.Handler | None = None
        self._audit_logger = self._setup_audit_logger()

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
            raise PVWriteDeniedError(
                "PV writes are disabled. Set EPICS_MCP_ALLOW_PV_WRITE=true to enable.",
                details={"pv_name": pv_name},
            )

        # 2. Pattern allowlist
        if self._pattern is not None and not self._pattern.fullmatch(pv_name):
            raise PVWriteDeniedError(
                f"PV '{pv_name}' does not match the write allowlist pattern "
                f"'{self._config.pv_write_pattern}'.",
                details={"pv_name": pv_name, "pattern": self._config.pv_write_pattern},
            )

        # 3. Rate limit (sliding window)
        now = time.monotonic()
        self._purge_old(now)
        if len(self._timestamps) >= self._config.write_rate_limit:
            raise RateLimitError(
                f"Write rate limit exceeded ({self._config.write_rate_limit} "
                f"writes per {self._WINDOW_SECONDS:.0f}s). Try again later.",
                details={
                    "pv_name": pv_name,
                    "limit": self._config.write_rate_limit,
                    "window_seconds": self._WINDOW_SECONDS,
                },
            )

        # Record this write timestamp
        self._timestamps.append(now)

    def audit_write(self, pv_name: str, old_value: object, new_value: object) -> None:
        """Log a completed write for audit purposes."""
        self._audit_logger.info(
            "PV_WRITE pv=%s old=%r new=%r",
            pv_name,
            old_value,
            new_value,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _purge_old(self, now: float) -> None:
        """Remove timestamps older than the sliding window."""
        cutoff = now - self._WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _setup_audit_logger(self) -> logging.Logger:
        """Create a dedicated logger for audit records."""
        audit = logging.getLogger("epics_pv_mcp.audit")
        audit.setLevel(logging.INFO)
        # Avoid duplicate handlers on repeated init
        if not audit.handlers:
            if self._config.audit_log_file:
                handler: logging.Handler = logging.FileHandler(self._config.audit_log_file)
            else:
                handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
            )
            audit.addHandler(handler)
            self._audit_handler = handler
        return audit
