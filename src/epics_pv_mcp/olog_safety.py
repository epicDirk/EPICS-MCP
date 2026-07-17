"""Write gate for Phoebus Olog logbook posts — env gate, test-server URL boundary, logbook
allowlist, rate-limit, privacy-clean audit.

A SEPARATE gate from :class:`~epics_pv_mcp.safety.SafetyLayer` (the PV write gate): Olog write is a
deliberately-authorized, separate logbook surface, so ``EPICS_MCP_ALLOW_PV_WRITE`` stays false and
untouched. This is a schwester class rather than a generalisation of ``SafetyLayer`` so the tested
PV write path is never touched — three things diverge deliberately:

* **Test-server URL boundary** (the one new building block vs. PV). PV write is implicitly test-safe
  through the EPICS address-list localhost isolation; Olog speaks HTTP to an arbitrary URL, where
  that isolation does NOT apply. So a write is refused unless ``olog_url`` resolves to a loopback
  host (the local Docker sandbox) OR is an allowlisted https URL with remote writes enabled (a
  plain-http remote is refused — Basic creds are cleartext) — a production write is a deliberate,
  auditable double action. The host is taken ONLY from
  ``urlparse(url).hostname`` (never ``.netloc`` / a substring): ``http://127.0.0.1@olog-prod/Olog``
  has hostname ``olog-prod`` and is refused.
* **Deny-all empty allowlist.** Gate on + empty logbook allowlist = deny every write (the INVERSE
  of the PV pattern, where an empty pattern allows all): a wrong logbook is a visible error.
* **Metadata-only audit.** The PV audit logs old/new VALUES; the Olog audit must NEVER log the
  ``title``/``description`` free text (that would route write around the READ redaction). Only
  logbook names, level, title LENGTH, entry id, and the service-account owner are recorded.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque

from epics_pv_mcp.config import EpicsConfig, get_config
from epics_pv_mcp.errors import OlogWriteDeniedError, RateLimitError, SafetyConfigError
from epics_pv_mcp.services._http import is_https_url, is_loopback_url, url_host

logger = logging.getLogger(__name__)

_olog_safety: OlogWriteGate | None = None
_olog_safety_lock = threading.Lock()


def get_olog_safety() -> OlogWriteGate:
    """Return the singleton OlogWriteGate instance (thread-safe)."""
    global _olog_safety
    with _olog_safety_lock:
        if _olog_safety is None:
            _olog_safety = OlogWriteGate(get_config())
    return _olog_safety


class OlogWriteGate:
    """Guards every Olog logbook write with five checks in fixed, fail-closed order.

    0. Non-empty logbooks  — an empty set slips through the ``⊆`` allowlist check, so guard first.
    1. Environment gate    — ``allow_olog_write`` must be True.
    2. Test-server URL boundary — loopback ``olog_url``, else an allowlisted remote https URL.
    3. Logbook allowlist   — every target logbook ∈ ``olog_write_logbooks`` (empty = deny-all).
    4. Rate limit          — at most ``olog_write_rate_limit`` writes per 60 s window.

    Every rejection is audited as DENY *before* the raise, i.e. before the rate token is appended,
    so a denial never consumes a token.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, config: EpicsConfig) -> None:
        self._config = config
        self._allowed_logbooks = self._split_csv(config.olog_write_logbooks)
        self._allowed_urls = self._split_csv(config.olog_write_url_allowlist)
        # Fail-closed: a config bypassing validation (EpicsConfig.model_construct) must not let a
        # bare ValueError from deque(maxlen<0) escape the fail-closed contract — mirror SafetyLayer.
        try:
            self._timestamps: deque[float] = deque(maxlen=config.olog_write_rate_limit)
        except ValueError as exc:
            raise SafetyConfigError(
                f"Invalid olog_write_rate_limit {config.olog_write_rate_limit!r}: must be >= 0",
                details={"olog_write_rate_limit": config.olog_write_rate_limit},
            ) from exc
        self._audit_handler: logging.Handler | None = None
        self._audit_logger = self._setup_audit_logger()
        # Defense-in-depth: writes ENABLED with an EMPTY allowlist is deny-all (fail-closed), so
        # warn loudly — an operator who set ALLOW_OLOG_WRITE but forgot the allowlist gets no write.
        if config.allow_olog_write and not self._allowed_logbooks:
            logging.getLogger(__name__).warning(
                "Olog writes are ENABLED but EPICS_MCP_OLOG_WRITE_LOGBOOKS is empty — every write "
                "is denied (deny-all). Set the allowed logbook names to enable writes."
            )

    @staticmethod
    def _split_csv(value: str) -> frozenset[str]:
        """A frozenset of the comma-separated, stripped, non-empty tokens of *value*."""
        return frozenset(token.strip() for token in value.split(",") if token.strip())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_write_allowed(self, logbooks: list[str], caller: str = "create_log_entry") -> None:
        """Raise if an Olog write to *logbooks* must not proceed.

        Raises:
            OlogWriteDeniedError: empty logbooks, gate off, URL not permitted, or logbook not in the
                allowlist.
            RateLimitError: write rate limit exceeded.
        """
        # 0. Non-empty logbooks — SEC-3: set() <= frozenset() is True, so an empty list would slip
        #    through the allowlist check, burn a rate token, and 400 at the server. Guard here.
        if not logbooks:
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                "Olog write refused: at least one target logbook is required.",
                details={"logbooks": logbooks},
            )

        # 1. Environment gate
        if not self._config.allow_olog_write:
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                "Olog writes are disabled. Set EPICS_MCP_ALLOW_OLOG_WRITE=true to enable "
                "(ALLOW_PV_WRITE is a separate gate and stays off).",
                details={"logbooks": logbooks},
            )

        # 2. Test-server URL boundary (the critical check — prevents an accidental production write)
        if not self._url_write_allowed():
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                f"Olog write refused: target {self._config.olog_url!r} is not a permitted write "
                "target. Only a loopback host, or an https URL that is in "
                "EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST with EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true, "
                "may be written to (a plain-http remote is refused — Basic creds are cleartext).",
                details={"olog_url": self._config.olog_url},
            )

        # 3. Logbook allowlist (empty allowlist = deny-all — the INVERSE of the PV pattern)
        if not set(logbooks) <= self._allowed_logbooks:
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                f"Olog write refused: logbook(s) {sorted(set(logbooks) - self._allowed_logbooks)} "
                "are not in the write allowlist (EPICS_MCP_OLOG_WRITE_LOGBOOKS).",
                details={"logbooks": logbooks},
            )

        # 4. Rate limit (sliding window) — LAST, so a denial above never consumes a token
        now = time.monotonic()
        self._purge_old(now)
        if len(self._timestamps) >= self._config.olog_write_rate_limit:
            self._audit_deny("RATE_LIMIT_EXCEEDED", caller)
            raise RateLimitError(
                f"Olog write rate limit exceeded ({self._config.olog_write_rate_limit} writes per "
                f"{self._WINDOW_SECONDS:.0f}s). Try again later.",
                details={
                    "limit": self._config.olog_write_rate_limit,
                    "window_seconds": self._WINDOW_SECONDS,
                },
            )

        # Record this write timestamp (success path only)
        self._timestamps.append(now)

    def audit_write(
        self,
        entry_id: str,
        logbooks: list[str],
        level: str | None,
        title_len: int,
        owner: str,
        in_reply_to: str | None = None,
        caller: str = "create_log_entry",
    ) -> None:
        """Log a completed (ALLOW) Olog write. Metadata only — NEVER title/description free text.

        ``owner`` is the write service-account name from config (the Principal the SERVER records
        as owner, not the redacted response, which drops owner).
        """
        self._emit(
            f"OLOG_WRITE event=ALLOW logbooks={self._join(logbooks)} level={self._lvl(level)} "
            f"title_len={title_len} entry_id={entry_id} owner={owner}"
            f"{self._reply_suffix(in_reply_to)} caller={caller}"
        )

    def audit_write_failed(
        self,
        logbooks: list[str],
        level: str | None,
        title_len: int,
        error_code: str,
        in_reply_to: str | None = None,
        caller: str = "create_log_entry",
    ) -> None:
        """Log a write that passed the gate but FAILED at the HTTP layer.

        SEC-5: NO ``entry_id`` (none exists for a failed create) and NO ``owner``; metadata only.
        """
        self._emit(
            f"OLOG_WRITE event=FAILED logbooks={self._join(logbooks)} level={self._lvl(level)} "
            f"title_len={title_len} error_code={error_code}"
            f"{self._reply_suffix(in_reply_to)} caller={caller}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url_write_allowed(self) -> bool:
        """True iff ``olog_url`` is a permitted write target (loopback, or allowlisted + remote).

        Three steps, in this order — the ORDER is load-bearing:

        1. **Unparseable → deny, before anything else** (SEC-2). ``url_host`` returns None for a
           hostless/garbage URL and for a MALFORMED bracketed-IPv6 authority (``http://[::1]./Olog``
           makes ``urlparse`` raise ``ValueError`` on Python 3.12+). This veto runs FIRST, so an
           unparseable URL is denied even if it is exactly allowlisted — a bad URL is a clean,
           audited DENY, never an uncaught crash and never a lucky pass.
        2. **Loopback → allow** (the local Docker sandbox).
        3. **Anything else** (INCLUDING RFC1918 private — the production Olog lives on a private
           network, so "private = allowed" would defeat the prod NO-GO): permit only an EXACTLY
           allowlisted base URL with remote writes explicitly enabled AND an ``https`` scheme (a
           plain-http Basic-auth write to a real server would expose the credentials — see
           :func:`~epics_pv_mcp.services._http.is_https_url`).

        The hardened host extraction lives in :func:`~epics_pv_mcp.services._http.url_host` and is
        shared with the Olog READ redaction — the PRIMITIVE is shared, this POLICY is not. Note the
        read side must NOT reuse this method: it returns True for an allowlisted REMOTE host too
        (step 3), which as a read predicate would surface a production logbook un-redacted.
        """
        url = self._config.olog_url
        if url_host(url) is None:  # SEC-2: unparseable → fail closed, allowlist cannot override
            return False
        if is_loopback_url(url):
            return True
        # A REMOTE (non-loopback) target must ALSO be https: a plain-http Basic-auth write to a real
        # server would expose the service-account credentials on the wire (and to any inherited
        # proxy). Loopback stayed http-OK above (the sandbox); only the remote lane is tightened.
        return (
            self._config.olog_write_allow_remote and url in self._allowed_urls and is_https_url(url)
        )

    def _audit_deny(self, error_code: str, caller: str) -> None:
        """Log a REJECTED write (empty logbooks / gate off / URL / allowlist / rate limit).

        Called *before* the ``raise`` in :meth:`check_write_allowed`, i.e. before the rate-limit
        token is appended, so a denial never consumes a token. No payload (SEC): code + caller only.
        """
        self._emit(f"OLOG_WRITE event=DENY error_code={error_code} caller={caller}")

    @staticmethod
    def _join(logbooks: list[str]) -> str:
        return ",".join(logbooks)

    @staticmethod
    def _lvl(level: str | None) -> str:
        return level if level else "(default)"

    @staticmethod
    def _reply_suffix(in_reply_to: str | None) -> str:
        return f" in_reply_to={in_reply_to}" if in_reply_to is not None else ""

    def _emit(self, message: str) -> None:
        """Single audit sink. The message is pre-formatted from discrete metadata only (no free
        text), and passed with NO logging args so a literal ``%`` in a logbook name is never treated
        as a format directive. The stdlib logging layer absorbs handler errors via
        ``Handler.handleError``, so an audit emission never turns a denial/failure into a crash."""
        self._audit_logger.info(message)

    def _purge_old(self, now: float) -> None:
        """Remove timestamps older than the sliding window."""
        cutoff = now - self._WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _setup_audit_logger(self) -> logging.Logger:
        """Create the dedicated Olog audit logger (its OWN name, distinct from the PV audit)."""
        audit = logging.getLogger("epics_pv_mcp.olog_audit")
        audit.setLevel(logging.INFO)
        if not audit.handlers:
            handler: logging.Handler
            if self._config.audit_log_file:
                # Fail-closed: a broken/unwritable audit path fails as SafetyConfigError at init,
                # symmetric to SafetyLayer, not as a raw OSError at the first write.
                try:
                    handler = logging.FileHandler(self._config.audit_log_file)
                except OSError as exc:
                    raise SafetyConfigError(
                        f"Invalid EPICS_MCP_AUDIT_LOG_FILE {self._config.audit_log_file!r}: {exc}",
                        details={"audit_log_file": self._config.audit_log_file},
                    ) from exc
            else:
                handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
            )
            audit.addHandler(handler)
            self._audit_handler = handler
        return audit
