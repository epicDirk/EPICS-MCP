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

import logging

import requests

from epics_pv_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmResponseError

logger = logging.getLogger(__name__)

# Default alarm config-tree (topic) name; the leading path segment that selects the ES index.
DEFAULT_ALARM_CONFIG = "Accelerator"


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
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json"})
        if auth_header:
            self.session.headers.update({"authorization": auth_header})

        from requests.adapters import HTTPAdapter

        try:
            from urllib3.util.retry import Retry

            retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        except ImportError:
            pass  # urllib3 retry unavailable — proceed without

    def _get(self, url: str, params: dict[str, str]) -> object:
        """Issue a GET and return parsed JSON, translating failures to client exceptions."""
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as exc:
            raise AlarmConnectionError(
                f"Failed to connect to Alarm Logger at {self.base_url}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise AlarmResponseError(f"Alarm Logger request failed ({url}): {exc}") from exc

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
                return True, record
        return False, {}
