"""Exceptions for the EPICS Archiver Appliance REST client (read-only).

Mirrors ``naming_exceptions.py``: a per-service base plus connection/response errors.
"""


class ArchiverError(Exception):
    """Base error for the Archiver Appliance client."""


class ArchiverConnectionError(ArchiverError):
    """Failed to establish a connection to the Archiver Appliance."""


class ArchiverResponseError(ArchiverError):
    """Unexpected response (HTTP error / bad payload) from the Archiver Appliance."""
