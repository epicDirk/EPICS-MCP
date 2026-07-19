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


class OlogFilterValueError(ValueError):
    """A search filter value is unusable, refused BEFORE any request is issued (OA2/OA5).

    Deliberately a :class:`ValueError` and NOT an :class:`OlogError`: nothing was sent, so this is
    a bad ARGUMENT, not a service failure — the same shape as
    :class:`~epics_pv_mcp.services._time_window.TimeWindowFormatError`, and the service maps it to
    ``INVALID_INPUT`` rather than to a transport/response code.

    The case that makes this necessary is measured, not hypothetical: a ``level`` that is present
    but blank is NOT "no filter" on the Olog side. ``""`` splits to ``[""]`` and becomes a wildcard
    matching nothing, so the server answers HTTP 200 with **0 hits** (measured 2026-07-19) — which
    reads exactly like "there are no such entries". ``title`` is asymmetric here (blank IS dropped,
    yielding the unfiltered count), so neither behaviour can be assumed from the other. Refusing a
    blank filter keeps a caller from reporting a fabricated emptiness as a fact."""


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


class OlogRoundTripUnsafe(OlogError):
    """An entry cannot be updated (OA3) because its attachments would not survive the round-trip.

    Olog keeps attachments across an update by ``retainAll`` against the SUBMITTED list, and that
    match is **filename-keyed** — ``Attachment.compareTo`` compares ``filename.compareToIgnoreCase``
    and the submitted side is a ``TreeSet`` (Attachment.java:55-68, Log.java:63), so the id is never
    consulted. Filenames colliding case-insensitively therefore collapse to one element, and an
    attachment without a usable filename cannot be matched at all — either way the server would
    silently DROP an attachment from an edit the caller only meant to change a field in.

    Refusing is deliberate (safe-refuse): a loud error is better than a silently lost file. The
    service checks this up front via
    :func:`~epics_pv_mcp.services.olog_client.unroundtrippable_attachment_filenames`; the client
    re-checks as a defense-in-depth backstop. NOT a server error — it never wraps an HTTP
    response."""


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
