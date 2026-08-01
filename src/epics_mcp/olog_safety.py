"""Write gate for Phoebus Olog logbook posts, env gate, test-server URL boundary, logbook
allowlist, rate-limit, privacy-clean audit.

A SEPARATE gate from :class:`~epics_mcp.safety.SafetyLayer` (the PV write gate): Olog write is a
deliberately-authorized, separate logbook surface, so ``EPICS_MCP_ALLOW_PV_WRITE`` stays false and
untouched. This is a schwester class rather than a generalisation of ``SafetyLayer`` so the tested
PV write path is never touched: three things diverge deliberately:

* **Test-server URL boundary** (the one new building block vs. PV). PV write is implicitly test-safe
  through the EPICS address-list localhost isolation; Olog speaks HTTP to an arbitrary URL, where
  that isolation does NOT apply. So a write is refused unless ``olog_url`` resolves to a loopback
  host (the local Docker sandbox) OR is an allowlisted https URL with remote writes enabled (a
  plain-http remote is refused, Basic creds are cleartext), a production write is a deliberate,
  auditable double action. The host comes ONLY from :func:`~epics_mcp.services._http.url_host`,
  which parses with **urllib3, the parser ``requests`` actually connects through**, never with a
  second parser and never a substring of the authority: ``http://127.0.0.1@olog-prod/Olog`` has host
  ``olog-prod`` and is refused. Naming ``urlparse`` here would be wrong AND dangerous: the two
  parsers disagree on a hostile authority (see ``url_host``'s docstring for the measured case), and
  a boundary validated with a different parser than the one that opens the socket is a bypass.
* **Deny-all empty allowlist.** Gate on + empty logbook allowlist = deny every write: a wrong
  logbook is a visible error. BOTH write gates are fail-closed on empty, in DIFFERENT shapes:
  the PV name-pattern (``SafetyLayer``) *refuses to start* when writes are on and the pattern is
  empty (``SafetyConfigError``; only an explicit ``.*`` deliberately allows all), while this gate
  constructs and denies at runtime. NOT an inverse (neither is fail-open), the shape is a
  deliberate per-surface choice; never describe either gate as "allow all on empty".
* **Metadata-only audit.** The PV audit logs old/new VALUES; the Olog audit must NEVER log the
  ``title``/``description`` free text: an audit file is a SEPARATE and longer-lived channel than
  the answer handed to one caller, and a person can be named in either field. Only logbook names,
  level, title LENGTH, entry id, and the service-account owner are recorded.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque

from epics_mcp.config import EpicsConfig, get_config
from epics_mcp.errors import OlogWriteDeniedError, RateLimitError, SafetyConfigError
from epics_mcp.services._http import is_https_url, is_loopback_url, url_host

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

    0. Non-empty logbooks: an empty set slips through the ``⊆`` allowlist check, so guard first.
    1. Environment gate: ``allow_olog_write`` must be True.
    2. Test-server URL boundary: loopback ``olog_url``, else an allowlisted remote https URL.
    3. Logbook allowlist: every target logbook ∈ ``olog_write_logbooks`` (empty = deny-all).
    3b. Attachment size cap: ``attachment_bytes`` ≤ ``olog_attach_max_bytes`` (OA1 anti-DoS; a
        no-op at the default ``attachment_bytes=0``). BEFORE the rate limit so it never burns a
        token.
    4. Rate limit: at most ``olog_write_rate_limit`` writes per 60 s window.

    Every rejection is audited as DENY *before* the raise, i.e. before the rate token is appended,
    so a denial never consumes a token.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, config: EpicsConfig) -> None:
        self._config = config
        self._allowed_logbooks = self._split_csv(config.olog_write_logbooks)
        self._allowed_urls = self._split_csv(config.olog_write_url_allowlist)
        # Fail-closed: a config bypassing validation (EpicsConfig.model_construct) must not let a
        # bare ValueError from deque(maxlen<0) escape the fail-closed contract, mirror SafetyLayer.
        try:
            self._timestamps: deque[float] = deque(maxlen=config.olog_write_rate_limit)
        except ValueError as exc:
            raise SafetyConfigError(
                f"Invalid olog_write_rate_limit {config.olog_write_rate_limit!r}: must be >= 0",
                details={"olog_write_rate_limit": config.olog_write_rate_limit},
            ) from exc
        self._audit_handler: logging.Handler | None = None
        self._audit_logger = self._setup_audit_logger()
        # S28: the rate-limit token acquisition (purge -> len-check -> append) must be ATOMIC. This
        # gate runs under asyncio.to_thread (checkers.query_olog_create), so two concurrent writes
        # execute in DIFFERENT worker threads; without a lock both could pass the len-check before
        # either appends and exceed the limit (measured: limit 1 -> 2 admitted). Per-instance lock;
        # the module-level _olog_safety_lock guards the singleton getter, a separate concern.
        self._rate_lock = threading.Lock()
        # Defense-in-depth: writes ENABLED with an EMPTY allowlist is deny-all (fail-closed), so
        # warn loudly, an operator who set ALLOW_OLOG_WRITE but forgot the allowlist gets no write.
        if config.allow_olog_write and not self._allowed_logbooks:
            logging.getLogger(__name__).warning(
                "Olog writes are ENABLED but EPICS_MCP_OLOG_WRITE_LOGBOOKS is empty, every write "
                "is denied (deny-all). Set the allowed logbook names to enable writes."
            )

    @staticmethod
    def _split_csv(value: str) -> frozenset[str]:
        """A frozenset of the comma-separated, stripped, non-empty tokens of *value*."""
        return frozenset(token.strip() for token in value.split(",") if token.strip())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_write_env_and_url(
        self, caller: str = "create_log_entry", logbooks: list[str] | None = None
    ) -> None:
        """The two write checks that need NO logbook knowledge: env gate + test-server URL boundary.

        Split out for the round-tripping callers (``add_log_attachment`` / ``update_log_entry``):
        their logbook allowlist is keyed on the TARGET entry's own logbooks, which requires a read
        first, but a caller the gate would refuse on the env or URL axis must be refused BEFORE
        that read (no HTTP round-trip, no entry-existence oracle, for a write the gate rejects).
        :meth:`check_write_preconditions` calls this too, so the checks and their audit lines stay
        the same in every path. Each failing check audits DENY before the raise.

        ``logbooks`` is what the caller ALREADY knows, and it exists so that splitting this method
        out did not silently reshape a denial the split was never about: the create path passes its
        target list, so its env denial keeps carrying ``{"logbooks": ...}`` in ``details`` exactly
        as before. The round-tripping callers pass nothing and get ``{"caller": ...}``, because at
        this point the target's logbooks are precisely what has not been read yet.
        """
        # 1. Environment gate
        if not self._config.allow_olog_write:
            env_details: dict[str, object] = (
                {"logbooks": logbooks} if logbooks is not None else {"caller": caller}
            )
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                "Olog writes are disabled. Set EPICS_MCP_ALLOW_OLOG_WRITE=true to enable "
                "(ALLOW_PV_WRITE is a separate gate and stays off).",
                details=env_details,
            )

        # 2. Test-server URL boundary (the critical check, prevents an accidental production write)
        if not self._url_write_allowed():
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                f"Olog write refused: target {self._config.olog_url!r} is not a permitted write "
                "target. Only a loopback host, or an https URL that is in "
                "EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST with EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true, "
                "may be written to (a plain-http remote is refused, Basic creds are cleartext).",
                details={"olog_url": self._config.olog_url},
            )

    def check_write_preconditions(
        self, logbooks: list[str], caller: str = "create_log_entry"
    ) -> None:
        """The CHEAP, deterministic write checks, non-empty logbooks, env gate, URL boundary,
        logbook allowlist, with NO rate token and NO filesystem work. Split out so an upload caller
        can run them BEFORE it stats attachment sizes: a denied write then touches no filesystem
        (restoring the "deny before any I/O" posture, a denied caller must not get a file-existence
        stat oracle). The size cap + rate limit stay in :meth:`check_write_allowed`, which calls
        this first. Each failing check audits DENY before the raise; none appends a rate token.
        """
        # 0. Non-empty logbooks, SEC-3: set() <= frozenset() is True, so an empty list would slip
        #    through the allowlist check, burn a rate token, and 400 at the server. Guard here.
        if not logbooks:
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                "Olog write refused: at least one target logbook is required.",
                details={"logbooks": logbooks},
            )

        # 1. + 2. Environment gate and test-server URL boundary (shared with the round-trip
        #    callers, see check_write_env_and_url). The logbooks ride along so the env denial
        #    keeps the details shape this path had before the two checks were split out.
        self.check_write_env_and_url(caller, logbooks)

        # 3. Logbook allowlist (empty allowlist = deny-all at runtime; the PV pattern is also
        #    fail-closed on empty but in a DIFFERENT shape, refuse-to-start, not allow-all;
        #    see this module's docstring / safety.py:61-67)
        if not set(logbooks) <= self._allowed_logbooks:
            self._audit_deny("OLOG_WRITE_DENIED", caller)
            raise OlogWriteDeniedError(
                f"Olog write refused: logbook(s) {sorted(set(logbooks) - self._allowed_logbooks)} "
                "are not in the write allowlist (EPICS_MCP_OLOG_WRITE_LOGBOOKS).",
                details={"logbooks": logbooks},
            )

    def check_write_allowed(
        self,
        logbooks: list[str],
        caller: str = "create_log_entry",
        attachment_bytes: int = 0,
    ) -> None:
        """Raise if an Olog write to *logbooks* must not proceed.

        *attachment_bytes* is the total size of any attachment upload (OA1); the caller sums it by
        ``stat`` before reading files, so an over-limit request is refused before any bytes are
        materialised. Default 0 → the size cap is a no-op for a plain (no-attachment) write.

        Raises:
            OlogWriteDeniedError: empty logbooks, gate off, URL not permitted, logbook not in the
                allowlist, or the attachment upload exceeds ``olog_attach_max_bytes``.
            RateLimitError: write rate limit exceeded.
        """
        # Checks 0-3 (the cheap, deterministic denials) run FIRST and touch no filesystem, so an
        # upload caller can run them BEFORE it stats attachment sizes (see
        # check_write_preconditions).
        self.check_write_preconditions(logbooks, caller)

        # 3b. Attachment size cap (OA1 anti-DoS), BEFORE the rate limit, so an over-limit upload
        #     never consumes a rate token, and matched by the caller reading file SIZES (stat)
        # before
        #     bytes, so a huge file is refused without being loaded. attachment_bytes defaults to 0
        #     (a no-attachment write), making this a no-op for every pre-OA1 caller.
        if attachment_bytes > self._config.olog_attach_max_bytes:
            self._audit_deny("OLOG_ATTACH_TOO_LARGE", caller)
            raise OlogWriteDeniedError(
                f"Olog write refused: attachment upload of {attachment_bytes} bytes exceeds the "
                f"limit of {self._config.olog_attach_max_bytes} bytes "
                "(EPICS_MCP_OLOG_ATTACH_MAX_BYTES).",
                details={
                    "attachment_bytes": attachment_bytes,
                    "olog_attach_max_bytes": self._config.olog_attach_max_bytes,
                },
            )

        # 4. Rate limit (sliding window): LAST, so a denial above never consumes a token.
        # S28: purge + len-check + append are ONE atomic step under _rate_lock, so two concurrent
        # writes (this gate runs under asyncio.to_thread = real threads) can never both pass the
        # check and exceed the limit. `now` is sampled inside the lock too. The audit + raise for a
        # rate denial run OUTSIDE the lock (I/O; never appends a token, the invariant holds).
        with self._rate_lock:
            now = time.monotonic()
            self._purge_old(now)
            over_limit = len(self._timestamps) >= self._config.olog_write_rate_limit
            if not over_limit:
                self._timestamps.append(now)  # record this write (success path only)
        if over_limit:
            self._audit_deny("RATE_LIMIT_EXCEEDED", caller)
            raise RateLimitError(
                f"Olog write rate limit exceeded ({self._config.olog_write_rate_limit} writes per "
                f"{self._WINDOW_SECONDS:.0f}s). Try again later.",
                details={
                    "limit": self._config.olog_write_rate_limit,
                    "window_seconds": self._WINDOW_SECONDS,
                },
            )

    def audit_write(
        self,
        entry_id: str,
        logbooks: list[str],
        level: str | None,
        title_len: int,
        owner: str,
        in_reply_to: str | None = None,
        caller: str = "create_log_entry",
        attachment_count: int = 0,
        attachment_bytes: int = 0,
    ) -> None:
        """Log a completed (ALLOW) Olog write. Metadata only, NEVER title/description free text.

        ``owner`` is the write service-account name from config (the Principal the SERVER records
        as owner, read from config, not parsed back out of the response). ``attachment_count``/
        ``attachment_bytes`` (OA1) are COUNTS/SIZES only, never a filename, which is author free
        text
        (a person can be named in it); they are appended only for an upload, so a plain write's
        audit
        line stays byte-identical.
        """
        self._emit(
            f"OLOG_WRITE event=ALLOW logbooks={self._join(logbooks)} level={self._lvl(level)} "
            f"title_len={title_len} entry_id={entry_id} owner={owner}"
            f"{self._reply_suffix(in_reply_to)}"
            f"{self._attach_suffix(attachment_count, attachment_bytes)} "
            f"caller={caller}"
        )

    def audit_write_failed(
        self,
        logbooks: list[str],
        level: str | None,
        title_len: int,
        error_code: str,
        in_reply_to: str | None = None,
        caller: str = "create_log_entry",
        entry_id: str | None = None,
    ) -> None:
        """Log a write that passed the gate but FAILED at the HTTP layer.

        SEC-5: still NO ``owner``; metadata only.

        ``entry_id`` is optional because a failed CREATE has none, but that is a statement about
        create, and it was wrongly generalised to every write. An EDIT (update_log_entry,
        add_log_attachment) targets an entry that already exists, and the server archives and
        mutates it BEFORE the response goes out: a timeout leaves an APPLIED write in front of a
        client that sees FAILED. Omitting the id there makes the one record of the attempt unable to
        say WHICH entry may now be altered. The id is a server-minted integer, never free text, so
        it carries no more than the ALLOW record already does.
        """
        entry_suffix = f" entry_id={entry_id}" if entry_id is not None else ""
        self._emit(
            f"OLOG_WRITE event=FAILED logbooks={self._join(logbooks)} level={self._lvl(level)} "
            f"title_len={title_len} error_code={error_code}{entry_suffix}"
            f"{self._reply_suffix(in_reply_to)} caller={caller}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url_write_allowed(self) -> bool:
        """True iff ``olog_url`` is a permitted write target (loopback, or allowlisted + remote).

        Three steps, in this order, the ORDER is load-bearing:

        1. **Unparseable → deny, before anything else** (SEC-2). ``url_host`` returns None for a
           hostless/garbage URL, for a scheme-less base URL, and for a MALFORMED authority (a bad
           bracketed IPv6 raises ``LocationParseError``/``ValueError`` in the urllib3 parser
           it uses, the same parser ``requests`` connects with). This veto runs FIRST, so an
           unparseable URL is denied even if it is exactly allowlisted; a bad URL is a clean,
           audited DENY, never an uncaught crash and never a lucky pass.
        2. **Loopback → allow** (the local Docker sandbox).
        3. **Anything else** (INCLUDING RFC1918 private, the production Olog lives on a private
           network, so "private = allowed" would defeat the prod NO-GO): permit only an EXACTLY
           allowlisted base URL with remote writes explicitly enabled AND an ``https`` scheme (a
           plain-http Basic-auth write to a real server would expose the credentials, see
           :func:`~epics_mcp.services._http.is_https_url`).

        The hardened host extraction lives in :func:`~epics_mcp.services._http.url_host`, the only
        parser this boundary trusts. What this method expresses is the WRITE policy and nothing
        else: it returns True for an allowlisted REMOTE host too (step 3), so it is never the
        answer to "is this a local test server", which is
        :func:`~epics_mcp.services._http.is_loopback_url`.
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

    @staticmethod
    def _attach_suffix(attachment_count: int, attachment_bytes: int) -> str:
        """Metadata-only audit fragment for an upload; empty for a no-attachment write (so its audit
        line is byte-identical to before). NEVER a filename, counts and total bytes only."""
        if attachment_count <= 0:
            return ""
        return f" attachments={attachment_count} attach_bytes={attachment_bytes}"

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
        """Create the dedicated Olog audit logger (its OWN name, distinct from the PV audit).

        The audit sink is VALIDATED on every construction, not only the first, mirroring
        SafetyLayer._setup_audit_logger (QA 2026-07-17): a broken audit path fails closed as
        SafetyConfigError even when an earlier gate already attached a handler to the process-global
        ``epics_mcp.olog_audit`` logger. Gating the whole block on ``if not audit.handlers`` used
        to skip the FileHandler path check on repeat construction. At most ONE handler is attached;
        a duplicate built only to validate the path is discarded.
        """
        audit = logging.getLogger("epics_mcp.olog_audit")
        audit.setLevel(logging.INFO)
        handler: logging.Handler
        if self._config.audit_log_file:
            # Fail-closed: a broken/unwritable audit path fails as SafetyConfigError at init, not as
            # a raw OSError at the first write. Built UNCONDITIONALLY so the path check runs on
            # every construction (now truly symmetric to SafetyLayer).
            try:
                # encoding="utf-8": without it FileHandler takes the platform locale
                # (Windows cp1252), and one micro sign, ohm sign or accented letter in an audit
                # line (for instance a logbook name with a non-ASCII letter) raises a
                # UnicodeEncodeError that the stdlib ``Handler.handleError`` swallows SILENTLY
                # (see the _emit docstring): the line disappears without trace. UTF-8 fixes the
                # encoding across platforms.
                handler = logging.FileHandler(self._config.audit_log_file, encoding="utf-8")
            except OSError as exc:
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
        # unconditionally, so its path validation already ran; if the logger is already configured,
        # close the extra one (a StreamHandler over sys.stderr does not own the stream).
        if not audit.handlers:
            audit.addHandler(handler)
            self._audit_handler = handler
        else:
            handler.close()
        return audit
