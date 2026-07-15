"""Client for the Phoebus Olog (electronic logbook) REST API — read plus one gated write path.

The reads are default-disabled and DS-PRIVACY-redacted; the single write
(:meth:`OlogClient.create_log_entry`, ``PUT /logs``) is gated separately by
:mod:`epics_pv_mcp.olog_safety` (this module only carries the transport) and its response goes
through the SAME output projection as a read — see :meth:`OlogClient._project`, which decides
whether that projection redacts.

Read-only jobs:

  GET {root}/logs/search?desc=…&logbooks=…&tags=…&start=…&end=…&size=…&from=…&sort=…  — search
  GET {root}/logs/{id}                                                  — one log entry
  GET {root}/logbooks                                                   — list logbook names
  GET {root}/tags                                                       — list tag names

``olog_url`` is the Olog REST root incl. context path (e.g. ``http://olog:8080/Olog``); the
``/logs`` paths are appended. Reading needs no auth by default; an optional ``Authorization``
header is forwarded for secured deployments. Structure mirrors
:mod:`epics_pv_mcp.services.alarm_client`.

⛔ **DS-PRIVACY — REDACTED BY DEFAULT.** Olog entries carry person data not only
in ``owner`` but throughout FREE TEXT — the title, the description/body, attachment filenames, and
per-item owners inside the ``logbooks``/``tags`` structures. A key-based strip is therefore NOT
sufficient. Against any REAL (non-loopback) server every entry is run through a strict OUTPUT
ALLOWLIST (:func:`_project_log_entry`): technical fields (``id``/dates/``level``/``state``) are
kept; ``logbooks``/``tags`` are reshaped to name-only lists (their per-item owners dropped);
``title``/``description`` keep their key but their value is WITHHELD (a name inside the text cannot
leak); attachments are surfaced only as a COUNT; and ``owner``/``source``/``properties``
(author-written) are dropped entirely.

Entries leave WHOLE (:func:`_expand_log_entry`) only when BOTH hold: the URL is loopback AND the
operator has DECLARED the data synthetic (``olog_assume_test_data``). Withholding the free text
otherwise costs a logbook its entire point: a search returns ids whose content the caller cannot
judge, and a write cannot verify what it just wrote. **ESS-SPEC PENDING** (decisions 2026-07-15):
the withholding policy above was written against an ASSUMED rule — none was ever specified for this
server — so it is DEFERRED for declared test data until a real specification exists, then re-applied
here rather than re-invented. The allowlist and the projection are kept intact meanwhile; only the
DEFAULT moved.

Both conditions are needed, and neither is redundant: a loopback ADDRESS cannot prove the DATA is
synthetic (a port-forward serves production on localhost with the URL unchanged — demonstrated live,
QA 2026-07-15), and the declaration alone would not catch "pointed at the facility and forgot". For
the same reason redirects are refused rather than followed (:meth:`OlogClient._get`): a hop would
move the data's true origin without changing the URL the decision was made from.

This is a RUNTIME output policy and is unrelated to keeping person names out of COMMITTED files —
that is enforced separately (the facility-agnostic guards and hand-transcription rule, CLAUDE.md).

⚠️ Search-param names (``desc``/``logbooks``/``tags``/``start``/``end``/``size``) and the entry
field names follow the documented Phoebus Olog model. ``start``/``end``/``tz`` are no longer
best-effort — they were smoke-tested live (2026-07-15) and the window turned out to be actively
BROKEN as sent: Olog cannot parse ISO-8601 and says so only by returning an empty result. See
:mod:`epics_pv_mcp.services.olog_time` for the mechanism and :meth:`OlogClient._add_window` for the
fix. The remaining names are still best-effort — the redaction is defence-in-depth regardless of
which extra fields a given Olog version returns.
"""

from __future__ import annotations

from epics_pv_mcp.config import get_config
from epics_pv_mcp.services._http import (
    build_retrying_session,
    http_status,
    is_http_400,
    is_http_404,
    is_loopback_url,
    rest_get_json,
    rest_put_json,
)
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError, OlogResponseError
from epics_pv_mcp.services.olog_time import (
    OLOG_WIRE_TZ,
    OlogTimeFormatError,
    normalize_olog_time,
)
from epics_pv_mcp.services.redact import redact_record

# Default cap on returned log entries (a wide/empty search is otherwise unbounded).
DEFAULT_MAX_LOGS = 50

