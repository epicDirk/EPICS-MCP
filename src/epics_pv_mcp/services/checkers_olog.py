"""The Olog logbook query + write surface (split out of :mod:`~.checkers`, MA-1).

The ten ``query_olog_*`` functions (search / entry / logbooks / tags / levels / create /
add_attachment / update / download / list_attachments), plus the Olog-only helpers
(:func:`_unknown_level_note`, :func:`_olog_error_code`, :func:`_reject_unknown_level`,
:func:`_default_olog_id_factory`) and constants. Held apart from the four REST-plane checkers so the
Olog surface -- its ``dict[str, object]`` shapes and its gated write path -- lives in one module.

The functions resolve their module-global collaborators (``OlogClient``, ``get_config`` ...) in THIS
module's namespace, so a test double is installed by patching
``epics_pv_mcp.services.checkers_olog.<name>``. For backward compatibility :mod:`~.checkers`
re-exports the ten functions and ``_olog_error_code``.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Callable
from typing import TypedDict

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError, OlogWriteDeniedError
from epics_pv_mcp.olog_safety import get_olog_safety
from epics_pv_mcp.services._http import basic_auth_header, http_status
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.olog_attachments import (
    plan_attachments,
    read_uploads,
    write_download,
)
from epics_pv_mcp.services.olog_client import (
    DEFAULT_MAX_LOGS,
    OlogClient,
    split_level_values,
    unroundtrippable_attachment_filenames,
)
from epics_pv_mcp.services.olog_exceptions import (
    OlogConnectionError,
    OlogError,
    OlogFilterValueError,
    OlogResponseError,
)

# --- Tool result shapes (MA-1 Commit C) -------------------------------------------------------
# One ``total=False`` TypedDict per query function: every key across the function's return paths
# (disabled / not-found / withheld / success + the conditionally-added note/warnings/attachments/
# download-sink keys). total=False = all optional, so a schema with no ``required`` and permissive
# extras; the nested entry/entries/attachments projections stay ``dict[str, object]`` because their
# inner keys differ between whole-mode and redacted-mode reads. FastMCP turns these into a typed
# ``outputSchema`` (properties) instead of the bare ``{additionalProperties: true}`` a plain dict
# yields. mypy --strict checks every ``return {...}`` literal against its declared shape here.


class OlogSearchResult(TypedDict, total=False):
    enabled: bool
    entries: list[dict[str, object]]
    total: int
    total_matches: int | None
    capped: bool
    note: str


class OlogEntryResult(TypedDict, total=False):
    enabled: bool
    id: str
    found: bool | None
    entry: dict[str, object]
    note: str


class OlogLogbooksResult(TypedDict, total=False):
    enabled: bool
    logbooks: list[str]
    note: str


class OlogTagsResult(TypedDict, total=False):
    enabled: bool
    tags: list[str]
    note: str


class OlogLevelsResult(TypedDict, total=False):
    enabled: bool
    levels: list[str]
    default_level: str | None
    note: str


class OlogCreateResult(TypedDict, total=False):
    enabled: bool
    created: bool
    entry: dict[str, object]
    attachments_uploaded: list[dict[str, object]]
    note: str


class OlogAddAttachmentResult(TypedDict, total=False):
    enabled: bool
    added: bool
    entry: dict[str, object]
    attachments_uploaded: list[dict[str, object]]
    note: str


class OlogUpdateResult(TypedDict, total=False):
    enabled: bool
    updated: bool
    entry: dict[str, object]
    warnings: list[str]
    note: str


class OlogDownloadResult(TypedDict, total=False):
    enabled: bool
    downloaded: bool
    withheld: bool
    size_bytes: int
    content_type: str | None
    filename: str | None
    content_base64: str
    output_path: str
    note: str


class OlogListAttachmentsResult(TypedDict, total=False):
    enabled: bool
    id: str
    found: bool | None
    attachments: list[dict[str, object]]
    attachment_count: int | None
    withheld: bool
    note: str


_OLOG_DISABLED_NOTE = (
    "Phoebus Olog is disabled. Set EPICS_MCP_OLOG_URL to the Olog REST root "
    "(e.g. http://host:8080/Olog) to enable logbook search."
)
# The download/list posture message: raw attachment bytes (and filenames, for a list) are author
# free text and bypass the dict-based redaction, so they leave ONLY from a declared local sandbox
# with the explicit opt-in — the same whole-mode threshold as an entry read, plus a second flag.
_ATTACH_WITHHELD_NOTE = (
    "Attachment bytes are withheld. Raw bytes leave only from a declared local test sandbox "
    "(loopback EPICS_MCP_OLOG_URL + EPICS_MCP_OLOG_ASSUME_TEST_DATA) AND with "
    "EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD=true (bytes bypass the entry redaction, so they need "
    "their own opt-in; the by-id endpoint has no server-side per-log authorization). Run "
    "epics-doctor to see the effective posture."
)
_ATTACH_LIST_WITHHELD_NOTE = (
    "Attachment filenames are withheld (author free text). Only the count is shown unless the Olog "
    "is a declared local test sandbox (loopback + EPICS_MCP_OLOG_ASSUME_TEST_DATA)."
)
# A base64 download is returned IN the tool result (response tokens), so it is capped far below the
# path-based ceiling (olog_attach_max_bytes) — a large blob must go to a workspace file, not the
# model context. The effective base64 cap is min(this, olog_attach_max_bytes).
_BASE64_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024


def _default_olog_id_factory() -> str:
    """A fresh attachment UUID. Injected into :func:`query_olog_create` so the upload logic is
    deterministic in tests (the project's no-``uuid``-in-logic rule); production uses this
    default."""
    return str(uuid.uuid4())


def _unknown_level_note(client: OlogClient, level: str) -> str | None:
    """Flag a level filter whose value does not name a configured level (OA2).

    Olog answers an unrecognised ``level`` with 200 and 0 hits, so "this level does not exist"
    and "no entries have this level" are the same response. Left alone, a caller reports the
    second when the first is true. This runs ONLY on a genuinely empty result, so the extra
    ``GET /levels`` costs nothing on the path that found something.

    Deliberately makes the WEAKEST claim that is still useful — a statement about the VALUE, never
    about the CAUSE of the emptiness. The stronger wording ("this is 'no such level', NOT 'no
    entries have it'") was wrong in four measured ways, each of which this now avoids:

    * **Wildcards are honoured.** ``level="Inf*"`` returns the ``Info`` entries (measured), so a
      value containing ``*``/``?`` cannot be judged against the name list at all — those parts are
      excluded from the verdict and named separately as unchecked.
    * **An OR-ed list is not all-or-nothing.** With ``level="Info,Warnign"`` the search really did
      run on ``Info``; concluding "no such level" over the whole filter is false.
    * **The level is not the only possible cause.** A time window, ``logbooks``/``tags`` or a
      ``text`` filter can produce the same 0. The note says the value is unconfigured; it does not
      claim that is *why* nothing matched.
    * It is only reached for a genuinely empty RESULT, never an empty PAGE past the end of a
      paginated set — the caller decides that (see :func:`query_olog_search`).

    Returns ``None`` when every checkable part IS a configured level — then the result needs no
    annotation. If ``/levels`` itself cannot be read, that is SAID rather than swallowed or raised:
    a failed cross-check must not overturn a search that succeeded (withheld ≠ no), and must not
    silently look like a clean bill of health either."""
    requested = split_level_values(level)
    checkable = [part for part in requested if "*" not in part and "?" not in part]
    wildcards = [part for part in requested if part not in checkable]
    try:
        names, _default, _note = client.list_log_levels()
    except OlogError as exc:
        return (
            f"Nothing matched. Could not verify whether {level!r} names configured levels "
            f"({exc}) — Olog returns 0 hits and no error for an unrecognised level, so this "
            "result may mean the value does not exist rather than that no entries carry it."
        )
    known = {name.casefold() for name in names}
    unknown = [part for part in checkable if part.casefold() not in known]
    if not unknown:
        return None

    plural = len(unknown) != 1
    parts = ", ".join(repr(part) for part in unknown)
    note = (
        f"Nothing matched. {parts} {'do' if plural else 'does'} not name a configured level on "
        f"this server (known: {', '.join(names) or 'none'}), and Olog returns 0 hits rather than "
        f"an error for an unrecognised level — so {'these values' if plural else 'that value'} "
        "matched nothing by construction."
    )
    recognised = [part for part in checkable if part not in unknown]
    if recognised:
        note += (
            " The filter is OR-ed, so the search did still run on "
            f"{', '.join(map(repr, recognised))}."
        )
    if wildcards:
        note += (
            f" {', '.join(map(repr, wildcards))} contains a wildcard, which Olog honours but which "
            "cannot be checked against the level names — it is not part of this verdict."
        )
    if not recognised and not wildcards:
        note += " Other filters in this search may account for the empty result as well."
    return note


async def query_olog_search(
    text: str | None = None,
    logbooks: str | None = None,
    tags: str | None = None,
    start: str | None = None,
    end: str | None = None,
    size: int = DEFAULT_MAX_LOGS,
    offset: int = 0,
    sort: str = "down",
    level: str | None = None,
    title: str | None = None,
    timeout: float = 5.0,
) -> OlogSearchResult:
    """Search the Phoebus Olog logbook. Read-only, gated.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns a structured ``enabled: false``
    result and makes NO network call (no ESS egress). Every returned entry passes the Olog client's
    output projection: redacted against a real server (author dropped, free text withheld), but
    WHOLE against a declared loopback test sandbox — do NOT assume this layer only ever sees
    redacted data (ESS-spec pending; see services.olog_client._project). *offset*
    (Olog wire ``from``) pages past the first *size* results; *sort* orders by create time (``down``
    newest-first default, ``up`` oldest-first). ``total`` is the number of entries returned;
    ``total_matches`` is the true total across all pages (Olog ``hitCount``, ``None`` if the Olog
    version omits it); ``capped`` is True when more than *size* matched on this page.

    *level* and *title* (OA2/OA5) filter by triage level and title; both are server-honoured and
    case-insensitive (differentially probed 2026-07-19, both controls). A blank value for either is
    refused before any request. Because Olog answers an UNKNOWN level with 0 hits instead of an
    error, an empty level-filtered result gets a ``note`` saying so (:func:`_unknown_level_note`) —
    otherwise "this level does not exist" is indistinguishable from "no entries have it". Backs
    ``search_logbook``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "entries": [], "total": 0, "note": _OLOG_DISABLED_NOTE}

    def _run() -> OlogSearchResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entries, capped, total_matches = client.search_logbook(
            text=text,
            logbooks=logbooks,
            tags=tags,
            start=start,
            end=end,
            size=size,
            offset=offset,
            sort=sort,
            level=level,
            title=title,
        )
        result: OlogSearchResult = {
            "enabled": True,
            "entries": entries,
            "total": len(entries),
            "total_matches": total_matches,
            "capped": capped,
        }
        # Only a GENUINELY empty result is worth explaining. An empty PAGE past the end of a
        # paginated set (offset beyond total_matches) also has no entries, but something DID match
        # — annotating it would contradict total_matches in the very same payload.
        nothing_matched = not entries and not total_matches
        if level and nothing_matched:
            note = _unknown_level_note(client, level)
            if note is not None:
                result["note"] = note
        return result

    # Three outcomes, three classes. Collapsing them all into EpicsConnectionError (as this did)
    # tells the caller the service is unreachable when in truth the ARGUMENT was bad or the server
    # ANSWERED and said no — three different next actions reported as one.
    try:
        return await asyncio.to_thread(_run)
    except TimeWindowFormatError as exc:
        # Nothing was sent: the time window itself is unusable.
        raise EpicsError(f"Olog: {exc}", error_code="INVALID_TIME_WINDOW") from exc
    except OlogFilterValueError as exc:
        # Nothing was sent either: a blank level/title filter, which Olog would answer misleadingly
        # rather than reject (0 hits for level, unfiltered for title).
        raise EpicsError(f"Olog: {exc}", error_code="INVALID_INPUT") from exc
    except OlogConnectionError as exc:
        # Genuinely could not reach Olog.
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # The server answered (a served 4xx/5xx, or a payload we could not read).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_entry(log_id: str, timeout: float = 5.0) -> OlogEntryResult:
    """Return one Olog entry by id (URL+declaration-bound posture). Read-only, config-gated.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` with
    ``found: None`` — the plane was NOT checked, mirroring the ``archived: None`` /
    ``configured: None`` / ``registered: None`` / get_archive_info ``found: None`` siblings
    (S11 Zusatzfläche 3: this was the lone ``found: False`` among them — a definitive "does not
    exist" from a plane that was never asked). ``found`` is False ONLY for the definitive 404.
    Backs ``get_log_entry``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "id": log_id, "found": None, "note": _OLOG_DISABLED_NOTE}

    def _run() -> OlogEntryResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entry = client.get_log_entry(log_id)
        if entry is None:
            return {"enabled": True, "id": log_id, "found": False}
        return {"enabled": True, "id": log_id, "found": True, "entry": entry}

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_logbooks(timeout: float = 5.0) -> OlogLogbooksResult:
    """List the valid Olog logbook names. Read-only, config-gated, name-only (owners dropped).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes no
    network call. When enabled, returns the logbook NAMES only — each Olog ``owner`` (PII) is
    dropped in the client. These names are the valid filter values for
    ``search_logbook(logbooks=…)``. Backs ``list_logbooks``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "logbooks": [], "note": _OLOG_DISABLED_NOTE}

    def _run() -> OlogLogbooksResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        return {"enabled": True, "logbooks": client.list_logbooks()}

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_tags(timeout: float = 5.0) -> OlogTagsResult:
    """List the valid Olog tag names. Read-only, config-gated (a ``Tag`` has no owner — PII-free).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes no
    network call. These names are the valid filter values for ``search_logbook(tags=…)``. Backs
    ``list_tags``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "tags": [], "note": _OLOG_DISABLED_NOTE}

    def _run() -> OlogTagsResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        return {"enabled": True, "tags": client.list_tags()}

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_levels(timeout: float = 5.0) -> OlogLevelsResult:
    """List the valid Olog log levels. Read-only, config-gated (a ``Level`` has no owner).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes no
    network call. Levels are the logbook's TRIAGE axis and are site-configurable, so the valid
    values can only come from the server — they are the valid values for
    ``search_logbook(level=…)`` and ``create_log_entry(level=…)``. ``default_level`` is the one a
    create uses when none is given; it is ``None`` with an explaining ``note`` whenever the server
    does not state it unambiguously
    (missing/unreadable flag, none marked, or more than one marked — the server's own seed data has
    two). Backs ``list_log_levels``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {
            "enabled": False,
            "levels": [],
            "default_level": None,
            "note": _OLOG_DISABLED_NOTE,
        }

    def _run() -> OlogLevelsResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        names, default_level, note = client.list_log_levels()
        result: OlogLevelsResult = {
            "enabled": True,
            "levels": names,
            "default_level": default_level,
        }
        if note is not None:
            result["note"] = note
        return result

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


