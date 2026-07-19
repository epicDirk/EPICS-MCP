"""Exceptions for the Phoebus Olog REST client (read-only).

A per-service base plus connection/response errors, deriving from the shared
:mod:`epics_pv_mcp.services.rest_exceptions` roots (so ``except RestClientError`` catches every
plane, while ``except OlogError`` still catches just this one). The 5th REST plane anticipated by
``services/_http`` and ``services/checkers``.
"""

from typing import ClassVar

from epics_pv_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class OlogError(RestClientError):
    """Base error for the Olog client.

    ``error_code`` is the discrete, freetext-free token that
    :func:`~epics_pv_mcp.services.checkers._olog_error_code` reports to the caller AND writes into
    the write audit. Each subclass carries its own, mirroring how :class:`EpicsError` and its
    subclasses work — so a new Olog exception brings its code with it instead of silently landing on
    the fallback.

    That fallback used to be the only behaviour, and it was wrong for every REFUSAL in this module:
    ``INTERNAL`` reads as a transient server fault, which invites a retry that burns a rate token
    and writes a FAILED audit line for a write that never happened. A refusal is permanent — the
    caller must fix the request, not repeat it.

    Kept freetext-free on purpose (SEC-5): the audit is metadata-only, so a code must never be
    derived from an exception message.
    """

    error_code: ClassVar[str] = "INTERNAL"


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

    error_code: ClassVar[str] = "OLOG_CONNECTION_ERROR"


class OlogResponseError(OlogError, RestResponseError):
    """Unexpected response (HTTP error / bad payload) from the Olog service."""

    # Refined to OLOG_HTTP_<status> by _olog_error_code when the served status is known;
    # this is the honest fallback for a response that carried no readable status.
    error_code: ClassVar[str] = "OLOG_RESPONSE_ERROR"


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

    # Same condition, same code as the service-level twin that normally catches this first:
    # checkers raises OlogWriteDeniedError (= OLOG_WRITE_DENIED) for a non-whole-mode write.
    error_code: ClassVar[str] = "OLOG_WRITE_DENIED"


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

    # Mirrors the service-level pre-check for exactly this case, which already raises
    # EpicsError(INVALID_INPUT) — the layer that catches it must not change the verdict.
    error_code: ClassVar[str] = "INVALID_INPUT"


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

    # A read-side privacy refusal: neither the write code nor a transport code fits, so it
    # gets its own — a caller can tell "the posture forbids this" from "the service failed".
    error_code: ClassVar[str] = "OLOG_ATTACHMENT_DOWNLOAD_DENIED"
