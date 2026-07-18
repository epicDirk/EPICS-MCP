"""Shared HTTP substrate for the REST clients (M3/M12/L-Logger/C3).

:func:`build_retrying_session` and :func:`rest_get_json` replace the session/retry constructor block
and the GET-and-translate method that were copied verbatim across the ChannelFinder / Archiver /
Alarm / Naming clients. A retry-policy or logging change is now ONE edit here instead of four, and a
5th REST plane reuses both directly.

Read is the default; the ONE write path (Olog logbook posts) reuses this substrate via
:func:`rest_put_json` and :func:`basic_auth_header`. Every write is gated separately
(:mod:`epics_pv_mcp.olog_safety`) — this module only carries the transport.

The single ``logger.debug`` line in :func:`rest_get_json`/:func:`rest_put_json` also wakes the
previously-dead per-client loggers: a swallowed REST failure (translated to a client exception, then
to a withheld verdict or an ``EpicsError``) now leaves a server-side trace it did not before.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
from typing import Any

import requests
import urllib3.exceptions
import urllib3.util
from requests.adapters import HTTPAdapter

from epics_pv_mcp.config import get_config
from epics_pv_mcp.services.rest_exceptions import RestConnectionError, RestResponseError

logger = logging.getLogger(__name__)


def url_host(url: str) -> str | None:
    """The normalised host of *url*, or None if it has none / cannot be parsed (fail closed).

    The hardened host extraction behind every "which server am I talking to?" decision. It answers
    with the host the connection would ACTUALLY reach, which is why it parses with urllib3 — the
    parser ``requests`` itself connects through — rather than ``urllib.parse``. The two disagree on
    a backslash in the authority (``http://evil.example.org:8080\\@127.0.0.1/Olog``: urlparse splits
    at the last ``@`` and answers ``127.0.0.1``, urllib3 connects to ``evil.example.org``), and a
    decision that names a different server than the socket does is worse than no decision at all.

    Either parser strips userinfo, so ``http://127.0.0.1@evil.example.org/Olog`` yields
    ``evil.example.org``, NOT loopback; IPv6 brackets are stripped. Normalised: lowercase, trailing
    FQDN dot removed — and emptiness is judged AFTER that (``http://./Olog`` has host ``.`` which
    normalises to nothing, so it is None, not ``""``).

    Returns None for every unparseable form: hostless/garbage URLs and malformed authorities (both
    parsers raise ``LocationParseError``/``ValueError``). Callers treat None as a hard veto — see
    :meth:`~epics_pv_mcp.olog_safety.OlogWriteGate._url_write_allowed`, where "unparseable" must
    lose even against an explicit allowlist, which :func:`is_loopback_url` alone cannot express
    (it collapses "parsed, not loopback" and "did not parse" into the same False).
    """
    try:
        parsed = urllib3.util.parse_url(url)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return None  # malformed URL (e.g. bad bracketed IPv6) → fail closed
    if not parsed.scheme:
        # urllib3 is lenient where urlparse is not: it reads a bare "garbage" as a hostname. A base
        # URL without a scheme is not one, and nothing could connect to it — treat it as unparseable
        # so the veto fires rather than letting such a value reach an allowlist comparison.
        return None
    host = parsed.host
    if not host:  # None or "" — hostless URL ("http:///Olog")
        return None
    # urllib3 keeps IPv6 brackets ("[::1]"); ipaddress needs them off. Then normalise, and judge
    # emptiness AFTER: "http://./Olog" has host "." which normalises to nothing → still a veto.
    return host.strip("[]").rstrip(".").lower() or None


def is_loopback_url(url: str) -> bool:
    """True iff *url*'s host is a loopback address — i.e. a LOCAL test server, not a real facility.

    The shared "am I talking to a local sandbox?" primitive, used by two callers with DIFFERENT
    policies on top:

    * the Olog write gate (:mod:`epics_pv_mcp.olog_safety`) — loopback is one of two ways to pass;
      an explicitly allowlisted remote is the other.
    * the Olog read redaction (:mod:`epics_pv_mcp.services.olog_client`) — loopback is the ONLY way
      to see un-redacted entries.

    Only the PRIMITIVE is shared, never the policy: the write gate's ``_url_write_allowed`` also
    returns True for an allowlisted REMOTE host, so reusing IT as the read predicate would read a
    production logbook in the clear. Both policies do agree on the boolean direction, though —
    False means "restrict" (deny the write / redact the read) — so no inversion is needed here.

    Fails closed via :func:`url_host` (see there). RFC1918 private is deliberately NOT loopback — a
    production service lives on a private network, so "private = local" would defeat the point.
    """
    host = url_host(url)
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname is not an IP literal → not loopback


def is_https_url(url: str) -> bool:
    """True iff *url*'s scheme is ``https`` — parsed with urllib3 (the parser requests connects
    with), fail-closed on anything unparseable.

    The Olog write gate uses this to refuse a plain-``http`` write to an allowlisted REMOTE host: a
    Basic-auth PUT over http exposes the service-account credentials on the wire (and to any proxy).
    Loopback stays http-OK (a local sandbox), so this gates only the remote lane — see
    :meth:`~epics_pv_mcp.olog_safety.OlogWriteGate._url_write_allowed`.
    """
    try:
        parsed = urllib3.util.parse_url(url)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return False
    return (parsed.scheme or "").lower() == "https"


def basic_auth_header(user: str, password: str) -> str | None:
    """Return an HTTP ``Basic <base64(user:pass)>`` header value, or ``None`` if either is empty.

    ``None`` (empty user OR password) means NO authorization header is sent, so a server that
    requires auth answers 401 — a clear failure, never a silent unauthenticated write. The single
    tested place a Basic header is minted (DoD-F1: no ad-hoc base64 scattered across callers)."""
    if not user or not password:
        return None
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


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


def build_write_session(
    *,
    accept: str = "application/json",
    auth_header: str | None = None,
    verify: bool | str | None = None,
) -> requests.Session:
    """Return a :class:`requests.Session` for the ONE write path (Olog ``PUT /logs``): no retries,
    and deliberately ENV-INDEPENDENT. The sibling of :func:`build_retrying_session` for a
    credential-carrying mutation, where the read session's two conveniences turn into hazards (S23).

    Two deliberate divergences from the read factory:

    * **No retry policy** (``max_retries=0`` — no adapter carrying a ``Retry``). Olog ``PUT /logs``
      is NOT idempotent: every PUT mints a new entry. Under the read session's 3-retry policy a
      request the server PROCESSED but whose response was lost would be replayed into a DUPLICATE
      entry (urllib3's default ``allowed_methods`` retries PUT). A lost PUT thus surfaces as an
      error — an ``unknown`` outcome the caller must resolve by SEARCHING, never a blind retry —
      not a silent second entry.
    * **``trust_env=False`` always** (the read factory keeps it on at the plain default to preserve
      the zero-code ``REQUESTS_CA_BUNDLE`` path). The write session inherits NO ambient environment:
      no proxy / ``NO_PROXY`` / netrc, and no ``REQUESTS_CA_BUNDLE`` env. This closes N03 — an
      inherited proxy can never carry the Basic ``Authorization`` header outward — and keeps the
      write deterministic. The cost falls only on a REMOTE https Olog (loopback needs neither): its
      internal CA must come from the ``EPICS_MCP_CA_BUNDLE`` config (the DS-1 chokepoint), not the
      env, and it is not reachable through an env proxy.

    ``verify`` resolves the same VALUE as the read factory (``ca_bundle`` > ``tls_verify`` > True);
    only the env fallbacks are dropped. Pass it explicitly (the Olog client passes its read
    session's already-resolved ``verify``) so the two sessions agree on that configured VALUE. But
    they do NOT necessarily trust the same EFFECTIVE CA: because this session drops env fallbacks
    (``trust_env=False``), a ``REQUESTS_CA_BUNDLE`` env CA is honoured only by the read session,
    and a remote-https write's CA must come from ``EPICS_MCP_CA_BUNDLE`` config (the N03 tradeoff,
    per :meth:`~epics_pv_mcp.services.olog_client.OlogClient._write_session`).
    """
    session = requests.Session()
    session.headers.update({"accept": accept})
    if auth_header:
        session.headers.update({"authorization": auth_header})
    if verify is None:
        cfg = get_config()
        verify = cfg.ca_bundle or cfg.tls_verify
    session.verify = verify
    # Env-independent by design: no proxy / netrc / REQUESTS_CA_BUNDLE for a credentialed write.
    session.trust_env = False
    # max_retries=0 → requests builds Retry(total=0); a lost non-idempotent PUT is never replayed.
    no_retry = HTTPAdapter(max_retries=0)
    session.mount("http://", no_retry)
    session.mount("https://", no_retry)
    return session


def rest_get_json(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None,
    timeout: float,
    *,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
    allow_redirects: bool = True,
) -> object:
    """GET *url* and return parsed JSON, translating failures to the caller's REST exceptions.

    A connection failure raises *conn_exc*; any other request/HTTP failure (including a bad-JSON
    body, which modern requests surfaces as a ``RequestException``) raises *resp_exc* — the
    per-service subclasses of :class:`RestConnectionError` / :class:`RestResponseError`. The one
    debug log here is the single place a swallowed REST failure is recorded before the caller maps
    the exception to a withheld verdict or an ``EpicsError``.

    ``allow_redirects=False`` makes a redirect a *resp_exc* instead of a followed hop. It matters
    wherever the RESPONDING host, not the requested one, is what a security decision rests on: a
    redirect moves the data's true origin without changing the configured URL. A 3xx is not an HTTP
    error, so ``raise_for_status`` would wave it through — hence the explicit check.
    """
    try:
        resp = session.get(url, params=params, timeout=timeout, allow_redirects=allow_redirects)
        if not allow_redirects and resp.is_redirect:
            raise resp_exc(
                f"Refused to follow a redirect from {url} (HTTP {resp.status_code}): the response "
                "would come from a redirect target, not the configured URL."
            )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        logger.debug("REST GET failed for %s: %s", url, exc)
        if isinstance(exc, requests.exceptions.ConnectionError):
            raise conn_exc(f"Failed to connect to {url}: {exc}") from exc
        raise resp_exc(f"Request failed ({url}): {exc}") from exc


def rest_put_json(
    session: requests.Session,
    url: str,
    json_body: dict[str, Any],
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
    allow_redirects: bool = True,
) -> object:
    """PUT *json_body* to *url* and return JSON, translating failures like :func:`rest_get_json`.

    The write mirror of :func:`rest_get_json` (same error contract: a connection failure raises
    *conn_exc*, any other request/HTTP failure raises *resp_exc*, chained via ``from`` so
    :func:`http_status` can read the served status code). *params* carries wire query args (Olog's
    ``inReplyTo``); *headers* carries per-request headers (a static client-info header). Auth, if
    any, rides on the session (see :func:`basic_auth_header` + :func:`build_retrying_session`).

    ``allow_redirects=False`` refuses a redirect rather than follow it (see
    :func:`rest_get_json`).
    It matters even more on a write: a followed hop would post the body — and the auth header — to a
    host the gate never approved."""
    try:
        resp = session.put(
            url,
            json=json_body,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        if not allow_redirects and resp.is_redirect:
            raise resp_exc(
                f"Refused to follow a redirect from {url} (HTTP {resp.status_code}): the write "
                "would land on a redirect target, not the URL the gate approved."
            )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        logger.debug("REST PUT failed for %s: %s", url, exc)
        if isinstance(exc, requests.exceptions.ConnectionError):
            raise conn_exc(f"Failed to connect to {url}: {exc}") from exc
        raise resp_exc(f"Request failed ({url}): {exc}") from exc


def http_status(exc: BaseException) -> int | None:
    """The HTTP status code *exc* wraps, or ``None`` if it wraps no HTTP response.

    :func:`rest_get_json` raises the per-service error with ``raise ... from <requests error>``, so
    the chained cause of a *served* HTTP failure is the requests ``HTTPError`` with ``.response``
    with ``.status_code``. A transport failure (unreachable host / TLS) has no ``.response`` →
    ``None``. Duck-typed (no direct ``requests`` dependency at the call site) and null-safe. Tells
    "reachable but the API answered with an error status" (a served 4xx/5xx — e.g. an Archiver URL
    pointing at the wrong webapp) from "the host is unreachable" (no response at all).
    """
    response = getattr(exc.__cause__, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_http_404(exc: BaseException) -> bool:
    """True iff *exc* wraps an HTTP 404 response.

    A resource-by-id endpoint (``getPVTypeInfo`` / Olog ``/logs/{id}``) answers a missing item with
    404, which callers map to a definitive "not found" while re-raising every other status. Thin
    wrapper over :func:`http_status`.
    """
    return http_status(exc) == 404


def is_http_400(exc: BaseException) -> bool:
    """True iff *exc* wraps an HTTP 400 response.

    Olog ``PUT /logs`` answers a bad request (a non-existent logbook/tag, an empty title, or an
    ``inReplyTo`` that identifies no entry) with 400 — distinct from "not found". Thin wrapper over
    :func:`http_status`.
    """
    return http_status(exc) == 400


def is_ssl_error(exc: BaseException) -> bool:
    """True iff *exc* wraps a TLS/CA verification failure.

    :func:`rest_get_json` and the clients' ``check_connectivity`` chain the original requests error
    via ``from exc``. ``requests.exceptions.SSLError`` (a subclass of ``ConnectionError``, hence
    otherwise indistinguishable from a plain unreachable host) signals a certificate / CA-bundle
    problem — the signal a config ``doctor`` needs to say "fix your CA bundle" rather than
    "host unreachable". Null-safe.
    """
    return isinstance(getattr(exc, "__cause__", None), requests.exceptions.SSLError)


def is_retry_error(exc: BaseException) -> bool:
    """True iff *exc* wraps a retry-exhausted 5xx response.

    :func:`build_retrying_session` force-lists 502/503/504, so a served-but-retryable 5xx that
    exhausts the retry budget surfaces as ``requests.exceptions.RetryError`` — a RequestException
    that is NOT a ConnectionError and whose ``.response`` is ``None`` (so :func:`http_status` cannot
    read a code). It means the host DID answer (repeatedly, with a 5xx), so a config ``doctor``
    should report it as reachable-but-erroring, NOT "unreachable". Null-safe.
    """
    return isinstance(getattr(exc, "__cause__", None), requests.exceptions.RetryError)
