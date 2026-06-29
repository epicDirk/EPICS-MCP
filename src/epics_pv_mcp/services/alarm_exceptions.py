"""Exceptions for the Phoebus Alarm Logger REST client (read-only).

Mirrors ``archiver_exceptions.py``: a per-service base plus connection/response errors.
"""


class AlarmError(Exception):
    """Base error for the Phoebus Alarm Logger client."""


class AlarmConnectionError(AlarmError):
    """Failed to establish a connection to the Alarm Logger."""


class AlarmResponseError(AlarmError):
    """Unexpected response (HTTP error / bad payload) from the Alarm Logger."""
