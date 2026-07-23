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


class RateLimitError(EpicsError):
    """Raised when a rate limit is exceeded — a write gate (PV / Olog) or the S3 read throttle."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message, error_code="RATE_LIMIT_EXCEEDED", details=details)


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
