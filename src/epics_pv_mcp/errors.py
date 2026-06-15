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


class RateLimitError(EpicsError):
    """Raised when write rate limit is exceeded."""

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
