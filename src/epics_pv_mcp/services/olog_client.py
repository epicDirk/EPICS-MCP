"""Read-only client for the Phoebus Olog (electronic logbook) REST API.

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

from epics_pv_mcp.services._http import build_retrying_session, is_http_404, rest_get_json
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError, OlogResponseError
from epics_pv_mcp.services.redact import redact_record

# Default cap on returned log entries (a wide/empty search is otherwise unbounded).
DEFAULT_MAX_LOGS = 50

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
    """Read-only client for the Phoebus Olog REST API. GET-only, DS-PRIVACY-redacted output."""

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