def _olog_error_code(exc: BaseException) -> str:
    """A discrete, freetext-free error code for an Olog failure (never a message string).

    An :class:`EpicsError` carries its own code; an Olog connection/response error maps to a
    discrete token (the served HTTP status for a response error, when known); any other
    :class:`OlogError` carries its code as a class attribute; anything else is INTERNAL. Never the
    exception message — a write FAILED audit must stay metadata-only (SEC-5), which is also why this
    stays freetext-free now that the read path classifies errors with it.

    ORDER MATTERS. The ``OlogError`` branch is LAST because ``OlogConnectionError`` and
    ``OlogResponseError`` are themselves OlogError subclasses: hoisting it would swallow both
    existing branches, and with them the HTTP-status resolution. (Their class attributes carry the
    same codes as a backstop, so a future reorder degrades to the honest token rather than to
    INTERNAL — but the status refinement lives only here.)

    Why the branch exists at all: every REFUSAL in ``olog_exceptions`` used to land on INTERNAL,
    which reads as a transient fault and invites a retry — each attempt burning a rate token and
    writing a FAILED audit line for a write that never happened."""
    if isinstance(exc, EpicsError):
        return exc.error_code
    if isinstance(exc, OlogConnectionError):
        return "OLOG_CONNECTION_ERROR"
    if isinstance(exc, OlogResponseError):
        status = http_status(exc)
        return f"OLOG_HTTP_{status}" if status is not None else "OLOG_RESPONSE_ERROR"
    if isinstance(exc, OlogError):
        return exc.error_code
    return "INTERNAL"


