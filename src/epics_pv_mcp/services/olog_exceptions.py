"""Exceptions for the Phoebus Olog REST client (read-only).

A per-service base plus connection/response errors, deriving from the shared
:mod:`epics_pv_mcp.services.rest_exceptions` roots (so ``except RestClientError`` catches every
plane, while ``except OlogError`` still catches just this one). The 5th REST plane anticipated by
``services/_http`` and ``services/checkers``.
"""

from epics_pv_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class OlogError(RestClientError):
    """Base error for the Olog client."""


class OlogConnectionError(OlogError, RestConnectionError):
    """Failed to establish a connection to the Olog service."""


class OlogResponseError(OlogError, RestResponseError):
    """Unexpected response (HTTP error / bad payload) from the Olog service."""
