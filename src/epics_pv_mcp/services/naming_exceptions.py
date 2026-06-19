"""Exceptions for the ESS Naming Service client.

Vendored (slimmed) from pvValidator's ``pvValidatorUtils/exceptions.py`` — only the
two Naming-Service errors the cross-plane check needs, so this repo stays standalone
(no pvValidator/SWIG dependency). Source: ``D:/pvValidator/.../exceptions.py``.
"""


class NamingServiceError(Exception):
    """Base error for the ESS Naming Service client."""


class NamingServiceConnectionError(NamingServiceError):
    """Failed to establish a connection to the Naming Service."""


class NamingServiceResponseError(NamingServiceError):
    """Unexpected response (HTTP error / bad payload) from the Naming Service."""
