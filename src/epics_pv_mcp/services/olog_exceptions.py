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


class OlogWholeModeRequired(OlogError):
    """A whole-entry round-trip (add_log_attachment, OA1b) was requested but the client is not in
    whole-mode (loopback URL + ``olog_assume_test_data``).

    Attaching to an existing entry goes through ``POST /logs/multipart`` = the server's destructive
    ``updateLog``, which PRUNES any attachment not resubmitted and OVERWRITES title/body/logbooks/
    tags/level/properties with what is sent. A safe attach must therefore round-trip the entry's
    FULL content — which is only readable whole (a redacted read withholds the free text and drops
    the raw attachment list). Against a redacted server the operation is refused. The service checks
    ``whole_mode`` up front; this backstop fires only if a raw read is reached, so a redacted entry
    is never round-tripped. NOT a server error — it never wraps an HTTP response."""


class OlogAttachmentDownloadDenied(OlogError):
    """Raw attachment bytes were requested but the read posture forbids them (OA1).

    The DEFENSE-IN-DEPTH backstop for the attachment-download privacy gate: bytes leave only when
    the
    client is in whole-mode (loopback + ``olog_assume_test_data``) AND
    ``olog_allow_attachment_download``
    is set (see :attr:`~epics_pv_mcp.services.olog_client.OlogClient.attachment_bytes_allowed`). The
    normal path checks that posture in the service layer and returns a structured ``withheld``
    result
    without a network call; this raise fires only if a byte-fetch is reached, so no un-redacted
    bytes can slip out through a code path that forgot the check. NOT a server error — it never
    wraps
    an HTTP response."""
