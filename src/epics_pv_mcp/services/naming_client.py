"""Client for the ESS Naming Service REST API (read-only).

Vendored and slimmed from pvValidator's ``pvValidatorUtils/naming_client.py`` (Alfio
Rizzo, ESS) so this repo stays standalone — pvValidator itself is Linux/SWIG-only and
cannot be imported on Windows, but its Naming-Service client is pure Python (``requests``
+ stdlib). The "Did you mean?"/confusable helpers (which pull in pvValidator's ``rules``)
are intentionally dropped; only the read-only validation calls the cross-plane check needs
are kept. Endpoints (all GET):

  GET /rest/parts/mnemonic/{mnemonic}  — validate System / Subsystem / Discipline / Device
  GET /rest/deviceNames/{name}         — check if an ESS device name is registered + status
"""

from __future__ import annotations

from typing import TypedDict
from urllib.parse import quote as url_quote

import requests

from epics_pv_mcp.services._http import build_retrying_session
from epics_pv_mcp.services.naming_exceptions import (
    NamingServiceConnectionError,
    NamingServiceNotFound,
    NamingServiceResponseError,
)


class NameStatus(TypedDict):
    """Result of :meth:`NamingServiceClient.validate_name`."""

    registered: bool
    status: str
    message: str


class NamingServiceClient:
    """Read-only client for the ESS Naming Service REST API.

    All methods issue ``GET`` requests only — nothing is ever written to the service.
    Results are cached in-memory for the lifetime of the instance.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
    ) -> None:
        # ``base_url`` is REQUIRED and caller-provided (from ``EPICS_MCP_NAMING_URL`` via config) —
        # there is deliberately NO built-in default host, so this client never reaches a hard-coded
        # ESS endpoint. Callers gate on an unset URL (see checkers.build_naming_client + diagnose).
        # Normalise like the other three REST clients (channelfinder/archiver/alarm): strip a
        # trailing slash so a URL configured with OR without it yields the same endpoints (M10;
        # without this, ``http://naming:8080/enotify-web`` produced ``…enotify-webrest/…`` → 404).
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Shared session builder (accept header + 3-retry/502-503-504 policy); naming needs no auth.
        self.session = build_retrying_session()

        self._parts_cache: dict[str, list[dict[str, object]]] = {}
        self._names_cache: dict[str, dict[str, object]] = {}
        self._bool_cache: dict[str, bool] = {}

    @property
    def parts_url(self) -> str:
        return f"{self.base_url}/rest/parts/mnemonic/"

    @property
    def names_url(self) -> str:
        return f"{self.base_url}/rest/deviceNames/"

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def check_connectivity(self) -> bool:
        """Return True if the Naming Service is reachable; raise otherwise."""
        try:
            # Use the configured timeout (default 5 s), not a hardcoded 1 s — a
            # slow-but-reachable Naming Service (e.g. over the ESS VPN) must not
            # be falsely reported unreachable while the real GETs (self.timeout)
            # would have succeeded.
            self.session.head(self.base_url, timeout=self.timeout)
            return True
        except (
            # Every transport failure — Timeout, ConnectionError, read/HTTP errors — reaches this
            # arm and becomes a NamingServiceConnectionError (a WITHHELD signal), never a raw escape
            # that the diagnose gatherer could misread as a definitive "not registered" (S8-5).
            # requests.exceptions.RequestException ⊂ OSError, so OSError alone would already catch
            # them all; RequestException and ConnectionError are named explicitly for intent, and to
            # keep this guard robust if the OSError arm is ever narrowed.
            requests.exceptions.RequestException,
            ConnectionError,
            OSError,
        ) as exc:
            raise NamingServiceConnectionError(
                f"Failed to connect to Naming Service at {self.base_url}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Low-level GETs
    # ------------------------------------------------------------------

    def _get_parts(self, mnemonic: str) -> list[dict[str, object]]:
        """``GET /rest/parts/mnemonic/{mnemonic}`` (cached)."""
        if mnemonic in self._parts_cache:
            return self._parts_cache[mnemonic]
        try:
            resp = self.session.get(
                self.parts_url + url_quote(mnemonic, safe="-:"), timeout=self.timeout
            )
            resp.raise_for_status()
            data: list[dict[str, object]] = resp.json()
        except requests.exceptions.RequestException as exc:
            raise NamingServiceResponseError(
                f"Failed to query parts for '{mnemonic}': {exc}"
            ) from exc
        self._parts_cache[mnemonic] = data
        return data

    def _get_device_name(self, name: str) -> dict[str, object]:
        """``GET /rest/deviceNames/{name}`` (cached).

        A 404 raises :class:`NamingServiceNotFound` (the DEFINITIVE "not registered"); every other
        HTTP/transport/JSON failure raises the generic :class:`NamingServiceResponseError` so the
        caller can tell "name not registered" apart from a service/URL error (DS-2).
        """
        if name in self._names_cache:
            return self._names_cache[name]
        try:
            resp = self.session.get(
                self.names_url + url_quote(name, safe="-:"), timeout=self.timeout
            )
            resp.raise_for_status()
            data: dict[str, object] = resp.json()
        except requests.exceptions.HTTPError as exc:
            # A 404 on the deviceNames endpoint is the service's "not registered" answer. Any other
            # status (5xx, 401/403, or a 404 caused by a WRONG base path) is a service/URL failure
            # that must NOT collapse into a false "not registered" — split it out here.
            response = exc.response
            if response is not None and response.status_code == 404:
                raise NamingServiceNotFound(
                    f'The name "{name}" is not registered in the Naming Service'
                ) from exc
            raise NamingServiceResponseError(
                f"Failed to query device name '{name}': {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise NamingServiceResponseError(
                f"Failed to query device name '{name}': {exc}"
            ) from exc
        self._names_cache[name] = data
        return data

    # ------------------------------------------------------------------
    # High-level validation (read-only)
    # ------------------------------------------------------------------

    def _approved_part(self, mnemonic: str, *, part_type: str, levels: tuple[str, ...]) -> bool:
        """True if *mnemonic* has an Approved part of *part_type* at one of *levels*."""
        try:
            parts = self._get_parts(mnemonic)
        except NamingServiceResponseError:
            return False
        return any(
            item.get("status") == "Approved"
            and item.get("type") == part_type
            and item.get("level") in levels
            for item in parts
        )

    def validate_system(self, system: str) -> bool:
        """True if *system* is an Approved System-Structure mnemonic (level 1 or 2)."""
        key = f"sys:{system}"
        if key not in self._bool_cache:
            self._bool_cache[key] = self._approved_part(
                system, part_type="System Structure", levels=("1", "2")
            )
        return self._bool_cache[key]

    def validate_discipline(self, discipline: str) -> bool:
        """True if *discipline* is an Approved Device-Structure mnemonic (level 1)."""
        key = f"dis:{discipline}"
        if key not in self._bool_cache:
            self._bool_cache[key] = self._approved_part(
                discipline, part_type="Device Structure", levels=("1",)
            )
        return self._bool_cache[key]

    def validate_name(self, ess_name: str) -> NameStatus:
        """Check whether an ESS device name is registered and ACTIVE.

        *ess_name* is the device-name part of a PV (e.g. ``DEV-TEST01:Ctrl-EVR-01``),
        without the trailing property. Returns ``registered=True`` only for ``ACTIVE``;
        ``OBSOLETE``/``DELETED``/unknown → ``registered=False`` with the status preserved. A
        genuine 404 → ``registered=False`` (definitively not registered). A NON-404 service/URL
        failure (5xx, bad JSON, wrong endpoint, timeout) is NOT swallowed — it PROPAGATES as a
        :class:`NamingServiceResponseError` so the caller withholds instead of reporting a false
        "not registered" (DS-2 / audit S5). Callers that consult naming best-effort (crossplane)
        catch it; ``diagnose`` already withholds on any naming exception.
        """
        try:
            data = self._get_device_name(ess_name)
        except NamingServiceNotFound:
            return NameStatus(
                registered=False,
                status="",
                message=f'The name "{ess_name}" is not registered in the Naming Service',
            )
        status = str(data.get("status", ""))
        messages = {
            "ACTIVE": f'The name "{ess_name}" is registered (ACTIVE)',
            "OBSOLETE": f'The name "{ess_name}" is OBSOLETE',
            "DELETED": f'The name "{ess_name}" is DELETED',
        }
        return NameStatus(
            registered=status == "ACTIVE",
            status=status,
            message=messages.get(status, f'The name "{ess_name}" has unknown status "{status}"'),
        )
