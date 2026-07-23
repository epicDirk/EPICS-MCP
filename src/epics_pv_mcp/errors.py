"""Machine-readable error hierarchy for the EPICS PV MCP Server."""


class EpicsError(Exception):
    """Base error with machine-readable error_code."""

    def __init__(
        self,
        message: str,
        error_code: str = "UNKNOWN",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class PVNotFoundError(EpicsError):
    """Raised when a PV cannot be found on the network."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="PV_NOT_FOUND", details=details)


class PVTimeoutError(EpicsError):
    """Raised when a PV operation exceeds the configured timeout."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="PV_TIMEOUT", details=details)


class PVWriteDeniedError(EpicsError):
    """Raised when a PV write is rejected by the safety layer."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="PV_WRITE_DENIED", details=details)


class PVWriteBoundsError(PVWriteDeniedError):
    """Raised when a PV write value is outside the record's own drive limits (control_t DRVL/DRVH).

    A distinct denial reason from the name/rate gate (:class:`PVWriteDeniedError`): the PV IS in the
    write allowlist, but the VALUE is out of range (O2). Subclasses ``PVWriteDeniedError`` so an
    existing ``except PVWriteDeniedError`` still catches it, with a distinct ``error_code`` so a
    caller can tell "fix the value" from "PV not allowlisted". Calls ``EpicsError.__init__``
    directly because ``PVWriteDeniedError.__init__`` hardcodes ``error_code="PV_WRITE_DENIED"``.
    """

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        EpicsError.__init__(self, message, error_code="PV_WRITE_OUT_OF_BOUNDS", details=details)


class OlogWriteDeniedError(EpicsError):
    """Raised when an Olog logbook write is rejected by the Olog write gate.

    A separate gate from the PV write gate (``PVWriteDeniedError``): Olog write has its
    own env gate (``EPICS_MCP_ALLOW_OLOG_WRITE``), test-server URL boundary and logbook
    allowlist. ``ALLOW_PV_WRITE`` is untouched by this.
    """

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="OLOG_WRITE_DENIED", details=details)


class OlogWholeModeRequiredError(OlogWriteDeniedError):
    """Raised when an Olog write tool refuses a redacted/remote server BEFORE the gate is consulted.

    ``add_log_attachment`` and ``update_log_entry`` both go through the server's destructive
    ``updateLog``, so they must round-trip the target entry's FULL content — readable only in
    whole-mode (loopback URL + ``EPICS_MCP_OLOG_ASSUME_TEST_DATA``). The service layer checks that
    posture at the very top of the call, *before* ``get_olog_safety()`` is reached.

    Why its own class (write-gate contract point 4, "a refusal raised outside the gate must not
    carry the gate's error code"): this refusal writes **no audit line at all** — it never reaches
    the gate, so there is no gate verdict to record. Reusing ``OLOG_WRITE_DENIED`` would make an
    un-audited pre-gate refusal indistinguishable from a real, audited gate DENY, and the audit's
    coverage claim unfalsifiable from the outside. Deliberately stays a **subclass** of
    :class:`OlogWriteDeniedError` so every existing ``except OlogWriteDeniedError`` keeps catching
    it, and calls ``EpicsError.__init__`` directly because ``OlogWriteDeniedError.__init__``
    hardcodes ``error_code="OLOG_WRITE_DENIED"`` — the same shape as :class:`PVWriteBoundsError`.

    Client-side twin: :class:`~epics_pv_mcp.services.olog_exceptions.OlogWholeModeRequired`, the
    defense-in-depth backstop inside ``OlogClient.get_raw_entry``. It carries the SAME code, via the
    ClassVar convention that module uses, so a caller sees one code for one condition.
    """

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        EpicsError.__init__(self, message, error_code="OLOG_WHOLE_MODE_REQUIRED", details=details)


class RateLimitError(EpicsError):
    """Raised when a rate limit is exceeded — a write gate (PV / Olog); see also the read throttle's
    own :class:`ReadRateLimitError`."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="RATE_LIMIT_EXCEEDED", details=details)


class ReadRateLimitError(RateLimitError):
    """Raised when the shared REST GET read throttle (S3) refuses a READ.

    A different event from a write gate's rate-limit denial, which is why it carries a different
    code: the read throttle sits in ``services/_http`` on the read chokepoint, it is consulted for
    reads only, and — like every pre-gate refusal — it writes **no audit line**. Write-gate contract
    point 4 forbids a refusal raised outside a gate from carrying the gate's error code, so
    ``RATE_LIMIT_EXCEEDED`` (the audited code both write gates emit) is reserved for them.

    The distinction is not cosmetic: the four Olog write tools reach the throttle on the reads they
    perform before their gate is consulted, so without a separate code a throttled read would be
    reported to the caller exactly like an audited write-rate DENY it never was.

    Deliberately a **subclass** of :class:`RateLimitError` so existing ``except RateLimitError``
    handlers keep working, and it calls ``EpicsError.__init__`` directly because
    ``RateLimitError.__init__`` hardcodes ``error_code="RATE_LIMIT_EXCEEDED"`` — the same shape as
    :class:`PVWriteBoundsError`.
    """

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        EpicsError.__init__(self, message, error_code="READ_RATE_LIMIT_EXCEEDED", details=details)


class EpicsConnectionError(EpicsError):
    """Raised when connection to EPICS infrastructure fails."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="EPICS_CONNECTION_FAILED", details=details)


class SafetyConfigError(EpicsError):
    """Raised when the safety configuration is invalid (e.g. a malformed
    ``pv_write_pattern`` regex).

    Fail-closed: the server refuses to start with a broken write-allowlist
    rather than silently disabling it (which would be fail-open).
    """

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="SAFETY_CONFIG_INVALID", details=details)
