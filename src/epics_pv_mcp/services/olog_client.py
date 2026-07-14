"""Client for the Phoebus Olog (electronic logbook) REST API — read plus one gated write path.

The reads are default-disabled and DS-PRIVACY-redacted; the single write
(:meth:`OlogClient.create_log_entry`, ``PUT /logs``) is gated separately by
:mod:`epics_pv_mcp.olog_safety` (this module only carries the transport) and its response is run
through the SAME output redaction as a read.

Read-only jobs:

  GET {root}/logs/search?desc=…&logbooks=…&tags=…&start=…&end=…&size=…&from=…&sort=…  — search
  GET {root}/logs/{id}                                                  — one log entry
  GET {root}/logbooks                                                   — list logbook names
  GET {root}/tags                                                       — list tag names

``olog_url`` is the Olog REST root incl. context path (e.g. ``http://olog:8080/Olog``); the
``/logs`` paths are appended. Reading needs no auth by default; an optional ``Authorization``
header is forwarded for secured deployments. Structure mirrors
:mod:`epics_pv_mcp.services.alarm_client`.

⛔ **DS-PRIVACY (HARD).** Olog entries carry person data not only in ``owner`` but throughout FREE
TEXT — the title, the description/body, attachment filenames, and per-item owners inside the
``logbooks``/``tags`` structures. A key-based strip is therefore NOT sufficient. Every entry is run
through a strict OUTPUT ALLOWLIST (:func:`_project_log_entry`): technical fields
(``id``/dates/``level``/``state``) are kept; ``logbooks``/``tags`` are reshaped to name-only lists
(their per-item owners dropped); ``title``/``description`` keep their key but their value is
WITHHELD (a name inside the text cannot leak); attachments are surfaced only as a COUNT; and
``owner``/``source``/``properties`` (author-written) are dropped entirely. Raw entries never leave
this module.

⚠️ Search-param names (``desc``/``logbooks``/``tags``/``start``/``end``/``size``) and the entry
field names follow the documented Phoebus Olog model but are best-effort until the ESS live-smoke —
the redaction is defence-in-depth regardless of which extra fields a given Olog version returns.
"""

from __future__ import annotations

from epics_pv_mcp.services._http import (
    build_retrying_session,
    http_status,
    is_http_400,
    is_http_404,
    rest_get_json,
    rest_put_json,
)
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError, OlogResponseError
from epics_pv_mcp.services.redact import redact_record

# Default cap on returned log entries (a wide/empty search is otherwise unbounded).
DEFAULT_MAX_LOGS = 50

# Static, non-identifying client-info header value (server-side only logged, never persisted). NOT a
# personal username/hostname — a facility-agnostic constant.
_CLIENT_INFO = "epics-pv-mcp"

# DS-PRIVACY output allowlist: the ONLY entry fields that may leave. ``title``/``description`` are
# kept for their PRESENCE but their value is withheld (they are free text). Everything not listed —
# ``owner``, ``source``, ``properties``, raw ``attachments``, and any field a future Olog version
# adds — is dropped by the allowlist, so a new person-bearing field never leaks by default.
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


def _project_log_entry(entry: dict[str, object]) -> dict[str, object]:
    """Return *entry* reduced to the DS-PRIVACY allowlist (see the module docstring).

    Technical fields kept; ``logbooks``/``tags`` reshaped to name-only lists; ``title``/
    ``description`` withheld; attachments surfaced as a count only; ``owner``/``source``/
    ``properties`` dropped. The single privacy barrier for every Olog entry that leaves this module.
    """
    projected = redact_record(entry, allowed=_LOG_ALLOWLIST, freetext=_LOG_FREETEXT)
    projected["logbooks"] = _names(entry.get("logbooks"))
    projected["tags"] = _names(entry.get("tags"))
    attachments = entry.get("attachments")
    projected["attachment_count"] = len(attachments) if isinstance(attachments, list) else 0
    return projected


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
    PUT /logs) is gated by olog_safety. DS-PRIVACY-redacted output on every path (read + write)."""

    def __init__(self, base_url: str, timeout: float = 5.0, auth_header: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = build_retrying_session(auth_header=auth_header)

    def _get(self, url: str, params: dict[str, str]) -> object:
        """Issue a GET and return parsed JSON, translating failures to Olog exceptions."""
        return rest_get_json(
            self.session,
            url,
            params,
            self.timeout,
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
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
        """Search log entries → ``(entries, capped, total_matches)`` — DS-PRIVACY-redacted.

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
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if offset > 0:
            params["from"] = str(offset)
        data = self._get(f"{self.base_url}/logs/search", params)
        entries = _entries_of(data)
        capped = len(entries) > size
        total_matches = _hit_count(data)
        projected = [_project_log_entry(e) for e in entries[:size] if isinstance(e, dict)]
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
            return _project_log_entry(data)
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

        The server's create response is a FULL log (with owner/free text) → it is run through the
        SAME :func:`_project_log_entry` redaction as a read before it leaves this module (owner
        dropped, title/description withheld, attachments as a count). HTTP 400 → a clear
        :class:`OlogResponseError` (a named logbook/tag does not exist, an empty title, or a bad
        ``inReplyTo`` — explicitly NOT "not found"); 401 → an auth error; 5xx propagates."""
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
            return _project_log_entry(data)
        raise OlogResponseError("Olog create returned an unexpected empty response.")
