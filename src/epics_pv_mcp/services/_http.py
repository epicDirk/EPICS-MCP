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
import functools
import ipaddress
import logging
import threading
import time
from collections import deque
from email.message import Message
from http.cookiejar import DefaultCookiePolicy
from typing import Any

import requests
import urllib3.exceptions
import urllib3.util
from requests.adapters import HTTPAdapter

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import RateLimitError
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
    retries: int = 3,
    backoff_factor: float = 0.5,
    pool_maxsize: int | None = None,
) -> requests.Session:
    """Return a :class:`requests.Session` with the accept header, optional auth, and a retry policy.

    The single source of the retry policy (default 3 retries, backoff 0.5, ``status_forcelist``
    502/503/504) shared by every REST client — change the defaults here and all planes inherit them.
    urllib3 ships with requests, but the ``Retry`` import stays guarded so a stripped environment
    degrades to no-retry rather than failing at construction.

    ``retries`` and ``backoff_factor`` are parametrized (Q2) so a caller can request ``retries=0`` —
    a SINGLE attempt with no retry-multiplied timeout, the „one attempt, long timeout" shape this
    factory could not express before (urllib3 applies the per-request ``timeout`` PER attempt, so a
    3-retry session's worst case is ≈ 4×T plus backoff, with no wall-clock deadline). The default
    stays ``(3, 0.5)`` so every existing caller is unchanged. The ``retries=0`` path mirrors
    :func:`build_write_session`'s no-retry adapter shape (``HTTPAdapter(max_retries=0)`` → requests
    builds ``Retry(total=0)``), keeping the redirect/other guards consistent with a no-retry session
    rather than carrying a ``status_forcelist`` that ``total=0`` would never consult anyway.

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

    ``pool_maxsize`` sizes the ``HTTPAdapter`` connection pool. Left ``None`` it keeps requests'
    default (10). :func:`get_shared_session` passes the executor width so a process-cached session
    reused across the ~32-thread REST fan-out keeps connections warm instead of discarding one per
    over-the-limit request.
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
    if retries <= 0:
        # Single attempt (Q2 „one attempt, long timeout"): reuse build_write_session's no-retry
        # adapter shape. No Retry import needed — requests builds Retry(total=0) from the int, so
        # even a stripped environment gets a deterministic no-retry adapter here. pool_maxsize is
        # passed only when set, so the default path keeps requests' own default (10).
        no_retry = (
            HTTPAdapter(max_retries=0)
            if pool_maxsize is None
            else HTTPAdapter(max_retries=0, pool_maxsize=pool_maxsize)
        )
        session.mount("http://", no_retry)
        session.mount("https://", no_retry)
    else:
        try:
            from urllib3.util.retry import Retry

            retry = Retry(
                total=retries, backoff_factor=backoff_factor, status_forcelist=[502, 503, 504]
            )
            adapter = (
                HTTPAdapter(max_retries=retry)
                if pool_maxsize is None
                else HTTPAdapter(max_retries=retry, pool_maxsize=pool_maxsize)
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        except ImportError:
            pass  # urllib3 retry unavailable — proceed without
    return session


_SHARED_POOL_MAXSIZE = 32
"""Connection-pool size for the cached read sessions (K5). Matches the default asyncio executor
width (``min(32, cpu+4)``) so that up to ~32 concurrent REST reads on one host reuse the pool
instead of each opening — and requests then discarding — its own connection (requests' default
``pool_maxsize`` is 10, which serialises and logs 'Connection pool is full' under our fan-out)."""


def get_shared_session(
    *,
    accept: str = "application/json",
    auth_header: str | None = None,
    verify: bool | str | None = None,
    retries: int = 3,
    backoff_factor: float = 0.5,
) -> requests.Session:
    """Return a PROCESS-CACHED :class:`requests.Session` for one read configuration (K5).

    The five REST clients are re-instantiated on every tool-call — each inside its own ``_run()``
    closure dispatched via :func:`asyncio.to_thread` — so building a fresh session per ``__init__``
    paid a new TCP/TLS handshake on every call (no leak, pure waste). This memoises ONE session per
    distinct ``(accept, auth_header, verify, retries, backoff_factor)``. The session is NOT bound to
    a URL, so ``base_url`` is deliberately absent from the key: two same-auth clients (even for
    different service hosts) share one pooled session, and the pool keys connections per host
    itself. The cache is keyed on the *resolved* ``verify`` (below), and the adapter carries
    ``pool_maxsize`` = the executor width (:data:`_SHARED_POOL_MAXSIZE`).

    ``verify`` is resolved from config HERE, before the cache key, so a config change (a reload, or
    a test's monkeypatched ``get_config``) selects a DIFFERENT cache entry rather than serving a
    session built under the old TLS trust — the one correctness trap of caching a config-derived
    object. Sharing is safe across worker threads: the clients only READ their session
    (``.get``/``.head``/``.verify``), the urllib3 pools are thread-safe, and the one piece of
    per-request-MUTABLE state — the cookie jar — is DISABLED on these sessions (see
    :func:`_shared_session_cached`), so no unsynchronised state travels between the several hosts a
    no-auth session may reach. Per-request ``timeout`` stays per call. Reset via
    :func:`clear_shared_sessions` (test isolation / a reload wanting fresh pools).
    """
    if verify is None:
        cfg = get_config()
        verify = cfg.ca_bundle or cfg.tls_verify
    return _shared_session_cached(accept, auth_header, verify, retries, backoff_factor)


@functools.cache
def _shared_session_cached(
    accept: str,
    auth_header: str | None,
    verify: bool | str,
    retries: int,
    backoff_factor: float,
) -> requests.Session:
    """Memoised core of :func:`get_shared_session`. ``verify`` is already resolved (never ``None``)
    so it is a faithful part of the cache key. Separate function only so the resolution above sits
    OUTSIDE the ``lru_cache`` key."""
    session = build_retrying_session(
        accept=accept,
        auth_header=auth_header,
        verify=verify,
        retries=retries,
        backoff_factor=backoff_factor,
        pool_maxsize=_SHARED_POOL_MAXSIZE,
    )
    # This session is shared across worker threads. A requests cookie jar is the ONE piece
    # of per-request-MUTABLE state on a Session: a Set-Cookie from one plane mutates the jar while
    # another thread iterates it in prepare_request → an unsynchronised "dictionary changed size"
    # race (cookielib iterates without its lock). These are stateless REST reads that need no
    # cookies, so block cookie storage outright (DefaultCookiePolicy with an EMPTY allowed_domains =
    # set_ok False for every domain) → the jar never mutates and the shared session is thread-safe.
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return session


def clear_shared_sessions() -> None:
    """Drop every cached shared read session. Called by the test-isolation fixture (each test gets
    a clean cache, so a session built under one test's monkeypatched config never leaks into the
    next) and available to a config reload that wants fresh connection pools."""
    _shared_session_cached.cache_clear()


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


class ReadThrottle:
    """A sliding-window read rate limiter for the shared REST GET chokepoint (S3).

    A deliberate THIRD copy of the ``SafetyLayer`` / ``OlogWriteGate`` token bucket (a ``deque`` of
    ``time.monotonic`` timestamps under a lock), kept SEPARATE so the tested write gates are never
    touched. Unlike them it guards READS: the ~24 read tools all reach ``rest_get_json`` /
    ``rest_get_bytes`` from worker threads (``asyncio.to_thread``), so the bucket is thread-safe,
    and over the limit it RAISES (never blocks) — a blocking wait at this sync chokepoint would hold
    one of the shared worker threads and reintroduce exactly the K4 starvation the monitor bulkhead
    removes.

    Disabled by default (``limit <= 0``): :meth:`check` returns immediately with no lock and no
    allocation, so the posture stays opt-in — existing read behaviour is unchanged until
    an operator sets ``EPICS_MCP_READ_RATE_LIMIT``.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, limit: int) -> None:
        self._limit = limit
        # maxlen only when enabled; a disabled throttle never appends, so unbounded is fine.
        self._timestamps: deque[float] = deque(maxlen=limit) if limit > 0 else deque()
        self._lock = threading.Lock()

    def check(self) -> None:
        """Admit one read, or raise :class:`RateLimitError` if the sliding window is full.

        Purge → len-check → append is ONE atomic step under the lock (symmetric with the write
        gates), so two concurrent reads can never both pass and exceed the limit; ``now`` is sampled
        inside the lock, and the raise runs OUTSIDE it — the deny path never appends a token."""
        if self._limit <= 0:
            return  # disabled — opt-in posture, no throttling, no lock taken
        with self._lock:
            now = time.monotonic()
            self._purge_old(now)
            over_limit = len(self._timestamps) >= self._limit
            if not over_limit:
                self._timestamps.append(now)  # record this read (admit path only)
        if over_limit:
            raise RateLimitError(
                f"Read rate limit exceeded ({self._limit} reads per "
                f"{self._WINDOW_SECONDS:.0f}s). Try again later.",
                details={"limit": self._limit, "window_seconds": self._WINDOW_SECONDS},
            )

    def _purge_old(self, now: float) -> None:
        """Remove timestamps older than the sliding window."""
        cutoff = now - self._WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


