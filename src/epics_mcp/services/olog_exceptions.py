"""Exceptions for the Phoebus Olog REST client (read-only).

A per-service base plus connection/response errors, deriving from the shared
:mod:`epics_mcp.services.rest_exceptions` roots (so ``except RestClientError`` catches every
plane, while ``except OlogError`` still catches just this one). The 5th REST plane anticipated by
``services/_http`` and ``services/checkers``.
"""

from typing import ClassVar

from epics_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class OlogError(RestClientError):
    """Base error for the Olog client.

    ``error_code`` is the discrete, freetext-free token that
    :func:`~epics_mcp.services.checkers._olog_error_code` reports to the caller AND writes into
    the write audit. Each subclass carries its own, mirroring how :class:`EpicsError` and its
    subclasses work, so a new Olog exception brings its code with it instead of silently landing on
    the fallback.

    That fallback used to be the only behaviour, and it was wrong for every REFUSAL in this module:
    ``INTERNAL`` reads as a transient server fault, which invites a retry that burns a rate token
    and writes a FAILED audit line for a write that never happened. A refusal is permanent, the
    caller must fix the request, not repeat it.

    Kept freetext-free on purpose (SEC-5): the audit is metadata-only, so a code must never be
    derived from an exception message.
    """

    error_code: ClassVar[str] = "INTERNAL"


class OlogFilterValueError(ValueError):
    """A search filter value is unusable, refused BEFORE any request is issued (OA2/OA5).

    Deliberately a :class:`ValueError` and NOT an :class:`OlogError`: nothing was sent, so this is
    a bad ARGUMENT, not a service failure, the same shape as
    :class:`~epics_mcp.services._time_window.TimeWindowFormatError`, and the service maps it to
    ``INVALID_INPUT`` rather than to a transport/response code.

    The case that makes this necessary is measured, not hypothetical: a ``level`` that is present
    but blank is NOT "no filter" on the Olog side. ``""`` splits to ``[""]`` and becomes a wildcard
    matching nothing, so the server answers HTTP 200 with **0 hits** (measured 2026-07-19), which
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


class OlogRoundTripUnsafe(OlogError):
    """An attachment list cannot be submitted safely, because the match would drop one of them.

    TWO occasions, and they differ in who owns the offending name and in what protects it:

    * the ENTRY's own attachments cannot survive the round-trip (OA3 update, and the OA1b attach,
      which round-trips the same list). The service pre-checks this up front via
      :func:`~epics_mcp.services.olog_client.unroundtrippable_attachment_filenames`, and only on
      the UPDATE path (``checkers_olog``); the client re-checks as a backstop.
    * the CALLER's new uploads collide with that submitted list (OA1b attach, OQ12). There is no
      service pre-check for this one at all, so the client's check is the only one, and it runs on
      the union that is actually sent rather than on the existing half.

    Olog keeps attachments across an update by ``retainAll`` against the SUBMITTED list, and that
    match is **filename-keyed**, ``Attachment.compareTo`` compares ``filename.compareToIgnoreCase``
    and the submitted side is a ``TreeSet`` (Attachment.java:55-68, Log.java:63), so the id is never
    consulted. Filenames colliding case-insensitively therefore collapse to one element, and an
    attachment without a usable filename cannot be matched at all, either way the server would
    silently DROP an attachment from an edit the caller only meant to change a field in.

    Refusing is deliberate (safe-refuse): a loud error is better than a silently lost file. Where
    the checks sit, and where one of them does NOT, is listed above rather than summarised here,
    because the two occasions are not covered alike.

    MEASURED 2026-07-20 against Olog 6.0.4-SNAPSHOT, and the measurement refines WHERE the danger
    sits without removing it. A controlled probe (identical multipart submission, second filename
    differing only in CASE → HTTP 400 and one attachment; genuinely different → 200 and two) shows
    the colliding state cannot be created *through multipart-with-files*. But the 400 is NOT a
    collision check:

    * ``Attachment.compareTo`` really does compare ``compareToIgnoreCase`` inside a ``TreeSet``, so
      the colliding pair COLLAPSES on deserialisation, the silent drop above is real.
    * What then fails is ``AttachmentsUploadUtil.areMultipartFilesOrphaned``, which matches an
      uploaded file back to its metadata with case-SENSITIVE ``equals``. After the collapse the
      uploaded file has no metadata left, is flagged orphaned, and the request is refused.

    So the refusal is a side effect of having sent a FILE, not a guard against collisions, and
    ``areMultipartFilesOrphaned`` returns false immediately when there are NO file parts. A plain
    field edit (``update_log_entry`` sends the logEntry part and zero files) therefore never reaches
    that check: the collapse happens, ``retainAll`` prunes, and nothing complains. That is exactly
    the scenario this exception guards, and it remains unprotected on the server side. NOT a server
    error, it never wraps an HTTP response."""

    # Mirrors the service-level pre-check for exactly this case, which already raises
    # EpicsError(INVALID_INPUT): the layer that catches it must not change the verdict.
    error_code: ClassVar[str] = "INVALID_INPUT"