def _reject_unknown_level(client: OlogClient, level: str | None, caller: str) -> None:
    """Refuse a ``level`` the server does not know — the write-side counterpart to the logbook/tag
    existence checks in :func:`query_olog_update`.

    Olog validates none of the three on a write: an unknown level is stored verbatim and HTTP 200
    comes back, after which the entry matches no level filter at all (``level="Urgnet"``). A BLANK
    level is worse than unknown — it is accepted and silently CLEARS the field — so it is refused
    separately, before any request, and with its own message.

    Exact string match, deliberately. Neither read-side helper fits here:
    :func:`~epics_pv_mcp.services.olog_client.split_level_values` is search semantics (OR-split on
    ``[|,;]`` + trim) and :func:`_unknown_level_note` matches casefold and tolerates wildcards. A
    level being WRITTEN is a scalar — ``"Info,Problem"`` and ``" Info"`` are values the server would
    store literally and no filter would ever find again.

    The listing is fail-closed by construction (``list_log_levels`` raises rather than collapsing to
    an empty list), so an unreadable ``/levels`` refuses the write instead of waving it through.
    """
    if level is None:
        return
    if not level.strip():
        raise EpicsError(
            f"{caller}: level must not be empty — Olog accepts a blank level and would store it, "
            "silently clearing the entry's triage level",
            error_code="INVALID_INPUT",
        )
    known = client.list_log_levels()[0]
    if level not in known:
        raise EpicsError(
            f"{caller}: unknown level {level!r} — Olog does not validate the level on a write, so "
            "the entry would be stored with a value that no level filter matches. Known levels: "
            f"{', '.join(known)}",
            error_code="INVALID_INPUT",
        )