# Static, non-identifying client-info header value (server-side only logged, never persisted). NOT a
# personal username/hostname — a facility-agnostic constant.
_CLIENT_INFO = "epics-pv-mcp"

# DS-PRIVACY output allowlist: the only entry fields that may leave whenever the posture redacts
# (a DECLARED loopback sandbox returns the whole entry — see _expand_log_entry). ``title``/
# are kept for their PRESENCE but their value is withheld (they are free text). Everything not
# listed — ``owner``, ``source``, ``properties``, raw ``attachments``, and any field a future Olog
# version adds — is dropped, so a new person-bearing field never leaks by default.
_LOG_ALLOWLIST = frozenset(
    {"id", "createdDate", "modifyDate", "level", "state", "title", "description"}
)
# Fields whose VALUE is author-written free text: kept-but-withheld (a person can be named inside).
_LOG_FREETEXT = frozenset({"title", "description"})


def _names(items: object) -> list[str]:
    """The ``name`` of each dict in *items* (logbook/tag structs); per-item owners dropped."""
    if not isinstance(items, list):
        return []
    return [str(item["name"]) for item in items if isinstance(item, dict) and "name" in item]


def _derive_shape(entry: dict[str, object], out: dict[str, object]) -> dict[str, object]:
    """Add the DERIVED fields both projections owe their callers, and return *out*.

    ``logbooks``/``tags`` are reshaped from Olog's list-of-structs to name-only lists, and
    ``attachment_count`` is SYNTHESISED (it is not an Olog field at all). Shared by both modes so
    the returned SHAPE never depends on the redaction: a caller sees the same keys and the same
    types either way, and the full mode only ADDS fields. Skipping this on the full path would
    silently drop ``attachment_count`` and flip ``logbooks`` from list[str] to list[dict] — and
    ``dict[str, object]`` is wide enough that mypy would not say a word.
    """
    out["logbooks"] = _names(entry.get("logbooks"))
    out["tags"] = _names(entry.get("tags"))
    attachments = entry.get("attachments")
    out["attachment_count"] = len(attachments) if isinstance(attachments, list) else 0
    return out


def _project_log_entry(entry: dict[str, object]) -> dict[str, object]:
    """Return *entry* reduced to the DS-PRIVACY allowlist (see the module docstring).

    Technical fields kept; ``logbooks``/``tags`` reshaped to name-only lists; ``title``/
    ``description`` withheld; attachments surfaced as a count only; ``owner``/``source``/
    ``properties`` dropped. The privacy barrier for every Olog entry that leaves this module,
    except from a declared local sandbox — see :meth:`OlogClient._project` for when it applies.
    """
    redacted = redact_record(entry, allowed=_LOG_ALLOWLIST, freetext=_LOG_FREETEXT)
    return _derive_shape(entry, redacted)


def _expand_log_entry(entry: dict[str, object]) -> dict[str, object]:
    """Return *entry* WHOLE — free text, owner and all — for a declared local test server only.

    ESS-SPEC PENDING (decisions 2026-07-15). The redaction above was written against an ASSUMED
    privacy rule, never a specification: no such rule was ever issued for this server. Withholding
    ``title``/``description`` costs the whole point of a logbook (a search returns ids whose content
    the caller cannot see, and a write cannot verify what it just wrote), so the policy is DEFERRED
    until a real specification exists — at which point it is re-applied here, not re-invented.
    Deferred, NOT dropped: :func:`_project_log_entry` and the allowlist stay intact and guard
    everything else.

    Reaching this function requires TWO independent conditions (see :meth:`OlogClient.__init__`):
    a loopback URL AND ``olog_assume_test_data``. Neither is sufficient alone, and the reason is
    worth knowing before anyone "simplifies" it: a loopback ADDRESS does not prove the DATA is
    synthetic — a port-forward (``ssh -L 8080:olog-prod:8080``) serves a production logbook on
    localhost without the URL ever changing, so binding to the URL alone silently un-redacts
    production (demonstrated live, QA 2026-07-15). No URL inspection can see through a tunnel; only
    a person can assert what the data is, which is what the flag is for. The loopback condition
    stays because it still catches the other failure — "pointed at the facility and forgot".
    """
    return _derive_shape(entry, dict(entry))