_read_throttle: ReadThrottle | None = None
_read_throttle_lock = threading.Lock()


def get_read_throttle() -> ReadThrottle:
    """Return the singleton read throttle, built from ``read_rate_limit`` on first use (thread-safe;
    mirrors ``get_config`` / ``get_safety``). The limit is fixed for its lifetime; a config
    change takes effect only after :func:`reset_read_throttle` (a test hook)."""
    global _read_throttle
    with _read_throttle_lock:
        if _read_throttle is None:
            _read_throttle = ReadThrottle(get_config().read_rate_limit)
    return _read_throttle


def reset_read_throttle() -> None:
    """Drop the singleton so the next :func:`get_read_throttle` rebuilds it with the current config
    (test isolation, or a reload wanting the new limit)."""
    global _read_throttle
    with _read_throttle_lock:
        _read_throttle = None


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
    get_read_throttle().check()  # S3 read throttle — no-op unless read_rate_limit > 0
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


#: A ``requests`` multipart ``files=`` payload as a LIST of ``(part_name, (filename, content,
#: content_type))`` tuples — deliberately a list, never a dict: the Olog multipart carries several
#: parts all named ``files``, and a dict would silently keep only the last (requests collapses
#: duplicate keys). ``filename`` is ``None`` for a text part (the ``logEntry`` JSON) and the unique
#: filename for a file part; ``content`` is a JSON ``str`` or the raw file ``bytes``.
MultipartFiles = list[tuple[str, tuple[str | None, str | bytes, str]]]


