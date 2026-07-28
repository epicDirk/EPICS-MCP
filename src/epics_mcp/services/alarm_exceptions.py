"""Exceptions for the Phoebus Alarm Logger REST client (read-only).

A per-service base plus connection/response errors, deriving from the shared
:mod:`epics_mcp.services.rest_exceptions` roots (so ``except RestClientError`` catches every
plane, while ``except AlarmError`` still catches just this one).
"""

from epics_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class AlarmError(RestClientError):
    """Base error for the Phoebus Alarm Logger client."""


class AlarmConnectionError(AlarmError, RestConnectionError):
    """Failed to establish a connection to the Alarm Logger."""


class AlarmResponseError(AlarmError, RestResponseError):
    """Unexpected response (HTTP error / bad payload) from the Alarm Logger."""
