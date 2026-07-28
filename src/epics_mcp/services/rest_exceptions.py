"""Common root for the four read-only REST clients' exception trios (M3/L-REST-Exceptions/C3).

Each REST plane (Naming / ChannelFinder / Archiver / Alarm) keeps its own named trio for clear,
service-specific error messages, but the three roots here let a caller catch across planes:

* :class:`RestClientError`, any REST client failure (base of all four service bases)
* :class:`RestConnectionError`, a connection could not be established/completed (base of the four
  ``*ConnectionError``)
* :class:`RestResponseError`, an unexpected response: HTTP error or bad payload (base of the four
  ``*ResponseError``)

The per-service ``*ConnectionError`` / ``*ResponseError`` derive from BOTH their service base and
the matching root here (multiple inheritance), so ``except ArchiverConnectionError`` still works AND
the shared :func:`epics_mcp.services._http.rest_get_json` can be typed against
``type[RestConnectionError]`` / ``type[RestResponseError]``.
"""

from __future__ import annotations


class RestClientError(Exception):
    """Base for any read-only REST client failure (all four service bases derive from this)."""


class RestConnectionError(RestClientError):
    """A connection to a REST service could not be established or completed."""


class RestResponseError(RestClientError):
    """An unexpected response from a REST service (HTTP error status or non-JSON/bad payload)."""
