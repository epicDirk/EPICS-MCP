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


class NamingServiceNotFound(NamingServiceResponseError):
    """The queried device name returned HTTP 404 — treated as the service's "not registered".

    Split out from the generic :class:`NamingServiceResponseError` so a **404** is distinguishable
    from every NON-404 response failure (5xx, bad JSON, auth/proxy after a successful reachability
    probe). ``validate_name`` maps a 404 to ``registered=False`` but lets the generic error
    PROPAGATE so the caller withholds instead of reporting a false ``name_typo`` (DS-2 / audit S5).

    RESIDUAL (honest): a 404 caused by a WRONG base path (e.g. the ``…/rest`` double-``/rest``
    mistake) is INDISTINGUISHABLE from a genuine "name not registered" 404, so it still yields
    ``registered=False``. That misconfiguration is prevented by correct URL config (the overlay
    script sets ``EPICS_MCP_NAMING_URL`` without a trailing ``/rest``; the URL-resolution test
    guards ``…/rest/deviceNames/``), NOT by this split.
    """
