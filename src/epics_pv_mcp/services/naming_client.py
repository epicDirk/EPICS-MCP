"""Client for the ESS Naming Service REST API (read-only).

Vendored and slimmed from pvValidator's ``pvValidatorUtils/naming_client.py`` (Alfio
Rizzo, ESS) so this repo stays standalone, pvValidator itself is Linux/SWIG-only and
cannot be imported on Windows, but its Naming-Service client is pure Python (``requests``
+ stdlib). The "Did you mean?"/confusable helpers (which pull in pvValidator's ``rules``) and the
parts/mnemonic validators (dead code with a fail-open trap, removed in S13) are intentionally
dropped; only the one read-only call the cross-plane check needs is kept. Endpoint (GET):

  GET /rest/deviceNames/{name}, check if an ESS device name is registered + status
"""

from __future__ import annotations

import logging
from typing import TypedDict
from urllib.parse import quote as url_quote

import requests

from epics_pv_mcp.services._http import get_read_throttle, get_shared_session
from epics_pv_mcp.services.naming_exceptions import (
    NamingServiceConnectionError,
    NamingServiceNotFound,
    NamingServiceResponseError,
)
from epics_pv_mcp.services.naming_identity import (
    NAMING_SWAGGER_PATH,
    IdentityVerdict,
    probe_naming_identity,
)

logger = logging.getLogger(__name__)


class NameStatus(TypedDict):
    """Result of :meth:`NamingServiceClient.validate_name`."""

    registered: bool
    status: str
    message: str


