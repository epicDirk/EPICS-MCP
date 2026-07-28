"""Exceptions for the ESS Naming Service client.

Vendored (slimmed) from pvValidator's ``pvValidatorUtils/exceptions.py``, only the
two Naming-Service errors the cross-plane check needs, so this repo stays standalone
(no pvValidator/SWIG dependency). Source: ``D:/pvValidator/.../exceptions.py``.

Derives from the shared :mod:`epics_mcp.services.rest_exceptions` roots like the other three
REST planes (so ``except RestClientError`` catches every plane, ``except NamingServiceError`` just
this one).
"""

from epics_mcp.services.rest_exceptions import (
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
    """The queried device name returned HTTP 404, treated as the service's "not registered".

    Split out from the generic :class:`NamingServiceResponseError` so a **404** is distinguishable
    from every NON-404 response failure (5xx, bad JSON, auth/proxy after a successful reachability
    probe). ``validate_name`` maps a 404 to ``registered=False`` but lets the generic error
    PROPAGATE so the caller withholds instead of reporting a false ``name_typo`` (DS-2 / audit S5).

    S13: a 404/204 is trusted as a definitive "not registered" ONLY after the responder proves it is
    the Naming Service via its swagger beacon (``naming_client._require_verified_identity``). A 404
    caused by a WRONG base path or a FOREIGN host, which the old contract could not tell from a
    genuine "name not registered" 404, the former RESIDUAL, is now WITHHELD as a generic
    :class:`NamingServiceResponseError` instead. Two residuals remain (both judged acceptable): a
    foreign host that serves the ESS Naming swagger verbatim AND 204/404s on deviceNames
    (implausible), and, OUTSIDE this 204/404 gate, the ungated positive/record path in
    ``validate_name`` (a foreign 200 with a well-formed ``status`` is trusted without an identity
    probe; the measured hazard was a foreign 404, S13 Nit 1).
    """
