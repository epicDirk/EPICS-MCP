"""Exceptions for the ESS Naming Service client.

Vendored (slimmed) from pvValidator's ``pvValidatorUtils/exceptions.py`` — only the
two Naming-Service errors the cross-plane check needs, so this repo stays standalone
(no pvValidator/SWIG dependency). Source: ``D:/pvValidator/.../exceptions.py``.

Derives from the shared :mod:`epics_pv_mcp.services.rest_exceptions` roots like the other three
REST planes (so ``except RestClientError`` catches every plane, ``except NamingServiceError`` just
this one).
"""

from epics_pv_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class NamingServiceError(RestClientError):
    """Base error for the ESS Naming Service client."""


class NamingServiceConnectionError(NamingServiceError, RestConnectionError):
    """Failed to establish a connection to the Naming Service."""


class NamingServiceResponseError(NamingServiceError, RestResponseError):
    """Unexpected response (HTTP error / bad payload) from the Naming Service."""