class NamingServiceClient:
    """Read-only client for the ESS Naming Service REST API.

    All methods issue ``GET`` requests only, nothing is ever written to the service.
    Results are cached in-memory for the lifetime of the instance.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
    ) -> None:
        # ``base_url`` is REQUIRED and caller-provided (from ``EPICS_MCP_NAMING_URL`` via config):
        # there is deliberately NO built-in default host, so this client never reaches a hard-coded
        # ESS endpoint. Callers gate on an unset URL (see checkers.build_naming_client + diagnose).
        # Normalise like the other three REST clients (channelfinder/archiver/alarm): strip a
        # trailing slash so a URL configured with OR without it yields the same endpoints (M10;
        # without this, ``http://naming:8080/enotify-web`` produced ``…enotify-webrest/…`` → 404).
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Shared cached session (accept header + 3-retry/502-503-504 policy); naming needs no auth.
        self.session = get_shared_session()

        self._names_cache: dict[str, dict[str, object]] = {}
        #: The service-identity verdict (S13), probed at most once per instance on the first
        #: definitive-negative lookup and reused thereafter, see ``_require_verified_identity``.
        self._identity: IdentityVerdict | None = None

    # ``base_url`` is the service root WITHOUT a trailing ``/rest``: this property appends it
    # itself, so configuring the URL with a trailing ``/rest`` yields ``/rest/rest/…`` → 404.
    @property
    def names_url(self) -> str:
        return f"{self.base_url}/rest/deviceNames/"

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def check_connectivity(self) -> bool:
        """Return True if the Naming Service is reachable; raise otherwise."""
        try:
            # Use the configured timeout (default 5 s), not a hardcoded 1 s, a
            # slow-but-reachable Naming Service (e.g. over the ESS VPN) must not
            # be falsely reported unreachable while the real GETs (self.timeout)
            # would have succeeded.
            self.session.head(self.base_url, timeout=self.timeout)
            return True
        except (
            # Every transport failure, Timeout, ConnectionError, read/HTTP errors, reaches this
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

    def _get_device_name(self, name: str) -> dict[str, object]:
        """``GET /rest/deviceNames/{name}`` (cached).

        TWO measured definitive "not registered" signals raise :class:`NamingServiceNotFound`:
        a 404, and, what the real ESS service actually answers for a nonexistent name (measured
        live 2026-07-16, S16a), an HTTP **204** No Content. Every other HTTP/transport/JSON
        failure, and a 2xx whose body is not a record dict (S11), raises the generic
        :class:`NamingServiceResponseError` so the caller can tell "name not registered" apart
        from a service/URL error (DS-2).

        **S13 identity gate:** a 204/404 is trusted as definitive ONLY after
        :meth:`_require_verified_identity` confirms the responder is the Naming Service (its swagger
        beacon). A foreign/misconfigured URL answering 404 because it lacks ``/rest/deviceNames/``
        is WITHHELD (raises :class:`NamingServiceResponseError`, NOT
        :class:`NamingServiceNotFound`), closing the wrong-base-path/foreign-host RESIDUAL the old
        contract documented as open. Two residuals remain, both judged acceptable: (1) a foreign
        host that BOTH serves the ESS Naming swagger verbatim AND answers 204/404 on deviceNames
        (implausible); and (2), OUTSIDE this 204/404 gate, the ungated positive/record path
        (:meth:`validate_name`): a foreign 200 carrying a well-formed ``status`` field is trusted
        without an identity probe. The measured hazard was a foreign 404; gating the positive path
        would withhold real records whenever the swagger endpoint is momentarily flaky (S13 Nit 1).
        """
        if name in self._names_cache:
            return self._names_cache[name]
        # S3 throttle: this Naming lookup uses a DIRECT session.get, NOT rest_get_json (it needs
        # the raw 204/404 for its identity-gated negative answer, which rest_get_json swallows),
        # so it must consult the shared read throttle itself, otherwise the documented "bounds the
        # REST planes incl. Naming" would be false. No-op unless read_rate_limit > 0; a cache
        # hit returns above without reaching here, so it costs no token.
        get_read_throttle().check()
        try:
            resp = self.session.get(
                self.names_url + url_quote(name, safe="-:"), timeout=self.timeout
            )
            resp.raise_for_status()
            if resp.status_code == 204:
                # Measured (S16a): the service's actual "no such name" is 204 + empty body:
                # NOT 404. Split it out BEFORE resp.json(), which would fail on the empty body
                # and needlessly withhold a definitive answer.
                self._require_verified_identity()  # S13: withhold unless the responder is Naming
                raise NamingServiceNotFound(
                    f'The name "{name}" is not registered in the Naming Service'
                )
            data: object = resp.json()
        except requests.exceptions.HTTPError as exc:
            # A 404 on the deviceNames endpoint is the service's "not registered" answer. Any other
            # status (5xx, 401/403, or a 404 caused by a WRONG base path) is a service/URL failure
            # that must NOT collapse into a false "not registered", split it out here.
            response = exc.response
            if response is not None and response.status_code == 404:
                self._require_verified_identity()  # S13: withhold unless the responder is Naming
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
        if not isinstance(data, dict):
            # S11: a non-dict 2xx used to escape as an uncaught AttributeError in validate_name
            # (crashing crossplane_check), it must be the plane's own error so callers withhold.
            raise NamingServiceResponseError(
                f"Naming Service device-name query for '{name}' returned an unreadable payload "
                f"(expected a record dict, got {type(data).__name__})."
            )
        self._names_cache[name] = data
        return data

    def _require_verified_identity(self) -> None:
        """Withhold a would-be definitive "not registered" unless the responder proved it is the
        Naming Service (S13).

        A 204/404 on ``/rest/deviceNames/`` is trusted as a definitive ``registered=False`` ONLY
        when the SAME host also identifies itself as the Naming Service via its swagger beacon
        (:func:`~epics_pv_mcp.services.naming_identity.probe_naming_identity`). A foreign or
        misconfigured URL that answers 404 because it lacks that path would otherwise fabricate a
        false ``registered=False`` → a false ``name_typo`` in ``diagnose`` (the measured S13 gap).
        If identity is not ``verified``, raise the PARENT :class:`NamingServiceResponseError`, NOT
        its subclass :class:`NamingServiceNotFound`, which :meth:`validate_name` maps to
        ``registered=False``: so every caller WITHHOLDS. Probed at most once per client instance
        and cached on ``self._identity`` (both ``unverified`` and ``probe_failed`` withhold; the
        distinction only enriches the debug trace)."""
        if self._identity is None:
            self._identity = probe_naming_identity(self.base_url, timeout=self.timeout)
        if self._identity != "verified":
            logger.debug(
                "Naming not-registered WITHHELD: identity probe to %s%s = %s (not verified)",
                self.base_url,
                NAMING_SWAGGER_PATH,
                self._identity,
            )
            raise NamingServiceResponseError(
                f"Naming Service identity at {self.base_url} could not be confirmed via its "
                f"swagger beacon (probe: {self._identity}); a 'not registered' answer is withheld "
                "rather than trusted as definitive (S13)."
            )

    # ------------------------------------------------------------------
    # High-level validation (read-only)
    # ------------------------------------------------------------------

    def validate_name(self, ess_name: str) -> NameStatus:
        """Check whether an ESS device name is registered and ACTIVE.

        *ess_name* is the device-name part of a PV (e.g. ``DEV-TEST01:Ctrl-EVR-01``),
        without the trailing property. Returns ``registered=True`` only for ``ACTIVE``;
        ``OBSOLETE``/``DELETED``/unknown → ``registered=False`` with the status preserved. The
        service's measured "no such name", HTTP **204** (S16a), and a genuine 404 map to
        ``registered=False`` (definitively not registered) ONLY when the responder proves it is the
        Naming Service (S13 gate → ``_require_verified_identity``); an
        unverified/foreign responder's 204/404 is WITHHELD instead. Every OTHER service/URL failure
        (5xx, bad JSON, wrong endpoint, timeout) and a 2xx record without a readable ``status``
        (S11) is NOT swallowed, it PROPAGATES as a :class:`NamingServiceResponseError` so the
        caller withholds instead of reporting a false "not registered" (DS-2 / audit S5).
        Callers that consult naming best-effort (crossplane) catch it; ``diagnose`` already
        withholds on any naming exception.
        """
        try:
            data = self._get_device_name(ess_name)
        except NamingServiceNotFound:
            return NameStatus(
                registered=False,
                status="",
                message=f'The name "{ess_name}" is not registered in the Naming Service',
            )
        raw_status = data.get("status")
        if not isinstance(raw_status, str) or not raw_status:
            # S11: the measured record always carries a NON-EMPTY string `status` (the anchor).
            # A missing, junk or empty status used to become the definitive registered=False
            # ('' / str()-minted '123' read as an unknown status), a fabricated negative.
            # Raise → callers withhold.
            raise NamingServiceResponseError(
                f"Naming Service record for '{ess_name}' carries no readable 'status' "
                f"(got {type(raw_status).__name__}); the answer is not readable."
            )
        status = raw_status
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