def _entries_of(data: object) -> list[object]:
    """The list of raw entries from an Olog search response (a bare list or a ``{logs: [...]}``)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        logs = data.get("logs")
        if isinstance(logs, list):
            return logs
    return []


def _hit_count(data: object) -> int | None:
    """The Olog ``hitCount`` (total matches for the query) from a ``{logs, hitCount}`` wrapper.

    Returns ``None`` for a bare-list response (an Olog version that returns just the page of logs
    with no total); the caller then falls back to the length of the returned page. ``hitCount`` is
    the total across ALL pages and need not equal the returned page size — Olog documents this on
    ``SearchResult`` (it differs whenever ``from``/``size`` paginate).
    """
    if isinstance(data, dict):
        count = data.get("hitCount")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


class OlogClient:
    """Client for the Phoebus Olog REST API. Reads are GET-only; the one write (create_log_entry,
    PUT /logs) is gated by olog_safety. Every path (read + write) leaves through _project:
    redacted against a real server, whole against a declared local sandbox."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        auth_header: str | None = None,
        assume_test_data: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = build_retrying_session(auth_header=auth_header)
        # ESS-SPEC PENDING (see _expand_log_entry): redact everything EXCEPT a server that is BOTH
        # loopback AND declared to hold test data. Two conditions, because neither suffices alone:
        # a loopback address does not prove the data is synthetic (a port-forward serves production
        # on localhost — demonstrated live, QA 2026-07-15), and a flag alone would not catch
        # "pointed
        # at the facility and forgot". Fails closed on both: an unparseable URL is not loopback, and
        # the declaration defaults to false. `assume_test_data=None` reads the config; pass
        # it explicitly to decide per client (tests do).
        if assume_test_data is None:
            assume_test_data = get_config().olog_assume_test_data
        self._redact = not (is_loopback_url(self.base_url) and assume_test_data)

    def _project(self, entry: dict[str, object]) -> dict[str, object]:
        """Project one entry on its way out: redacted for a real server, whole for a local sandbox.

        The ONE place the mode is decided. Note this must not be derived from the write gate's
        ``_url_write_allowed``: that is True for an allowlisted REMOTE too, which as a read
        predicate would surface a production logbook in the clear.
        """
        return _project_log_entry(entry) if self._redact else _expand_log_entry(entry)

    def _get(self, url: str, params: dict[str, str]) -> object:
        """Issue a GET and return parsed JSON, translating failures to Olog exceptions.

        Redirects are refused, not followed: the output posture is decided from the CONFIGURED host
        (see :meth:`_project`), so a followed hop would let a loopback URL serve entries from a real
        server un-redacted — demonstrated live (QA 2026-07-15). Olog's REST API has no legitimate
        redirect, so a loud error is the right answer.
        """
        return rest_get_json(
            self.session,
            url,
            params,
            self.timeout,
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            allow_redirects=False,
        )

    def check_connectivity(self) -> bool:
        """Return True if Olog is reachable; raise OlogConnectionError otherwise.

        A HEAD to the service root proves transport + TLS (the CA bundle) — any HTTP response counts
        as reachable (status irrelevant, as in the Naming client). A transport/TLS failure re-raises
        as OlogConnectionError with the original requests error as ``__cause__``, so a caller can
        inspect it (:func:`is_ssl_error`) to tell a CA problem from a plain unreachable host.
        """
        try:
            self.session.head(self.base_url, timeout=self.timeout)
            return True
        except OSError as exc:
            # requests.exceptions.RequestException ⊂ OSError (see naming_client.check_connectivity).
            raise OlogConnectionError(
                f"Failed to connect to Olog at {self.base_url}: {exc}"
            ) from exc

    @staticmethod
    def _add_window(params: dict[str, str], start: str | None, end: str | None) -> None:
        """Put the normalized time window into *params* — or raise before anything is sent.

        Olog cannot read ISO-8601 and does not say so: an unparseable value degrades to *now*,
        collapsing the window to nothing and returning a well-formed EMPTY result (measured live;
        see :mod:`epics_pv_mcp.services.olog_time`). So every value is normalized to a form Olog
        actually parses, or refused here.

        ``tz`` is sent iff an absolute value is present: it exists only to interpret wall-clock
        strings, and Olog resolves relative amounts by instant arithmetic where the zone is a
        no-op. Omitting it would let Olog read our strings in ITS default zone — an invisible
        offset against any Olog not running UTC.
        """
        start_is_absolute = end_is_absolute = False
        if start:
            params["start"], start_is_absolute = normalize_olog_time(start, param="start")
        if end:
            params["end"], end_is_absolute = normalize_olog_time(end, param="end")
        if start_is_absolute or end_is_absolute:
            params["tz"] = OLOG_WIRE_TZ
        if start_is_absolute and end_is_absolute and params["start"] > params["end"]:
            # Both absolute -> comparable right here, without a clock. The normalized form is
            # fixed-width and always UTC, so a string compare IS a chronological one. Left to
            # Olog this is a 400, which an anonymous read only ever sees as a 401
            # ("unauthorized") — a misleading answer to what is simply a swapped window. A
            # mixed/relative window stays the server's call: resolving the amount ourselves would
            # substitute our clock for the one that owns the data.
            raise OlogTimeFormatError(
                f"start ({start!r}) is after end ({end!r}) — the window is empty. "
                f"Swap them, or drop end to search up to now."
            )

    def search_logbook(
        self,
        text: str | None = None,
        logbooks: str | None = None,
        tags: str | None = None,
        start: str | None = None,
        end: str | None = None,
        size: int = DEFAULT_MAX_LOGS,
        offset: int = 0,
        sort: str = "down",
    ) -> tuple[list[dict[str, object]], bool, int | None]:
        """Search log entries → ``(entries, capped, total_matches)``; see :meth:`_project`.

        *text* searches the description; *logbooks*/*tags* are comma-separated names; *start*/*end*
        bound the time window. *offset* is the 0-based pagination offset (Olog wire param ``from``;
        ``from`` is a Python keyword, hence the ``offset`` name — read past the first page).
        *sort* orders by create time: ``down`` (default) = newest first, ``up`` = oldest first.

        ``capped`` is True when more than *size* matched on this page (one extra is requested to
        detect it honestly, the Archiver/ChannelFinder pattern). ``total_matches`` is the true total
        across all pages (Olog ``hitCount``); it is ``None`` only when the Olog version returns a
        bare list with no count — ``capped`` then still signals honestly whether more matched (no
        fabricated total).
        """
        params: dict[str, str] = {"size": str(size + 1), "sort": sort}
        if text:
            params["desc"] = text
        if logbooks:
            params["logbooks"] = logbooks
        if tags:
            params["tags"] = tags
        self._add_window(params, start, end)
        if offset > 0:
            params["from"] = str(offset)
        try:
            data = self._get(f"{self.base_url}/logs/search", params)
        except OlogResponseError as exc:
            # Chain from the ORIGINAL transport cause so http_status still reads the served code
            # downstream (mirrors create_log_entry). 5xx / anything else propagates as-is.
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            if http_status(exc) == 401:
                # Measured: Olog answers 401 to an ANONYMOUS caller for every server-side 400 —
                # its error dispatch requires auth and so masks its own rejection. This read path
                # IS anonymous, making 401 the reachable branch; blaming credentials would send
                # the reader after the wrong problem entirely.
                raise OlogResponseError(
                    "Olog rejected the search (HTTP 401). On this anonymous read path that "
                    "almost always means the QUERY was rejected, not that credentials are "
                    "wrong: Olog's error dispatch requires authentication and returns 401 in "
                    "place of its own 400. Check the time window (start/end) first."
                ) from cause
            if is_http_400(exc):
                raise OlogResponseError(
                    "Olog rejected the search (HTTP 400): most likely the time window — start "
                    "must not be after end, and with no end Olog compares against now, so a "
                    "start in the future is rejected."
                ) from cause
            raise
        entries = _entries_of(data)
        capped = len(entries) > size
        total_matches = _hit_count(data)
        projected = [self._project(e) for e in entries[:size] if isinstance(e, dict)]
        return projected, capped, total_matches

    def get_log_entry(self, log_id: str) -> dict[str, object] | None:
        """Return one log entry by id (DS-PRIVACY-redacted), or ``None`` when it does not exist.

        A missing/deleted id makes the Olog answer HTTP 404, which is the definitive "not found"
        (mapped to ``None``); every OTHER failure (5xx, bad payload, an unreachable service)
        PROPAGATES, so a could-not-read never masquerades as not-found. Mirrors the archiver
        ``getPVTypeInfo`` handling.
        """
        try:
            data = self._get(f"{self.base_url}/logs/{log_id}", {})
        except OlogResponseError as exc:
            if is_http_404(exc):
                return None
            raise
        if isinstance(data, dict) and data:
            return self._project(data)
        return None

    def list_logbooks(self) -> list[str]:
        """Return the names of all Olog logbooks (``GET /logbooks``), name-only.

        DS-PRIVACY: each ``Logbook`` carries an ``owner`` (PII); :func:`_names` keeps only the
        ``name``, so no owner reaches the caller. The names are the valid filter values for
        ``search_logbook(logbooks=…)``.
        """
        data = self._get(f"{self.base_url}/logbooks", {})
        return _names(data)

    def list_tags(self) -> list[str]:
        """Return the names of all Olog tags (``GET /tags``), name-only.

        A ``Tag`` has only ``name``/``state`` (no owner), so this is trivially PII-free. The names
        are the valid filter values for ``search_logbook(tags=…)``.
        """
        data = self._get(f"{self.base_url}/tags", {})
        return _names(data)

    def create_log_entry(
        self,
        title: str,
        logbooks: list[str],
        description: str | None = None,
        level: str | None = None,
        tags: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, object]:
        """Create a log entry (``PUT /logs``) and return it DS-PRIVACY-redacted.

        *title* and *logbooks* are mandatory server-side (empty → HTTP 400); the named logbooks and
        tags MUST already exist (else 400). *in_reply_to* (wire query ``inReplyTo``) threads the new
        entry as a reply to an existing entry (a bad id → 400, checked server-side BEFORE the
        logbook validation). Auth rides on the session (Basic, see :func:`basic_auth_header`); the
        server sets ``owner`` from the auth Principal and IGNORES a caller-supplied owner, so this
        client never sends one.

        The server's create response is a FULL log (with owner/free text) → it leaves this module
        through the SAME :meth:`_project` as a read, so write and read never disagree about what is
        visible: redacted against a real server, whole against a loopback sandbox (where seeing it
        is the point — a write can then verify what it wrote instead of needing an outside client).
        HTTP 400 → a clear :class:`OlogResponseError` (a named logbook/tag does not exist, an empty
        title, or a bad ``inReplyTo`` — explicitly NOT "not found"); 401 → an auth error; 5xx
        propagates."""
        body: dict[str, object] = {
            "title": title,
            "logbooks": [{"name": name} for name in logbooks],
            # Always send a present description string (empty when the caller gave none): Olog's
            # save path dereferences description WITHOUT a null-guard (unlike source), so a missing
            # description 500s the server (NPE). A complete payload keeps the write robust.
            # Verified live (loopback phoebus-olog: a null description NPEs, an empty one succeeds).
            "description": description if description is not None else "",
        }
        if level is not None:
            body["level"] = level
        if tags:
            body["tags"] = [{"name": name} for name in tags]
        params: dict[str, str] = {}
        if in_reply_to is not None:
            params["inReplyTo"] = str(in_reply_to)
        try:
            data = rest_put_json(
                self.session,
                f"{self.base_url}/logs",
                body,
                self.timeout,
                params=params or None,
                headers={"X-Olog-Client-Info": _CLIENT_INFO},
                conn_exc=OlogConnectionError,
                resp_exc=OlogResponseError,
                # Never follow a redirect on a write: the gate approved THIS host, and a hop would
                # carry the body and the Basic auth header somewhere it never inspected.
                allow_redirects=False,
            )
        except OlogResponseError as exc:
            # Re-raise a clearer message for the two actionable statuses, chaining from the ORIGINAL
            # transport cause (exc.__cause__, the requests HTTPError) so http_status still reads the
            # served code downstream (the audit error_code). 5xx / anything else propagates as-is.
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            if is_http_400(exc):
                raise OlogResponseError(
                    "Olog rejected the entry (HTTP 400): a logbook or tag does not exist, the "
                    "title is empty, or in_reply_to does not identify an existing entry."
                ) from cause
            if http_status(exc) == 401:
                raise OlogResponseError(
                    "Olog authentication failed (HTTP 401): check the write service-account "
                    "credentials (EPICS_MCP_OLOG_WRITE_USER / _PASSWORD)."
                ) from cause
            raise
        if isinstance(data, dict) and data:
            return self._project(data)
        raise OlogResponseError("Olog create returned an unexpected empty response.")