def _request_multipart(
    session: requests.Session,
    method: str,
    url: str,
    files: MultipartFiles,
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
    allow_redirects: bool = False,
) -> object:
    """Send a ``multipart/form-data`` body via *method* to *url* and return JSON — the shared core
    of :func:`rest_put_multipart` (create) and :func:`rest_post_multipart` (attach-to-existing).

    *files* is a LIST of ``(name, (filename, content, content_type))`` tuples (see
    :data:`MultipartFiles`). ``requests`` builds the multipart body from it AND sets the
    ``Content-Type: multipart/form-data; boundary=…`` header itself — so this passes **no** manual
    ``Content-Type`` (one would clobber the boundary and the server could not parse the body). This
    mirrors CS-Studio's own client, which builds the same body by hand
    (``HttpRequestMultipartBody``): a ``logEntry`` JSON part plus one ``files`` part per attachment.

    *headers* carries per-request headers (a static client-info header); it MUST NOT include
    ``Content-Type`` — ``requests`` sets that (with the boundary) from *files*. Auth rides on the
    session. ``allow_redirects=False`` refuses a redirect rather than follow it (see
    :func:`rest_put_json`): on a write a followed hop would post the body — and the Basic auth
    header — to a host the gate never approved. Defaults to False because every Olog attachment
    write is gated to a specific host."""
    try:
        resp = session.request(
            method,
            url,
            files=files,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        if not allow_redirects and resp.is_redirect:
            raise resp_exc(
                f"Refused to follow a redirect from {url} (HTTP {resp.status_code}): the upload "
                "would land on a redirect target, not the URL the gate approved."
            )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        logger.debug("REST %s (multipart) failed for %s: %s", method, url, exc)
        if isinstance(exc, requests.exceptions.ConnectionError):
            raise conn_exc(f"Failed to connect to {url}: {exc}") from exc
        raise resp_exc(f"Request failed ({url}): {exc}") from exc


def rest_put_multipart(
    session: requests.Session,
    url: str,
    files: MultipartFiles,
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
    allow_redirects: bool = False,
) -> object:
    """PUT a ``multipart/form-data`` body to *url* (Olog create-with-attachments, ``PUT
    /logs/multipart``) — a thin :func:`_request_multipart` wrapper. See there for the contract."""
    return _request_multipart(
        session,
        "PUT",
        url,
        files,
        timeout,
        params=params,
        headers=headers,
        conn_exc=conn_exc,
        resp_exc=resp_exc,
        allow_redirects=allow_redirects,
    )


def rest_post_multipart(
    session: requests.Session,
    url: str,
    files: MultipartFiles,
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
    allow_redirects: bool = False,
) -> object:
    """POST a ``multipart/form-data`` body to *url* (Olog attach-to-existing, ``POST
    /logs/multipart`` = the server's ``updateLog``) — the POST sibling of
    :func:`rest_put_multipart`. Same contract; the verb differs because the server routes create
    (PUT) and update (POST) separately."""
    return _request_multipart(
        session,
        "POST",
        url,
        files,
        timeout,
        params=params,
        headers=headers,
        conn_exc=conn_exc,
        resp_exc=resp_exc,
        allow_redirects=allow_redirects,
    )


def _filename_from_content_disposition(header: str | None) -> str | None:
    """The download filename parsed from a ``Content-Disposition`` header value, or ``None``.

    Uses the stdlib :class:`email.message.Message` parser (handles quoting and the RFC 2231
    ``filename*=`` form), so no ad-hoc regex. Olog serves attachments with
    ``Content-Disposition: attachment; filename=…``; the value is author free text, so callers gate
    it under the same posture as the bytes (a person can be named in a filename).
    """
    if not header:
        return None
    message = Message()
    message["content-disposition"] = header
    filename = message.get_filename()
    return filename if isinstance(filename, str) and filename else None


def _read_body_capped(
    resp: requests.Response, max_bytes: int | None, url: str, resp_exc: type[RestResponseError]
) -> bytes:
    """Read the response body, refusing anything over *max_bytes* (``None`` = no cap).

    A declared ``Content-Length`` over the cap is refused BEFORE the body is read; then the streamed
    body is accumulated only up to the cap, so a MISSING or LYING length cannot slip a huge object
    past it. This keeps a multi-GB attachment from being materialised into the MCP process
    memory."""
    if max_bytes is None:
        return resp.content
    declared = resp.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise resp_exc(
            f"Response body from {url} exceeds the size cap "
            f"({max_bytes} bytes; Content-Length {declared})."
        )
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > max_bytes:
            raise resp_exc(f"Response body from {url} exceeds the size cap ({max_bytes} bytes).")
        chunks.append(chunk)
    return b"".join(chunks)


def rest_get_bytes(
    session: requests.Session,
    url: str,
    timeout: float,
    *,
    params: dict[str, str] | None = None,
    max_bytes: int | None = None,
    conn_exc: type[RestConnectionError],
    resp_exc: type[RestResponseError],
    allow_redirects: bool = False,
) -> tuple[bytes, str | None, str | None]:
    """GET raw BYTES from *url* → ``(content, filename, content_type)``, translating failures like
    :func:`rest_get_json`.

    The byte mirror of :func:`rest_get_json` for an attachment download (Olog's response body is a
    file, not JSON). Streams (``stream=True``) and **explicitly closes the response** via the
    context
    manager — there is no other streaming caller in this module, so a leaked connection would be a
    silent regression. *max_bytes* caps the body (:func:`_read_body_capped` — a Content-Length over
    it
    is refused before any read, and the stream is accumulated only up to the cap); ``None`` = no
    cap,
    but the attachment caller always passes one, so a huge object is never materialised into memory.
    ``filename`` comes from ``Content-Disposition`` (Olog sends ``attachment; filename=…``) and
    ``content_type`` from the response header (Olog derives it from the file extension server-side
    and
    may omit it → ``None``); both are surfaced but a caller applies the download privacy gate first.

    ``allow_redirects=False`` refuses a redirect (see :func:`rest_get_json`): the download posture
    is
    decided from the CONFIGURED host, so a followed hop would let a loopback URL serve bytes from a
    real server. Defaults to False here because the whole attachment surface is host-gated."""
    get_read_throttle().check()  # S3 read throttle — no-op unless read_rate_limit > 0
    try:
        with session.get(
            url,
            params=params,
            timeout=timeout,
            stream=True,
            allow_redirects=allow_redirects,
        ) as resp:
            if not allow_redirects and resp.is_redirect:
                raise resp_exc(
                    f"Refused to follow a redirect from {url} (HTTP {resp.status_code}): the bytes "
                    "would come from a redirect target, not the configured URL."
                )
            resp.raise_for_status()
            content = _read_body_capped(resp, max_bytes, url, resp_exc)
            filename = _filename_from_content_disposition(resp.headers.get("Content-Disposition"))
            content_type = resp.headers.get("Content-Type")
            return content, filename, content_type
    except requests.exceptions.RequestException as exc:
        logger.debug("REST GET (bytes) failed for %s: %s", url, exc)
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
