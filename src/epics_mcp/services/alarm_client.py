"""Read-only client for the Phoebus Alarm Logger REST API.

One read-only job, the Wedge-3 **coverage** signal:

  GET {root}/search/alarm/config?config=/{ConfigName}/*{pv}, is *pv* in the alarm config tree?

The Alarm Logger exposes a typed REST layer over its Elasticsearch indices. The ``config`` param is
mandatory and its FIRST path segment (after a leading slash) selects the ES index, the server does
``config.split("/")[1].toLowerCase()`` (verified against the upstream
``AlarmLogSearchUtil.searchConfig``), then matches the ``config`` field as a wildcard substring
(``*<config>*``). A bare PV name (no leading slash) raises HTTP 500 there, so we ALWAYS build the
path ``/{ConfigName}/*{pv}`` (the ``*`` spans any component nesting between root and the PV).

⚠ ``/search/alarm/config`` is a config-CHANGE log (one ES doc per change): a HIT proves the PV is
configured; a MISS is only trustworthy if the Alarm Logger was running when the tree was imported
(else the change never reached ES). Callers should treat a miss as a real negative only under that
precondition and otherwise as withheld.

``alarm_url`` is the logger REST root (e.g. ``http://localhost:8081``). Queries need no
authentication by default; an optional ``Authorization`` header is forwarded for secured
deployments. Structure mirrors :mod:`epics_mcp.services.archiver_client` (GET-only,
Session + Retry on 502/503/504, per-service exceptions).
"""

from __future__ import annotations

from epics_mcp.services._http import get_shared_session, rest_get_json
from epics_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmResponseError
from epics_mcp.services.alarm_time import normalize_alarm_time
from epics_mcp.services.redact import project_allowlist, redact_record

# MA-2b(d): there is deliberately NO default alarm-tree name. The former ``"Accelerator"`` default
# matched nothing at a real facility (the tree names are site-specific, DTL/BIS/FBIS/..., and a
# committed default cannot name one without violating the facility-agnostic guardrail), so it made
# ``is_alarm_configured`` silently withhold. The caller MUST name the tree (the leading path segment
# that selects the ES index).

# DS-PRIVACY: a Phoebus alarm config-CHANGE document carries ``user`` and ``host``, WHO last
# changed the alarm config, alongside the technical settings, AND its authored free-text fields
# (guidance/displays/commands/actions/description) can name a person in prose (e.g. "call Jane Doe")
# or a ``mailto:`` notification action. Returning the raw ES record would leak a person's username;
# keeping the free-text VALUES would leak a name inside them. We therefore ``redact_record`` the
# doc: project onto this allowlist of technical fields (an allowlist, not a denylist, a NEW
# person-bearing field a future logger version adds is dropped by default), THEN withhold the
# authored free-text VALUES (:data:`_ALARM_CONFIG_FREETEXT`). ``user``/``host`` are not on the
# allowlist, so they are gone entirely.
_ALARM_CONFIG_ALLOWLIST = frozenset(
    {
        "config",
        "pv",
        "enabled",
        "latching",
        "annunciating",
        "delay",
        "count",
        "filter",
        "description",
        "guidance",
        "displays",
        "commands",
        "actions",
        "message_time",
        "time",
    }
)

# DS-PRIVACY: the allowlist fields whose VALUE is AUTHORED free text, a person can be named inside
# them (guidance prose "call Jane Doe", an ``actions`` ``mailto:jane.doe@...``). Kept for their
# PRESENCE but their value is withheld (the Olog free-text treatment). This resolves the former
# accepted Batch-1 residual after the pre-live-smoke privacy audit exhibited the leak against the
# non-negotiable "no person data" guardrail (project decision, 2026-07: withhold all five).
_ALARM_CONFIG_FREETEXT = frozenset({"description", "guidance", "displays", "commands", "actions"})


def _project_alarm_config(record: dict[str, object]) -> dict[str, object]:
    """Return *record* reduced to the technical allowlist with authored free-text values withheld.

    Uses the shared DS-PRIVACY :func:`~epics_mcp.services.redact.redact_record` barrier: it drops
    ``user``/``host``/unknown (allowlist) and then replaces the authored free-text VALUES
    (:data:`_ALARM_CONFIG_FREETEXT`, guidance/actions/commands/description/displays) with a
    withheld marker, so a person named in prose or a ``mailto:`` action cannot leak (the Olog
    treatment). The key is kept, so a caller still learns the field is present.
    """
    return redact_record(record, allowed=_ALARM_CONFIG_ALLOWLIST, freetext=_ALARM_CONFIG_FREETEXT)


# DS-PRIVACY (DS-3): an alarm STATE/history document (``/search/alarm``) can carry ``user`` and
# ``host``, WHO acknowledged / enabled / disabled the alarm, plus a ``command`` (the action taken)
# and a ``config_msg`` (potentially authored). Returning the raw ES record would leak a person's
# username. We project onto this allowlist of technical alarm fields (matching the AlarmLogMessage
# model in the Phoebus alarm-logger). An allowlist (not a denylist) means a NEW person-bearing field
# a future logger version adds is dropped by default. ``pv``/``config`` are kept so the caller can
# SEE which PV each event belongs to (the ``pv`` query matches a substring of the config path, so
# results can include sibling PVs, keeping the identity makes that transparent, not hidden).
_ALARM_HISTORY_ALLOWLIST = frozenset(
    {
        "severity",
        "message",
        "value",
        "time",
        "current_severity",
        "current_message",
        "enabled",
        "mode",
        "message_time",
        "pv",
        "config",
    }
)


def _project_alarm_event(record: dict[str, object]) -> dict[str, object]:
    """Return *record* restricted to the technical allowlist (drops ``user``/``host``/``command``/
    ``config_msg``/unknown) via the shared ``redact.project_allowlist`` barrier."""
    return project_allowlist(record, _ALARM_HISTORY_ALLOWLIST)


def _require_alarm_records(data: object, endpoint: str) -> list[dict[str, object]]:
    """The measured ``/search/alarm*`` payload: a list of docs, each a dict with a string
    ``config`` (S11).

    ``config`` is the identity field this client reads (config docs carry no ``pv``), and it is
    the one field BOTH measured doc types (``state:`` and ``config:``) always carry, the schema
    anchor. Anything else used to collapse into a definitive answer: a non-list payload read as
    ``[]``, a junk record was silently dropped (auditor probe ALARM_HISTORY_BAD_2XX →
    ``([], False)``; plan-review finding A1 → a definitive ``False`` from ``[123]``).
    """
    if not isinstance(data, list):
        raise AlarmResponseError(
            f"Alarm Logger {endpoint} returned an unreadable payload "
            f"(expected a list, got {type(data).__name__}); the answer is not readable."
        )
    records: list[dict[str, object]] = []
    for record in data:
        if not isinstance(record, dict) or not isinstance(record.get("config"), str):
            raise AlarmResponseError(
                f"Alarm Logger {endpoint} returned an unreadable record "
                f"(expected a dict carrying a string 'config', got {type(record).__name__})."
            )
        records.append(record)
    return records


class AlarmClient:
    """Read-only client for the Phoebus Alarm Logger REST API. GET-only."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        auth_header: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = get_shared_session(auth_header=auth_header)

    def _get(self, url: str, params: dict[str, str]) -> object:
        """Issue a GET and return parsed JSON, translating failures to Alarm client exceptions."""
        return rest_get_json(
            self.session,
            url,
            params,
            self.timeout,
            conn_exc=AlarmConnectionError,
            resp_exc=AlarmResponseError,
        )

    def check_connectivity(self) -> bool:
        """Return True if the Alarm Logger is reachable; raise AlarmConnectionError otherwise.

        A HEAD to the service root proves transport + TLS (the CA bundle), any HTTP response counts
        as reachable (status irrelevant, as in the Naming client). A transport/TLS failure re-raises
        as AlarmConnectionError with the original requests error as ``__cause__``, so a caller can
        inspect it (:func:`is_ssl_error`) to tell a CA problem from a plain unreachable host.
        """
        try:
            self.session.head(self.base_url, timeout=self.timeout)
            return True
        except OSError as exc:
            # requests.exceptions.RequestException ⊂ OSError (see naming_client.check_connectivity).
            raise AlarmConnectionError(
                f"Failed to connect to Alarm Logger at {self.base_url}: {exc}"
            ) from exc

    def is_alarm_configured(
        self, pv: str, config_name: str
    ) -> tuple[bool | None, dict[str, object]]:
        """Return ``(configured, detail)``: True iff alarm tree *config_name* contains *pv*.

        ``configured`` is **None when the answer is withheld**: the tree itself produced nothing, so
        "not configured" cannot be told apart from "wrong tree name" (see below). Never ``False`` in
        that case, the ``withheld != no`` rule.

        Queries ``/search/alarm/config`` with ``config=/{config_name}/*{pv}`` (leading slash +
        config name select the ES index; the ``*`` spans component nesting). The config-change index
        documents carry **no** ``pv`` field, identity is the LAST path segment of the ``config``
        field (stored as ``config:/{tree}/{components}/{pv}``); we compare that leaf to *pv* to
        reject a substring over-match. NOTE: the server caps the result at the single most-recent
        matching record (``size=1``, ``message_time`` DESC), so a sibling PV whose name strictly
        contains *pv* and changed more recently could mask a real hit, a known backend limitation,
        harmless for the distinct sandbox device set.

        WHY THE TREE PROBE (measured live 2026-07-15, not inferred): *config_name* is honoured by
        the server in two different ways, and a mismatch between them is silent. It is lower-cased
        to pick the ES index, but goes into the ``wildcard`` query CASE-PRESERVED against a
        ``keyword`` field (``AlarmLogSearchUtil.java:305-313``), so ``"accelerator"`` selects the
        RIGHT index and matches NO document. An unknown tree is equally quiet: the index pattern
        ends in ``*``, so Elasticsearch answers 200 + ``[]`` instead of ``index_not_found``.
        Measured: ``/Accelerator/*Temp1Value`` → 1 record, while ``/accelerator/*Temp1Value``,
        ``/Nonexistent/*Temp1Value`` and a genuinely unconfigured PV ALL → ``[]``. Re-asking for
        the bare tree (``/{config_name}/*``) separates them, it returns a record iff the tree name
        was read as intended, so only the last of those four stays a real ``False``.

        The returned ``detail`` is the config record reduced by :func:`_project_alarm_config`, the
        raw ES record's ``user``/``host`` (who changed the config) are dropped and the authored
        free-text values (guidance/actions/commands/description/displays) are withheld, so no person
        can leak in the audit metadata OR inside the prose (DS-PRIVACY).
        """
        config_query = f"/{config_name}/*{pv}"
        data = self._get(f"{self.base_url}/search/alarm/config", {"config": config_query})
        # S11: unreadable must never become a definitive answer. A non-list payload used to read
        # as [] (a miss), and junk records inside a list were silently dropped, either way an
        # answering tree then turned "could not read" into a definitive False.
        records = _require_alarm_records(data, "/search/alarm/config")
        for record in records:
            # Config docs have NO `pv` field → identity = leaf segment of the `config` path
            # (the validated string anchor from _require_alarm_records).
            leaf = str(record["config"]).rsplit("/", 1)[-1]
            if leaf == pv:
                return True, _project_alarm_config(record)
        # A miss is only a real negative if the tree answered at all, one extra request, and only
        # on the miss path (a hit above already proved the tree).
        if not self._alarm_tree_answers(config_name):
            return None, {}
        return False, {}

    def _alarm_tree_answers(self, config_name: str) -> bool:
        """Return True iff alarm tree *config_name* yields a READABLE config record.

        Distinguishes a real "PV not configured" from an unknown/misspelled/mis-cased tree name,
        which the server reports identically (200 + empty). See :meth:`is_alarm_configured`.
        S11: only a readable record (a dict with a string ``config``) counts as proof, junk is
        no evidence the tree name was read as intended, so a junk-only answer stays "not
        answered" and the miss above stays withheld (None), never a definitive False.
        """
        data = self._get(f"{self.base_url}/search/alarm/config", {"config": f"/{config_name}/*"})
        if not isinstance(data, list):
            return False
        return any(
            isinstance(record, dict) and isinstance(record.get("config"), str) for record in data
        )

    def get_alarm_history(
        self,
        pv: str,
        start: str,
        end: str,
        max_events: int = 100,
        *,
        root: str | None = None,
        command: str | None = None,
        severity: str | None = None,
        current_severity: str | None = None,
    ) -> tuple[list[dict[str, object]], bool]:
        """Return ``(events, capped)``, the alarm state/history of *pv* over ``[start, end]``.

        Queries ``/search/alarm`` (sibling of the config query above). The server matches ``pv`` as
        a wildcard substring on the alarm ``config`` path, applies the ``message_time`` range
        ``[start, end]`` and returns the newest ``size`` records first (``message_time`` DESC).
        *start* and *end* are REQUIRED (a defaultless query must never pull the whole history); each
        accepts an absolute time (ISO-8601) or a relative amount (e.g. ``"8 hours"``). Both are
        normalized by :func:`~epics_mcp.services.alarm_time.normalize_alarm_time` FIRST, the
        server's ``TimeParser`` reads ISO only WITH a zone and silently takes anything unreadable
        as *now*, answering 200 with an empty list rather than an error (measured live; see that
        module). NOTE: without an index/root restriction ``/search/alarm``
        returns alarm STATE-change AND alarm CONFIG-change documents; the ``config`` field prefix
        (``state:`` vs ``config:``) distinguishes them and is kept in the projected event.

        We request ``size = max_events + 1`` so ``capped`` is an honest ``fetched > max_events``
        (the query_channels pattern) rather than a ``>=`` that false-flags exactly ``max_events``
        real events; the newest ``max_events`` are kept. Each event is projected onto the technical
        allowlist (:data:`_ALARM_HISTORY_ALLOWLIST`), the raw ES doc's ``user``/``host`` (who
        acknowledged/enabled/disabled) and ``command``/``config_msg`` never leave this method
        (DS-PRIVACY).

        NOTE (backend limitation, mirrors ``is_alarm_configured``'s size cap): the server clamps
        the result to ``min(es_max_size, size)`` (``es_max_size`` default 1000). If ``es_max_size``
        is at or below ``max_events`` the ``+1`` probe record cannot come back, so a full window of
        exactly ``max_events`` events cannot be told apart from a truncated one and ``capped`` may
        under-report. The ``get_alarm_history`` tool caps ``max_events`` at 999 so this cannot
        happen at the DEFAULT ``es_max_size``; only a backend configured with a lower
        ``es_max_size`` is exposed.
        """
        params = {
            "pv": pv,
            "start": normalize_alarm_time(start, param="start"),
            "end": normalize_alarm_time(end, param="end"),
            "size": str(max_events + 1),
        }
        # MA-2b(a/b/c): optional server-side filters (AlarmLogSearchUtil). Added ONLY when set, so
        # the unset default preserves the all-trees / all-severity search. ⚠ SERVER-DECIDED: the
        # logger SILENTLY IGNORES an unsupported query param (its switch default is a bare
        # ``break;``), an older logger BROADENS the result instead of erroring, so whether the
        # running server honours these is a live-probe question, not a promise. root selects the
        # config tree(s); severity/current_severity match the alarm severity fields; command maps to
        # the ``enabled`` field (Enabled→true / Disabled→false).
        if root is not None:
            params["root"] = root
        if command is not None:
            params["command"] = command
        if severity is not None:
            params["severity"] = severity
        if current_severity is not None:
            params["current_severity"] = current_severity
        data = self._get(f"{self.base_url}/search/alarm", params)
        # S11: unreadable used to read as ([], False), "no alarms in the window", and junk
        # records were silently dropped (a fabricated, smaller history). Both raise now.
        records = _require_alarm_records(data, "/search/alarm")
        # QA(2026-07-22): capture the server-side truncation signal (the ``size = max_events + 1``
        # over-fetch) BEFORE any client-side filtering below consumes it. Otherwise the config:
        # filter could drop the +1 probe record and flip ``capped`` to False on a genuinely
        # truncated window, a silent false-completeness breaking the honest-``capped`` invariant.
        server_truncated = len(records) > max_events
        if command is not None:
            # MA-2b(b): ``command`` maps server-side to the ``enabled`` field, which is set on BOTH
            # state: and config: docs (a state-change doc carries enabled=false intrinsically).
            # Restrict to config: docs so "which alarm CONFIGS were enabled/disabled" is not swamped
            # by state-change events, the config-path prefix distinguishes them. A client-side
            # correctness guard, independent of whether the server honoured the param. NOTE: with a
            # truncated window (capped) the config: docs among the newest page can be crowded out by
            # state: docs, capped stays True to flag it; raise max_events or narrow the window.
            records = [record for record in records if str(record["config"]).startswith("config:")]
        capped = server_truncated or len(records) > max_events
        events = [_project_alarm_event(record) for record in records[:max_events]]
        return events, capped
