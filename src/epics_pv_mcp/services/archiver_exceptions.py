"""Exceptions for the EPICS Archiver Appliance REST client (read-only).

A per-service base plus connection/response errors, deriving from the shared
:mod:`epics_pv_mcp.services.rest_exceptions` roots (so ``except RestClientError`` catches every
plane, while ``except ArchiverError`` still catches just this one).
"""

from epics_pv_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class ArchiverError(RestClientError):
    """Base error for the Archiver Appliance client."""


class ArchiverConnectionError(ArchiverError, RestConnectionError):
    """Failed to establish a connection to the Archiver Appliance."""


class ArchiverResponseError(ArchiverError, RestResponseError):
    """Unexpected response (HTTP error / bad payload) from the Archiver Appliance."""