async def query_olog_create(
    title: str,
    logbooks: list[str],
    description: str | None = None,
    level: str | None = None,
    tags: list[str] | None = None,
    in_reply_to: str | None = None,
    attachments: list[str] | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
    *,
    id_factory: Callable[[], str] = _default_olog_id_factory,
) -> OlogCreateResult:
    """Create (or, with *in_reply_to*, reply to) an Olog log entry. MUTATING, gated, redacted.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes NO
    network call. When enabled, the CHEAP :class:`~epics_pv_mcp.olog_safety.OlogWriteGate` stages
    (env gate + test-server URL boundary + logbook allowlist) run BEFORE any I/O — such a denial
    raises (audited DENY) before a client is even constructed. The two REMAINING stages are later:
    since the level check was added, a ``GET /levels`` (only when a ``level`` is passed) and the
    attachment ``stat`` both happen BEFORE the attachment size cap and the rate token, so a denial
    from *those* two does follow some I/O. That ordering is deliberate — a bad level must not cost a
    rate token — and is spelled out at the call sites below. The full server response
    is run through the read redaction before return (owner dropped, title/description withheld). A
    completed write is audited ALLOW; a write that passes the gate but fails at the HTTP layer is
    audited FAILED (no entry id/owner) and re-raised. Backs ``create_log_entry`` / ``reply_to_log``.

    *attachments* (OA1) are workspace file paths uploaded with the entry (create-with-attachments,
    ``PUT /logs/multipart``); *embed_image_base64* is a small inline image whose
    ``![](attachment/<uuid>)`` markup is appended to the description. Attachment SIZES are summed
    (``stat``, not read) and fed to the gate's size cap BEFORE any bytes are read; the UUIDs are
    minted by *id_factory* (injected for deterministic tests). ``attachments_uploaded`` echoes the
    ``{id[, filename]}`` of each upload (filename only in whole-mode — it is author free text).
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "created": False, "note": _OLOG_DISABLED_NOTE}

    caller = "reply_to_log" if in_reply_to is not None else "create_log_entry"

    def _run() -> OlogCreateResult:
        gate = get_olog_safety()
        # Cheap gate checks FIRST — env / URL boundary / logbook allowlist — BEFORE any filesystem
        # I/O, so a denied write never even stats the attachment paths (no file-existence oracle for
        # a caller the gate rejects). A denial is audited DENY and propagates unchanged.
        gate.check_write_preconditions(logbooks, caller=caller)
        auth = basic_auth_header(cfg.olog_write_user, cfg.olog_write_password)
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=auth)
        # Level vocabulary BEFORE the rate token, mirroring the update path (see the ordering note
        # there): a typo must not cost a token. This is the ONE network read that happens before the
        # full gate, and only when a level was actually passed — a create that takes the server
        # default still makes exactly one HTTP call. It stays BEHIND check_write_preconditions (a
        # caller the gate rejects gets no vocabulary oracle) and AHEAD of plan_attachments (no path
        # is stat'ed for a write that a bad level already doomed), so both properties the comment
        # above promises survive; only "no I/O at all before the full gate" is now narrower.
        _reject_unknown_level(client, level, caller)
        # Now resolve + SIZE attachments (stat, NO read yet) so the size cap can refuse an
        # over-limit
        # upload before any bytes are materialised (anti-DoS). A bad path / bad base64 raises
        # EpicsError(INVALID_INPUT) here — before the full gate, and still before any file READ.
        plan = plan_attachments(attachments, embed_image_base64, id_factory)
        # Full gate: re-runs the (idempotent) cheap checks, then the attachment size cap and the
        # rate
        # token — the token is taken only here, on the success path, once. File READ + network
        # follow.
        gate.check_write_allowed(logbooks, caller=caller, attachment_bytes=plan.total_bytes)
        uploads = (
            read_uploads(plan.specs, max_total_bytes=get_config().olog_attach_max_bytes)
            if plan.specs
            else None
        )
        effective_description = (
            (description or "") + plan.inline_markup if plan.inline_markup else description
        )
        try:
            entry = client.create_log_entry(
                title=title,
                logbooks=logbooks,
                description=effective_description,
                level=level,
                tags=tags,
                in_reply_to=in_reply_to,
                attachments=uploads,
            )
        except Exception as exc:  # broad on purpose: audit ANY failed write attempt, then re-raise
            gate.audit_write_failed(
                logbooks=logbooks,
                level=level,
                title_len=len(title),
                error_code=_olog_error_code(exc),
                in_reply_to=in_reply_to,
                caller=caller,
            )
            raise
        gate.audit_write(
            entry_id=str(entry.get("id", "")),
            logbooks=logbooks,
            level=level,
            title_len=len(title),
            owner=cfg.olog_write_user,
            in_reply_to=in_reply_to,
            caller=caller,
            attachment_count=len(plan.specs),
            attachment_bytes=plan.total_bytes,
        )
        result: OlogCreateResult = {"enabled": True, "created": True, "entry": entry}
        if uploads is not None:
            # Filenames are author free text → whole-mode only; ids are UUIDs (safe) → always.
            result["attachments_uploaded"] = [
                (
                    {"id": u["id"], "filename": u["filename"]}
                    if client.whole_mode
                    else {"id": u["id"]}
                )
                for u in uploads
            ]
        return result

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_add_attachment(
    log_id: str,
    attachments: list[str] | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
    *,
    id_factory: Callable[[], str] = _default_olog_id_factory,
) -> OlogAddAttachmentResult:
    """Attach files to an EXISTING Olog entry (OA1b). MUTATING, gated, WHOLE-MODE ONLY.

    Default-disabled (no ``EPICS_MCP_OLOG_URL`` → ``enabled: false``, no network). WHOLE-MODE ONLY:
    the server's ``POST /logs/multipart`` runs a DESTRUCTIVE ``updateLog`` (it prunes any attachment
    not resubmitted and overwrites title/body/logbooks/tags/level/properties), so a safe attach must
    round-trip the target entry's FULL content — readable only whole (loopback +
    ``EPICS_MCP_OLOG_ASSUME_TEST_DATA``). Against a redacted server it is refused. The write goes
    through the SAME :class:`~epics_pv_mcp.olog_safety.OlogWriteGate` as create, with the allowlist
    keyed on the TARGET entry's own logbooks (read first). Backs ``add_log_attachment``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "added": False, "note": _OLOG_DISABLED_NOTE}
    if not attachments and not embed_image_base64:
        raise EpicsError(
            "add_log_attachment needs at least one attachment (attachments or embed_image_base64)",
            error_code="INVALID_INPUT",
        )
    if not log_id.strip().isdigit():
        raise EpicsError(
            "add_log_attachment needs a numeric log_id (the id of the target entry)",
            error_code="INVALID_INPUT",
        )
    caller = "add_log_attachment"

    def _run() -> OlogAddAttachmentResult:
        auth = basic_auth_header(cfg.olog_write_user, cfg.olog_write_password)
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=auth)
        # Whole-mode FIRST — before any read or write. It is required for the round-trip source AND
        # implies the loopback write-URL boundary; a redacted/remote server is refused structurally.
        if not client.whole_mode:
            raise OlogWriteDeniedError(
                "Olog write refused: add_log_attachment needs a declared local test sandbox "
                "(loopback EPICS_MCP_OLOG_URL + EPICS_MCP_OLOG_ASSUME_TEST_DATA). Attaching to an "
                "existing entry must round-trip its full content — the server's update prunes any "
                "attachment or field not resubmitted — which is withheld against a redacted server."
            )
        raw = client.get_raw_entry(log_id)
        if raw is None:
            raise EpicsError(
                f"add_log_attachment: no Olog entry with id {log_id}", error_code="OLOG_HTTP_404"
            )
        raw_logbooks = raw.get("logbooks")
        logbook_items = raw_logbooks if isinstance(raw_logbooks, list) else []
        target_logbooks = [
            str(item["name"]) for item in logbook_items if isinstance(item, dict) and "name" in item
        ]
        raw_level = raw.get("level")
        level = raw_level if isinstance(raw_level, str) else None
        title_len = len(str(raw.get("title") or ""))
        gate = get_olog_safety()
        # Gate ordering mirrors create: cheap checks (env / URL / allowlist on the TARGET logbooks)
        # with NO filesystem first, then stat-size the attachments, then the size cap + rate token.
        gate.check_write_preconditions(target_logbooks, caller=caller)
        plan = plan_attachments(attachments, embed_image_base64, id_factory)
        gate.check_write_allowed(target_logbooks, caller=caller, attachment_bytes=plan.total_bytes)
        uploads = read_uploads(plan.specs, max_total_bytes=get_config().olog_attach_max_bytes)
        try:
            entry = client.add_attachment(log_id, raw, uploads, inline_markup=plan.inline_markup)
        except Exception as exc:  # broad on purpose: audit ANY failed attach, then re-raise
            gate.audit_write_failed(
                logbooks=target_logbooks,
                level=level,
                title_len=title_len,
                error_code=_olog_error_code(exc),
                caller=caller,
                entry_id=log_id,
            )
            raise
        gate.audit_write(
            entry_id=str(entry.get("id", log_id)),
            logbooks=target_logbooks,
            level=level,
            title_len=title_len,
            owner=cfg.olog_write_user,
            caller=caller,
            attachment_count=len(plan.specs),
            attachment_bytes=plan.total_bytes,
        )
        # Whole-mode is guaranteed here (refused otherwise), so filenames may be echoed.
        return {
            "enabled": True,
            "added": True,
            "entry": entry,
            "attachments_uploaded": [{"id": u["id"], "filename": u["filename"]} for u in uploads],
        }

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_update(
    log_id: str,
    title: str | None = None,
    description: str | None = None,
    level: str | None = None,
    logbooks: list[str] | None = None,
    tags: list[str] | None = None,
    timeout: float = 5.0,
) -> OlogUpdateResult:
    """Edit an EXISTING Olog entry's fields (OA3). MUTATING, gated, WHOLE-MODE ONLY.

    Default-disabled (no ``EPICS_MCP_OLOG_URL`` → ``enabled: false``, no network). WHOLE-MODE ONLY
    for the same structural reason as ``add_log_attachment``: the server's update is DESTRUCTIVE (it
    prunes any attachment not resubmitted and NULLS any field not sent), so a safe edit must
    round-trip the target entry's FULL content — readable only whole (loopback +
    ``EPICS_MCP_OLOG_ASSUME_TEST_DATA``). Against a redacted server it is refused.

    ``None`` means "leave unchanged"; only the fields passed are overlaid. The write goes
    through the SAME :class:`~epics_pv_mcp.olog_safety.OlogWriteGate` as create, but the
    allowlist is keyed on the **UNION** of the entry's current logbooks and the ones it would
    end up in: moving an entry INTO a logbook and pulling it OUT of one are both writes to that
    logbook, so gating on either side alone leaves a hole. Backs ``update_log_entry``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "updated": False, "note": _OLOG_DISABLED_NOTE}
    if (
        title is None
        and description is None
        and level is None
        and logbooks is None
        and tags is None
    ):
        raise EpicsError(
            "update_log_entry needs at least one field to change "
            "(title, description, level, logbooks or tags)",
            error_code="INVALID_INPUT",
        )
    stripped_id = log_id.strip()
    # isascii() too: isdigit() alone accepts non-ASCII digits that int() handles inconsistently.
    if not (stripped_id.isascii() and stripped_id.isdigit()):
        raise EpicsError(
            "update_log_entry needs a numeric log_id (the id of the entry to edit)",
            error_code="INVALID_INPUT",
        )
    if title is not None and not title.strip():
        raise EpicsError(
            "update_log_entry: title must not be empty — unlike create, Olog's update does NOT "
            "reject an empty title, so the entry would silently lose it",
            error_code="INVALID_INPUT",
        )
    caller = "update_log_entry"

    def _run() -> OlogUpdateResult:
        auth = basic_auth_header(cfg.olog_write_user, cfg.olog_write_password)
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=auth)
        # Whole-mode FIRST — before any read or write. Required for the round-trip source AND it
        # implies the loopback write-URL boundary; a redacted/remote server is refused structurally.
        if not client.whole_mode:
            raise OlogWriteDeniedError(
                "Olog write refused: update_log_entry needs a declared local test sandbox "
                "(loopback EPICS_MCP_OLOG_URL + EPICS_MCP_OLOG_ASSUME_TEST_DATA). Editing an entry "
                "must round-trip its full content — the server's update prunes any attachment and "
                "nulls any field not resubmitted — which is withheld against a redacted server."
            )
        raw = client.get_raw_entry(stripped_id)
        if raw is None:
            raise EpicsError(
                f"update_log_entry: no Olog entry with id {stripped_id}",
                error_code="OLOG_HTTP_404",
            )
        # Safe-refuse BEFORE the gate: Olog matches attachments by filename (case-insensitively), so
        # a duplicate/missing filename would be silently dropped by the very round-trip that is
        # supposed to preserve it. A loud refusal beats a quietly lost file.
        unsafe = unroundtrippable_attachment_filenames(raw)
        if unsafe:
            raise EpicsError(
                f"update_log_entry: entry {stripped_id} cannot be edited safely — its attachments "
                f"({', '.join(unsafe)}) would not survive the round-trip, because Olog matches "
                "attachments by filename (case-insensitively) and would silently drop the "
                "duplicate/unnamed one.",
                error_code="INVALID_INPUT",
            )
        raw_logbooks = raw.get("logbooks")
        logbook_items = raw_logbooks if isinstance(raw_logbooks, list) else []
        current_logbooks = [
            str(item["name"]) for item in logbook_items if isinstance(item, dict) and "name" in item
        ]
        # The set the entry actually lives in AFTER the write (what must be non-empty)...
        effective_logbooks = logbooks if logbooks is not None else current_logbooks
        if not effective_logbooks:
            raise EpicsError(
                "update_log_entry: an entry must stay in at least one logbook",
                error_code="INVALID_INPUT",
            )
        # ...and the union of before+after (what the allowlist must cover — a move writes to both).
        gate_logbooks = sorted(set(current_logbooks) | set(effective_logbooks))
        raw_level = raw.get("level")
        entry_level = raw_level if isinstance(raw_level, str) else None
        title_len = len(title) if title is not None else len(str(raw.get("title") or ""))
        gate = get_olog_safety()
        # Gate ordering mirrors create/add: the cheap deterministic checks (env / URL / allowlist)
        # first, then the name validation reads, then the rate token last.
        gate.check_write_preconditions(gate_logbooks, caller=caller)
        # Olog's update does NOT validate logbook/tag existence (create does) or the level (neither
        # path does) — an unknown name would be stored silently as a phantom reference and an
        # unknown level as a value no filter matches, so all three are checked here.
        if logbooks is not None:
            unknown = sorted(set(logbooks) - set(client.list_logbooks()))
            if unknown:
                raise EpicsError(
                    f"update_log_entry: unknown logbook(s) {', '.join(unknown)} — Olog's update "
                    "would store them silently as phantom references.",
                    error_code="INVALID_INPUT",
                )
        if tags is not None:
            unknown_tags = sorted(set(tags) - set(client.list_tags()))
            if unknown_tags:
                raise EpicsError(
                    f"update_log_entry: unknown tag(s) {', '.join(unknown_tags)} — Olog's update "
                    "would store them silently as phantom references.",
                    error_code="INVALID_INPUT",
                )
        _reject_unknown_level(client, level, caller)
        gate.check_write_allowed(gate_logbooks, caller=caller, attachment_bytes=0)
        warnings: list[str] = []
        if description is None and not isinstance(raw.get("source"), str):
            warnings.append(
                "This entry carries no raw 'source' (a legacy/old-client entry). Olog will "
                "regenerate its body from the description through the CommonMark renderer, so "
                "markup-significant characters in the visible text may be rewritten."
            )
        try:
            entry = client.update_log_entry(
                raw,
                title=title,
                description=description,
                level=level,
                logbooks=logbooks,
                tags=tags,
            )
        except Exception as exc:  # broad on purpose: audit ANY failed edit, then re-raise
            gate.audit_write_failed(
                logbooks=gate_logbooks,
                level=level if level is not None else entry_level,
                title_len=title_len,
                error_code=_olog_error_code(exc),
                caller=caller,
                entry_id=stripped_id,
            )
            raise
        gate.audit_write(
            entry_id=str(entry.get("id", stripped_id)),
            logbooks=gate_logbooks,
            level=level if level is not None else entry_level,
            title_len=title_len,
            owner=cfg.olog_write_user,
            caller=caller,
        )
        result: OlogUpdateResult = {"enabled": True, "updated": True, "entry": entry}
        if warnings:
            result["warnings"] = warnings
        return result

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_download(
    log_id: str | None = None,
    filename: str | None = None,
    attachment_id: str | None = None,
    output_path: str | None = None,
    as_base64: bool = False,
    timeout: float = 5.0,
) -> OlogDownloadResult:
    """Download one Olog attachment's raw bytes. Read-only, config-gated, and POSTURE-gated (OA1).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` +
    ``downloaded: false`` and makes NO network call. Bytes leave ONLY from a declared local sandbox
    (whole-mode) AND with the explicit ``EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD`` opt-in —
    otherwise
    the result is ``withheld: true`` and NO byte fetch happens (the posture is checked BEFORE the
    request; the client's :meth:`~.OlogClient._require_attachment_bytes_allowed` is a further
    backstop). Identify the attachment by ``(log_id + filename)`` — the primary route — or by
    ``attachment_id`` (the by-id route an inline image uses). Bytes cross the MCP boundary either
    written to ``output_path`` (a NEW workspace file, ``EPICS_MCP_ALLOWED_ROOTS``-checked) or, with
    ``as_base64=true``, base64-encoded in the result (small files only — the payload is response
    tokens). Backs ``download_log_attachment``.
    """
    if not filename and not attachment_id:
        raise EpicsError(
            "download needs either filename (with log_id) or attachment_id",
            error_code="INVALID_INPUT",
        )
    if filename and not log_id:
        raise EpicsError("download by filename needs log_id", error_code="INVALID_INPUT")
    if not as_base64 and not output_path:
        raise EpicsError(
            "download needs output_path (a workspace file to write) or as_base64=true",
            error_code="INVALID_INPUT",
        )
    if as_base64 and output_path:
        # Contradictory sinks: base64 would silently win (the elif below never runs) and the
        # smaller base64 cap would apply — an output_path handover for a >5 MiB file would then
        # fail spuriously. Refuse up front rather than pick one silently.
        raise EpicsError(
            "download takes either output_path or as_base64, not both",
            error_code="INVALID_INPUT",
        )
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "downloaded": False, "note": _OLOG_DISABLED_NOTE}

    def _run() -> OlogDownloadResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        # Posture FIRST: if raw bytes may not leave, withhold structurally — no byte fetch at all.
        if not client.attachment_bytes_allowed:
            return {
                "enabled": True,
                "downloaded": False,
                "withheld": True,
                "note": _ATTACH_WITHHELD_NOTE,
            }
        # A base64 result rides back in the tool output (response tokens), so it is capped far below
        # the path-based ceiling; a path download uses the client's default (olog_attach_max_bytes).
        max_bytes = (
            min(cfg.olog_attach_max_bytes, _BASE64_DOWNLOAD_MAX_BYTES) if as_base64 else None
        )
        if attachment_id is not None:
            content, server_filename, content_type = client.get_attachment_by_id(
                attachment_id, max_bytes=max_bytes
            )
        elif log_id is not None and filename is not None:
            content, server_filename, content_type = client.get_attachment(
                log_id, filename, max_bytes=max_bytes
            )
        else:  # unreachable — the outer guards ensure one identity is fully specified
            raise EpicsError("download identity is incomplete", error_code="INVALID_INPUT")
        result: OlogDownloadResult = {
            "enabled": True,
            "downloaded": True,
            "size_bytes": len(content),
            "content_type": content_type,
            "filename": server_filename,
        }
        if as_base64:
            result["content_base64"] = base64.b64encode(content).decode("ascii")
        elif output_path is not None:
            result["output_path"] = write_download(output_path, content)
        else:  # unreachable — guarded above
            raise EpicsError("download needs output_path or as_base64", error_code="INVALID_INPUT")
        return result

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_list_attachments(
    log_id: str, timeout: float = 5.0
) -> OlogListAttachmentsResult:
    """List one Olog entry's attachments (id + fileMetadataDescription; filename whole-mode only).

    Read-only, config-gated. Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset returns
    ``enabled: false`` + ``found: None``. ``found: false`` for a definitive 404. Reuses
    :meth:`~.OlogClient.get_log_entry`'s projection: in whole-mode the raw ``attachments`` array
    (id / filename / fileMetadataDescription) is surfaced; in redacted mode only the count is known
    and filenames are withheld (author free text). Backs ``list_log_attachments``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {
            "enabled": False,
            "id": log_id,
            "found": None,
            "attachments": [],
            "note": _OLOG_DISABLED_NOTE,
        }

    def _run() -> OlogListAttachmentsResult:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entry = client.get_log_entry(log_id)
        if entry is None:
            return {"enabled": True, "id": log_id, "found": False, "attachments": []}
        raw = entry.get("attachments")
        if isinstance(raw, list):
            # whole-mode: the raw list is present (id/filename/fileMetadataDescription/checksum).
            attachments: list[dict[str, object]] = [
                {
                    "id": item.get("id"),
                    "filename": item.get("filename"),
                    "fileMetadataDescription": item.get("fileMetadataDescription"),
                }
                for item in raw
                if isinstance(item, dict)
            ]
            return {
                "enabled": True,
                "id": log_id,
                "found": True,
                "attachments": attachments,
                "attachment_count": len(attachments),
            }
        # Redacted mode: no raw list — only the synthesised count; filenames withheld. entry is an
        # untyped dict, so narrow the count to int|None at runtime (a non-int would be a projection
        # bug — surface None, not a wrong-typed value).
        raw_count = entry.get("attachment_count")
        return {
            "enabled": True,
            "id": log_id,
            "found": True,
            "attachments": [],
            "withheld": True,
            "attachment_count": raw_count if isinstance(raw_count, int) else None,
            "note": _ATTACH_LIST_WITHHELD_NOTE,
        }

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc
