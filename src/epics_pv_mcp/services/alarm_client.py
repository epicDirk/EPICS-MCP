"""Read-only client for the Phoebus Alarm Logger REST API.

One read-only job — the Wedge-3 **coverage** signal:

  GET {root}/search/alarm/config?config=/{ConfigName}/*{pv}   — is *pv* in the alarm config tree?

The Alarm Logger exposes a typed REST layer over its Elasticsearch indices. The ``config`` param is
mandatory and its FIRST path segment (after a leading slash) selects the ES index — the server does
``config.split("/")[1].toLowerCase()`` (verified against the upstream
``AlarmLogSearchUtil.searchConfig``), then matches the ``config`` field as a wildcard substring
(``*<config>*``). A bare PV name (no leading slash) raises HTTP 500 there — so we ALWAYS build the
path ``/{ConfigName}/*{pv}`` (the ``*`` spans any component nesting between root and the PV).

⚠ ``/search/alarm/config`` is a config-CHANGE log (one ES doc per change): a HIT proves the PV is
configured; a MISS is only trustworthy if the Alarm Logger was running when the tree was imported
(else the change never reached ES). Callers should treat a miss as a real negative only under that
precondition and otherwise as withheld.

``alarm_url`` is the logger REST root (e.g. ``http://localhost:8081``). Queries need no
authentication by default; an optional ``Authorization`` header is forwarded for secured
deployments. Structure mirrors :mod:`epics_pv_mcp.services.archiver_client` (GET-only,
Session + Retry on 502/503/504, per-service exceptions).
"""

from __future__ import annotations

from epics_pv_mcp.services._http import build_retrying_session, rest_get_json
from epics_pv_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmResponseError

# Default alarm config-tree (topic) name; the leading path segment that selects the ES index.
DEFAULT_ALARM_CONFIG = "Accelerator"

# DS-PRIVACY: a Phoebus alarm config-CHANGE document carries ``user`` and ``host`` — WHO last
# changed the alarm config — alongside the technical settings. Returning the raw ES record would
# leak a person's username. We project onto this allowlist of technical fields instead. An allowlist
# (not a denylist) means a NEW person-bearing field a future logger version adds cannot leak
# silently — an unknown field is dropped by default. The alarm's technical configuration
# (enable/latch/delay, operator guidance/commands) is preserved; ``user``/``host`` are not on the
# list, so they are gone.
# RESIDUAL (Batch-1 accepted): guidance/displays/commands/actions/description are AUTHORED free text
# and could embed a person's name in prose (e.g. "call Jane Doe"). Structurally removing the auto
# audit metadata (user/host) is the Batch-1 guard; general free-text redaction is deferred to the
# Batch-3/DS-5 redactor (same class as Olog free text).
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


def _project_alarm_config(record: dict[str, object]) -> dict[str, object]:
    """Return *record* restricted to the technical allowlist (drops ``user``/``host``/unknown)."""
    return {key: value for key, value in record.items() if key in _ALARM_CONFIG_ALLOWLIST}


# DS-PRIVACY (DS-3): an alarm STATE/history document (``/search/alarm``) can carry ``user`` and
# ``host`` — WHO acknowledged / enabled / disabled the alarm — plus a ``command`` (the action taken)
# and a ``config_msg`` (potentially authored). Returning the raw ES record would leak a person's
# username. We project onto this allowlist of technical alarm fields (matching the AlarmLogMessage
# model in the Phoebus alarm-logger). An allowlist (not a denylist) means a NEW person-bearing field
# a future logger version adds is dropped by default. ``pv``/``config`` are kept so the caller can
# SEE which PV each event belongs to (the ``pv`` query matches a substring of the config path, so
# results can include sibling PVs — keeping the identity makes that transparent, not hidden).
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
    ``config_msg``/unknown)."""
    return {key: value for key, value in record.items() if key in _ALARM_HISTORY_ALLOWLIST}


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
        self.session = build_retrying_session(auth_header=auth_header)

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

    def is_alarm_configured(
        self, pv: str, config_name: str = DEFAULT_ALARM_CONFIG
    ) -> tuple[bool, dict[str, object]]:
        """Return ``(configured, detail)`` — True iff alarm tree *config_name* contains *pv*.

        Queries ``/search/alarm/config`` with ``config=/{config_name}/*{pv}`` (leading slash +
        config name select the ES index; the ``*`` spans component nesting). The config-change index
        documents carry **no** ``pv`` field — identity is the LAST path segment of the ``config``
        field (stored as ``config:/{tree}/{components}/{pv}``); we compare that leaf to *pv* to
        reject a substring over-match. NOTE: the server caps the result at the single most-recent
        matching record (``size=1``, ``message_time`` DESC), so a sibling PV whose name strictly
        contains *pv* and changed more recently could mask a real hit — a known backend limitation,
        harmless for the distinct sandbox device set.

        The returned ``detail`` is the config record projected onto the technical allowlist
        (:data:`_ALARM_CONFIG_ALLOWLIST`) — the raw ES record's ``user``/``host`` (who changed the
        config) never leave this method (DS-PRIVACY).
        """
        config_query = f"/{config_name}/*{pv}"
        data = self._get(f"{self.base_url}/search/alarm/config", {"config": config_query})
        records = data if isinstance(data, list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            # Config docs have NO `pv` field → identity = leaf segment of the `config` path.
            leaf = str(record.get("config", "")).rsplit("/", 1)[-1]
            if leaf == pv:
                return True, _project_alarm_config(record)
        return False, {}

    def get_alarm_history(
        self, pv: str, start: str, end: str, max_events: int = 100
    ) -> tuple[list[dict[str, object]], bool]:
        """Return ``(events, capped)`` — the alarm state/history of *pv* over ``[start, end]``.

        Queries ``/search/alarm`` (sibling of the config query above). The server matches ``pv`` as
        a wildcard substring on the alarm ``config`` path, applies the ``message_time`` range
        ``[start, end]`` and returns the newest ``size`` records first (``message_time`` DESC).
        *start* and *end* are REQUIRED (a defaultless query must never pull the whole history); each
        accepts an absolute time (ISO-8601) or a relative amount (e.g. ``"8 hours"``) — the server's
        ``TimeParser`` handles both. NOTE: without an index/root restriction ``/search/alarm``
        returns alarm STATE-change AND alarm CONFIG-change documents; the ``config`` field prefix
        (``state:`` vs ``config:``) distinguishes them and is kept in the projected event.

        We request ``size = max_events + 1`` so ``capped`` is an honest ``fetched > max_events``
        (the query_channels pattern) rather than a ``>=`` that false-flags exactly ``max_events``
        real events; the newest ``max_events`` are kept. Each event is projected onto the technical
        allowlist (:data:`_ALARM_HISTORY_ALLOWLIST`) — the raw ES doc's ``user``/``host`` (who
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
        params = {"pv": pv, "start": start, "end": end, "size": str(max_events + 1)}
        data = self._get(f"{self.base_url}/search/alarm", params)
        records = data if isinstance(data, list) else []
        capped = len(records) > max_events
        events = [
            _project_alarm_event(record)
            for record in records[:max_events]
            if isinstance(record, dict)
        ]
        return events, capped
