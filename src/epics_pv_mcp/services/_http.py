"""Shared HTTP substrate for the read-only REST clients (M3/M12/L-Logger/C3).

:func:`build_retrying_session` and :func:`rest_get_json` replace the session/retry constructor block
and the GET-and-translate method that were copied verbatim across the ChannelFinder / Archiver /
Alarm / Naming clients. A retry-policy or logging change is now ONE edit here instead of four, and a
5th REST plane reuses both directly.

The single ``logger.debug`` line in :func:`rest_get_json` also wakes the previously-dead
per-client loggers: a swallowed REST failure (translated to a client exception, then to a withheld
verdict or an ``EpicsError``) now leaves a server-side trace it did not before.
"""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter

from epics_pv_mcp.config import get_config
from epics_pv_mcp.services.rest_exceptions import RestConnectionError, RestResponseError

logger = logging.getLogger(__name__)


def build_retrying_session(
    *,
    accept: str = "application/json",
    auth_header: str | None = None,
    verify: bool | str | None = None,
) -> requests.Session:
    """Return a :class:`requests.Session` with the accept header, optional auth, and a retry policy.

    The single source of the retry policy (3 retries, backoff 0.5, ``status_forcelist`` 502/503/504)
    shared by every REST client — change it here and all planes inherit it. urllib3 ships with
    requests, but the ``Retry`` import stays guarded so a stripped environment degrades to no-retry
    rather than failing at construction.

    TLS trust is resolved HERE, the single place every REST session is built, so all four clients
    (and the crossplane/coverage adapters and the direct diagnose naming client) inherit it without
    threading a ``verify`` argument through nine construction sites. ``verify`` defaults to the
    config (``ca_bundle`` path > ``tls_verify=False`` > ``True``); pass it explicitly only in tests.
    When the effective ``verify`` is anything other than plain ``True`` (a CA-bundle path, or
    verification disabled) the session also pins ``trust_env=False`` — otherwise a
    ``REQUESTS_CA_BUNDLE`` in the environment would win over ``session.verify`` via requests'
    per-request environment merge. On the plain default (``verify is True``) ``trust_env`` stays on,
    keeping the zero-code
    ``REQUESTS_CA_BUNDLE`` path working. Tradeoff: ``trust_env=False`` also disables proxy /
    ``NO_PROXY`` / netrc environment, which is why it is pinned ONLY when an explicit CA decision is
    in play (the internal-network REST planes), not on the default.
    """
    session = requests.Session()
    session.headers.update({"accept": accept})
    if auth_header:
        session.headers.update({"authorization": auth_header})
    if verify is None:
        cfg = get_config()
        verify = cfg.ca_bundle or cfg.tls_verify
    session.verify = verify
    if verify is not True:
        session.trust_env = False
    try:
        from urllib3.util.retry import Retry

        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    except ImportError:
        pass  # urllib3 retry unavailable — proceed without
    return session


def rest_get_json(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None,
    timeout: float,
    *,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
) -> object:
    """GET *url* and return parsed JSON, translating failures to the caller's REST exceptions.

    A connection failure raises *conn_exc*; any other request/HTTP failure (including a bad-JSON
    body, which modern requests surfaces as a ``RequestException``) raises *resp_exc* — the
    per-service subclasses of :class:`RestConnectionError` / :class:`RestResponseError`. The one
    debug log here is the single place a swallowed REST failure is recorded before the caller maps
    the exception to a withheld verdict or an ``EpicsError``.
    """
    try:
        resp = session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        logger.debug("REST GET failed for %s: %s", url, exc)
        if isinstance(exc, requests.exceptions.ConnectionError):
            raise conn_exc(f"Failed to connect to {url}: {exc}") from exc
        raise resp_exc(f"Request failed ({url}): {exc}") from exc


def is_http_404(exc: BaseException) -> bool:
    """True iff *exc* wraps an HTTP 404 response.

    :func:`rest_get_json` raises the per-service response error with ``raise ... from <requests
    error>``, so the chained cause of an HTTP failure is the requests ``HTTPError`` carrying
    ``.response``. A resource-by-id endpoint (``getPVTypeInfo`` / Olog ``/logs/{id}``) answers a
    missing item with 404, which callers map to a definitive "not found" while re-raising every
    other status. Duck-typed (no direct ``requests`` dependency at the call site) and null-safe.
    """
    response = getattr(exc.__cause__, "response", None)
    return getattr(response, "status_code", None) == 404
