"""Shared HTTP substrate for the REST clients (M3/M12/L-Logger/C3).

:func:`build_retrying_session` and :func:`rest_get_json` replace the session/retry constructor block
and the GET-and-translate method that were copied verbatim across the ChannelFinder / Archiver /
Alarm / Naming clients. A retry-policy or logging change is now ONE edit here instead of four, and a
5th REST plane reuses both directly.

Read is the default; the ONE write path (Olog logbook posts) reuses this substrate via
:func:`rest_put_json` and :func:`basic_auth_header`. Every write is gated separately
(:mod:`epics_mcp.olog_safety`), this module only carries the transport.

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
from http import HTTPStatus
from http.cookiejar import DefaultCookiePolicy
from typing import Any

import requests
import urllib3.exceptions
import urllib3.util
from requests.adapters import HTTPAdapter

from epics_mcp.config import get_config
from epics_mcp.errors import ReadRateLimitError
from epics_mcp.services.rest_exceptions import RestConnectionError, RestResponseError

logger = logging.getLogger(__name__)


def url_host(url: str) -> str | None:
    """The normalised host of *url*, or None if it has none / cannot be parsed (fail closed).

    The hardened host extraction behind every "which server am I talking to?" decision. It answers
    with the host the connection would ACTUALLY reach, which is why it parses with urllib3, the
    parser ``requests`` itself connects through, rather than ``urllib.parse``. The two disagree on
    a backslash in the authority (``http://evil.example.org:8080\\@127.0.0.1/Olog``: urlparse splits
    at the last ``@`` and answers ``127.0.0.1``, urllib3 connects to ``evil.example.org``), and a
    decision that names a different server than the socket does is worse than no decision at all.

    Either parser strips userinfo, so ``http://127.0.0.1@evil.example.org/Olog`` yields
    ``evil.example.org``, NOT loopback; IPv6 brackets are stripped. Normalised: lowercase, trailing
    FQDN dot removed, and emptiness is judged AFTER that (``http://./Olog`` has host ``.`` which
    normalises to nothing, so it is None, not ``""``).

    Returns None for every unparseable form: hostless/garbage URLs and malformed authorities (both
    parsers raise ``LocationParseError``/``ValueError``). Callers treat None as a hard veto, see
    :meth:`~epics_mcp.olog_safety.OlogWriteGate._url_write_allowed`, where "unparseable" must
    lose even against an explicit allowlist, which :func:`is_loopback_url` alone cannot express
    (it collapses "parsed, not loopback" and "did not parse" into the same False).
    """
    try:
        parsed = urllib3.util.parse_url(url)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return None  # malformed URL (e.g. bad bracketed IPv6) → fail closed
    if not parsed.scheme:
        # urllib3 is lenient where urlparse is not: it reads a bare "garbage" as a hostname. A base
        # URL without a scheme is not one, and nothing could connect to it, treat it as unparseable
        # so the veto fires rather than letting such a value reach an allowlist comparison.
        return None
    host = parsed.host
    if not host:  # None or "", hostless URL ("http:///Olog")
        return None
    # urllib3 keeps IPv6 brackets ("[::1]"); ipaddress needs them off. Then normalise, and judge
    # emptiness AFTER: "http://./Olog" has host "." which normalises to nothing → still a veto.
    return host.strip("[]").rstrip(".").lower() or None


def url_without_credentials(url: str) -> str:
    """*url* rebuilt without its userinfo, query and fragment, for printing.

    Not a regex redaction, and that distinction was measured rather than argued. The pattern-based
    redactor in ``services/doctor.py`` matches ``scheme://user:pass@`` up to the FIRST ``@``, while
    urllib3 (the parser ``requests`` connects with, and the one this function uses) splits the
    authority at the LAST one. So a password that legitimately contains ``@``, say
    ``https://svc:hun@ter2@host/Olog``, keeps its tail in the clear under the regex, and a bare
    ``https://svc@host/Olog`` username is not touched by it at all. Rebuilding drops the whole
    userinfo whatever it contains.

    Query and fragment go too, because a base URL does not need them and a token is a normal thing
    to find in a query string. Returns ``"(unparseable)"`` when the parser refuses the URL: such a
    value is already a hard veto at every boundary that reads it, and echoing the raw string would
    reintroduce exactly the leak this function exists to close.

    ⚠️ The result is NOT a string an operator can hold against
    ``EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST``, and an earlier version of this docstring said it was.
    The gate compares the RAW configured value, exactly and case-sensitively
    (``olog_safety.write_target_allowed``), while this rebuild normalises: measured, the host comes
    back lower-cased, a query or fragment is dropped, and a space in the path is percent-encoded,
    so four out of five realistic spellings print differently from the string the boundary
    compared. What it IS good for is naming the ADDRESS a write would reach, host and port
    included, which is the question the report asks around it.

    Where a reader has to COMPARE the answer against a configured value instead of reading an
    address off it, :func:`url_without_userinfo` is the one to reach for: it deletes the userinfo
    and leaves every other character alone, at the price of withholding what it cannot prove.
    """
    try:
        parsed = urllib3.util.parse_url(url)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return "(unparseable)"
    if not parsed.scheme or not parsed.host:
        return "(unparseable)"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.host}{port}{parsed.path or ''}"


def _authority_span(url: str) -> tuple[int, int]:
    """The half-open ``[start, end)`` of *url*'s authority, as offsets into the RAW string.

    Offsets rather than the parsed pieces, because urllib3 re-encodes what it hands back
    (``svc:hun@ter2`` comes out as ``svc:hun%40ter2``), and a caller that wants to delete the
    userinfo without rewriting anything else has to find it in the original characters. The
    authority starts after the ``//`` and ends at the first ``/``, ``?`` or ``#``. The span is
    deliberately never NARROWER than urllib3's own, which also ends at a backslash, so the last
    ``@`` that parser saw is always inside it; it may be wider, and refusing a cut that used the
    extra width is the verification's job, not this function's.
    """
    separator = url.find("//")
    start = separator + 2 if separator != -1 else 0
    ends = (url.find(delimiter, start) for delimiter in "/?#")
    return start, min((end for end in ends if end != -1), default=len(url))


def _keeps_the_same_address(candidate: str, original: urllib3.util.Url) -> bool:
    """True iff *candidate* is *original*'s address with the userinfo gone and nothing else moved.

    Three conditions, each closing a case the textual cut gets wrong on its own, all measured:
    no ``@`` survives ANYWHERE (``https://svc:p@ss/w0rd@host/x`` parses with host ``ss``, so
    cutting its authority would leave half the password behind in the path), the result parses,
    and it agrees with the original on all six components (a backslash in the authority ends it
    for urllib3 but not for a delimiter scan, and a cut using the wider span turned
    ``evil.example.org`` into ``127.0.0.1``, naming a host nothing would connect to).

    A fourth condition, "and it carries no userinfo", was written here and then removed as
    UNREACHABLE rather than left standing: urllib3 reads a userinfo only from the text before an
    ``@``, so the first condition already refuses every candidate that could have one. Measured on
    200000 ``@``-free strings, not one is given an ``auth``. A guard no input can reach is not a
    second line of defence, it is a claim nothing tests.

    The comparison runs on PARSED components on both sides, so urllib3's normalisation cancels out
    and a result that preserved the original's case, its spaces and its query still compares equal.
    """
    if "@" in candidate:
        return False
    try:
        after = urllib3.util.parse_url(candidate)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return False
    return (
        after.scheme,
        after.host,
        after.port,
        after.path,
        after.query,
        after.fragment,
    ) == (
        original.scheme,
        original.host,
        original.port,
        original.path,
        original.query,
        original.fragment,
    )


def url_without_userinfo(url: str) -> str | None:
    """*url* with its userinfo removed and EVERY OTHER CHARACTER unchanged, or ``None``.

    The sibling of :func:`url_without_credentials`, and the two are not interchangeable. That one
    REBUILDS the address from the parse, which normalises (lower-cased host, dropped query and
    fragment, percent-encoded path), and is right where the question is "which ADDRESS would a
    write reach". This one DELETES a span out of the string, and is right where a reader has to
    COMPARE the answer against a configured value character for character, which is what
    ``epics-pv://config`` exists for (``docs/deployment.md``): a normalised address makes that
    comparison false-negative, showing a difference where there is none.

    The rule in one sentence: an address the parser accepts and that carries no ``@`` is passed
    through unchanged; an address with one is either PROVABLY the same address minus its userinfo,
    or it is withheld as ``None``.

    A LITERAL ``@`` is the precondition, and urllib3 decides everything after it, because it is
    the parser ``requests`` connects with and its reading is the one a socket follows. The cut is a
    pure deletion at the LAST ``@`` of the authority (a regex stopping at the first one leaves the
    tail of a password containing ``@`` in the clear). The result is then handed BACK to urllib3,
    see :func:`_keeps_the_same_address`; that verification is what refuses the spellings a textual
    rule alone gets wrong, and ``None`` is the answer whenever it cannot be satisfied.

    An address the PARSER REFUSES is withheld whether or not it carries an ``@``. Without that, a
    credential whose separator is percent-encoded (``https://svc:pw%40host/x``, measured: urllib3
    refuses it, and it has no literal ``@``) would be printed in full. Nothing can connect to such
    a string either way, so withholding it costs an operator only the sight of their own typo.

    The ``auth is None`` clause below is not redundant, and the earlier claim that it was is
    withdrawn: measured, ``https://@host/x`` has NO userinfo for urllib3 while a delimiter scan
    finds an ``@`` in the authority, so without the clause the address would be silently rewritten.
    It has its own row in the pinned table.

    ``None`` costs one harmless case on purpose: an ``@`` inside a path or query (``/CF?mail=a@b``)
    is withheld as well, because nothing distinguishes it from a credential written in a spelling
    urllib3 reads differently than a person does (``https://DOMAIN\\user:pw@host/x`` parses with
    host ``domain``). A service ROOT carrying an ``@`` outside its userinfo is not an address this
    server has ever had to print.

    ⚠️ A token in a QUERY STRING is NOT removed, which is the price of the character-for-character
    promise; the sibling drops the query for exactly that reason, pinned in
    ``tests/test_doctor.py::test_a_credential_in_the_olog_url_never_reaches_the_report``. The
    documented place for credentials is the ``EPICS_MCP_*_AUTH`` header, never the URL, see
    ``docs/configuration.md`` and ``docs/known-limits.md``.
    """
    try:
        parsed = urllib3.util.parse_url(url)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return None  # the parser refuses it, so a credential can hide in a spelling with no "@"
    if "@" not in url:
        return url  # nothing that could be a userinfo, so nothing to prove
    if not parsed.scheme or not parsed.host or parsed.auth is None:
        return None  # no address a socket could follow, or an "@" urllib3 does not read as one
    start, end = _authority_span(url)
    # rfind cannot miss here: urllib3 found a userinfo, and its authority is never wider than the
    # span above. If it ever did, the candidate would keep its "@" and be refused below rather
    # than trusted, so this needs no branch of its own.
    at = url.rfind("@", start, end)
    candidate = url[:start] + url[at + 1 :]
    return candidate if _keeps_the_same_address(candidate, parsed) else None


def is_loopback_url(url: str) -> bool:
    """True iff *url*'s host is a loopback address, i.e. a LOCAL test server, not a real facility.

    The shared "am I talking to a local sandbox?" primitive, used by two callers with DIFFERENT
    policies on top:

    * the Olog write gate (:mod:`epics_mcp.olog_safety`), loopback is one of two ways to pass;
      an explicitly allowlisted remote is the other.
    * the Olog read redaction (:mod:`epics_mcp.services.olog_client`), loopback is the ONLY way
      to see un-redacted entries.

    Only the PRIMITIVE is shared, never the policy: the write gate's ``_url_write_allowed`` also
    returns True for an allowlisted REMOTE host, so reusing IT as the read predicate would read a
    production logbook in the clear. Both policies do agree on the boolean direction, though:
    False means "restrict" (deny the write / redact the read), so no inversion is needed here.

    Fails closed via :func:`url_host` (see there). RFC1918 private is deliberately NOT loopback, a
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
    """True iff *url*'s scheme is ``https``, parsed with urllib3 (the parser requests connects
    with), fail-closed on anything unparseable.

    The Olog write gate uses this to refuse a plain-``http`` write to an allowlisted REMOTE host: a
    Basic-auth PUT over http exposes the service-account credentials on the wire (and to any proxy).
    Loopback stays http-OK (a local sandbox), so this gates only the remote lane, see
    :meth:`~epics_mcp.olog_safety.OlogWriteGate._url_write_allowed`.
    """
    try:
        parsed = urllib3.util.parse_url(url)
    except (urllib3.exceptions.LocationParseError, ValueError):
        return False
    return (parsed.scheme or "").lower() == "https"


def basic_auth_header(user: str, password: str) -> str | None:
    """Return an HTTP ``Basic <base64(user:pass)>`` header value, or ``None`` if either is empty.

    ``None`` (empty user OR password) means NO authorization header is sent, so a server that
    requires auth answers 401, a clear failure, never a silent unauthenticated write. The single
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
    502/503/504) shared by every REST client, change the defaults here and all planes inherit them.
    urllib3 ships with requests, but the ``Retry`` import stays guarded so a stripped environment
    degrades to no-retry rather than failing at construction.

    ``retries`` and ``backoff_factor`` are parametrized (Q2) so a caller can request ``retries=0``:
    a SINGLE attempt with no retry-multiplied timeout, the "one attempt, long timeout" shape this
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
    verification disabled) the session also pins ``trust_env=False``, otherwise a
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
        # Single attempt (Q2 "one attempt, long timeout"): reuse build_write_session's no-retry
        # adapter shape. No Retry import needed, requests builds Retry(total=0) from the int, so
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
            pass  # urllib3 retry unavailable, proceed without
    return session


_SHARED_POOL_MAXSIZE = 32
"""Connection-pool size for the cached read sessions (K5). Matches the default asyncio executor
width (``min(32, cpu+4)``) so that up to ~32 concurrent REST reads on one host reuse the pool
instead of each opening, and requests then discarding, its own connection (requests' default
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

    The five REST clients are re-instantiated on every tool-call, each inside its own ``_run()``
    closure dispatched via :func:`asyncio.to_thread`, so building a fresh session per ``__init__``
    paid a new TCP/TLS handshake on every call (no leak, pure waste). This memoises ONE session per
    distinct ``(accept, auth_header, verify, retries, backoff_factor)``. The session is NOT bound to
    a URL, so ``base_url`` is deliberately absent from the key: two same-auth clients (even for
    different service hosts) share one pooled session, and the pool keys connections per host
    itself. The cache is keyed on the *resolved* ``verify`` (below), and the adapter carries
    ``pool_maxsize`` = the executor width (:data:`_SHARED_POOL_MAXSIZE`).

    ``verify`` is resolved from config HERE, before the cache key, so a config change (a reload, or
    a test's monkeypatched ``get_config``) selects a DIFFERENT cache entry rather than serving a
    session built under the old TLS trust, the one correctness trap of caching a config-derived
    object. Sharing is safe across worker threads: the clients only READ their session
    (``.get``/``.head``/``.verify``), the urllib3 pools are thread-safe, and the one piece of
    per-request-MUTABLE state, the cookie jar, is DISABLED on these sessions (see
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

    * **No retry policy** (``max_retries=0``, no adapter carrying a ``Retry``). Olog ``PUT /logs``
      is NOT idempotent: every PUT mints a new entry. Under the read session's 3-retry policy a
      request the server PROCESSED but whose response was lost would be replayed into a DUPLICATE
      entry (urllib3's default ``allowed_methods`` retries PUT). A lost PUT thus surfaces as an
      error, an ``unknown`` outcome the caller must resolve by SEARCHING, never a blind retry,
      not a silent second entry.
    * **``trust_env=False`` always** (the read factory keeps it on at the plain default to preserve
      the zero-code ``REQUESTS_CA_BUNDLE`` path). The write session inherits NO ambient environment:
      no proxy / ``NO_PROXY`` / netrc, and no ``REQUESTS_CA_BUNDLE`` env. This closes N03, an
      inherited proxy can never carry the Basic ``Authorization`` header outward, and keeps the
      write deterministic. The cost falls only on a REMOTE https Olog (loopback needs neither): its
      internal CA must come from the ``EPICS_MCP_CA_BUNDLE`` config (the DS-1 chokepoint), not the
      env, and it is not reachable through an env proxy.

    ``verify`` resolves the same VALUE as the read factory (``ca_bundle`` > ``tls_verify`` > True);
    only the env fallbacks are dropped. Pass it explicitly (the Olog client passes its read
    session's already-resolved ``verify``) so the two sessions agree on that configured VALUE. But
    they do NOT necessarily trust the same EFFECTIVE CA: because this session drops env fallbacks
    (``trust_env=False``), a ``REQUESTS_CA_BUNDLE`` env CA is honoured only by the read session,
    and a remote-https write's CA must come from ``EPICS_MCP_CA_BUNDLE`` config (the N03 tradeoff,
    per :meth:`~epics_mcp.services.olog_client.OlogClient._write_session`).
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
    and over the limit it RAISES (never blocks), a blocking wait at this sync chokepoint would hold
    one of the shared worker threads and reintroduce exactly the K4 starvation the monitor bulkhead
    removes.

    Disabled by default (``limit <= 0``): :meth:`check` returns immediately with no lock and no
    allocation, so the posture stays opt-in, existing read behaviour is unchanged until
    an operator sets ``EPICS_MCP_READ_RATE_LIMIT``.
    """

    _WINDOW_SECONDS = 60.0

    def __init__(self, limit: int) -> None:
        self._limit = limit
        # maxlen only when enabled; a disabled throttle never appends, so unbounded is fine.
        self._timestamps: deque[float] = deque(maxlen=limit) if limit > 0 else deque()
        self._lock = threading.Lock()

    def check(self) -> None:
        """Admit one read, or raise :class:`ReadRateLimitError` if the sliding window is full.

        Purge → len-check → append is ONE atomic step under the lock (symmetric with the write
        gates), so two concurrent reads can never both pass and exceed the limit; ``now`` is sampled
        inside the lock, and the raise runs OUTSIDE it, the deny path never appends a token.

        The refusal carries ``READ_RATE_LIMIT_EXCEEDED``, NOT the write gates' own
        ``RATE_LIMIT_EXCEEDED``: this throttle is not a write gate, it writes no audit line, and it
        is reached from the reads the Olog write tools perform *before* their gate is consulted.
        Write-gate contract point 4 (CLAUDE.md) forbids a refusal raised outside a gate from
        carrying that gate's code, otherwise a throttled read would be reported to the caller
        exactly like an audited write DENY it never was."""
        if self._limit <= 0:
            return  # disabled, opt-in posture, no throttling, no lock taken
        with self._lock:
            now = time.monotonic()
            self._purge_old(now)
            over_limit = len(self._timestamps) >= self._limit
            if not over_limit:
                self._timestamps.append(now)  # record this read (admit path only)
        if over_limit:
            raise ReadRateLimitError(
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
    body, which modern requests surfaces as a ``RequestException``) raises *resp_exc*, the
    per-service subclasses of :class:`RestConnectionError` / :class:`RestResponseError`. The one
    debug log here is the single place a swallowed REST failure is recorded before the caller maps
    the exception to a withheld verdict or an ``EpicsError``.

    ``allow_redirects=False`` makes a redirect a *resp_exc* instead of a followed hop. It matters
    wherever the RESPONDING host, not the requested one, is what a security decision rests on: a
    redirect moves the data's true origin without changing the configured URL. A 3xx is not an HTTP
    error, so ``raise_for_status`` would wave it through, hence the explicit check.
    """
    get_read_throttle().check()  # S3 read throttle, no-op unless read_rate_limit > 0
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
    It matters even more on a write: a followed hop would post the body, and the auth header, to a
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
#: content_type))`` tuples, deliberately a list, never a dict: the Olog multipart carries several
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
    """Send a ``multipart/form-data`` body via *method* to *url* and return JSON, the shared core
    of :func:`rest_put_multipart` (create) and :func:`rest_post_multipart` (attach-to-existing).

    *files* is a LIST of ``(name, (filename, content, content_type))`` tuples (see
    :data:`MultipartFiles`). ``requests`` builds the multipart body from it AND sets the
    ``Content-Type: multipart/form-data; boundary=...`` header itself, so this passes **no** manual
    ``Content-Type`` (one would clobber the boundary and the server could not parse the body). This
    mirrors CS-Studio's own client, which builds the same body by hand
    (``HttpRequestMultipartBody``): a ``logEntry`` JSON part plus one ``files`` part per attachment.

    *headers* carries per-request headers (a static client-info header); it MUST NOT include
    ``Content-Type``: ``requests`` sets that (with the boundary) from *files*. Auth rides on the
    session. ``allow_redirects=False`` refuses a redirect rather than follow it (see
    :func:`rest_put_json`): on a write a followed hop would post the body, and the Basic auth
    header, to a host the gate never approved. Defaults to False because every Olog attachment
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
    /logs/multipart``), a thin :func:`_request_multipart` wrapper. See there for the contract."""
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
    /logs/multipart`` = the server's ``updateLog``), the POST sibling of
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
    ``Content-Disposition: attachment; filename=...``; the value is author free text, so callers
    gate it under the same posture as the bytes (a person can be named in a filename).
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
    manager, there is no other streaming caller in this module, so a leaked connection would be a
    silent regression. *max_bytes* caps the body (:func:`_read_body_capped`, a Content-Length over
    it
    is refused before any read, and the stream is accumulated only up to the cap); ``None`` = no
    cap,
    but the attachment caller always passes one, so a huge object is never materialised into memory.
    ``filename`` comes from ``Content-Disposition`` (Olog sends ``attachment; filename=...``) and
    ``content_type`` from the response header (Olog derives it from the file extension server-side
    and
    may omit it → ``None``); both are surfaced but a caller applies the download privacy gate first.

    ``allow_redirects=False`` refuses a redirect (see :func:`rest_get_json`): the download posture
    is
    decided from the CONFIGURED host, so a followed hop would let a loopback URL serve bytes from a
    real server. Defaults to False here because the whole attachment surface is host-gated."""
    get_read_throttle().check()  # S3 read throttle, no-op unless read_rate_limit > 0
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
    "reachable but the API answered with an error status" (a served 4xx/5xx, e.g. an Archiver URL
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
    ``inReplyTo`` that identifies no entry) with 400, distinct from "not found". Thin wrapper over
    :func:`http_status`.
    """
    return http_status(exc) == 400


def is_ssl_error(exc: BaseException) -> bool:
    """True iff *exc* wraps a TLS/CA verification failure.

    :func:`rest_get_json` and the clients' ``check_connectivity`` chain the original requests error
    via ``from exc``. ``requests.exceptions.SSLError`` (a subclass of ``ConnectionError``, hence
    otherwise indistinguishable from a plain unreachable host) signals a certificate / CA-bundle
    problem, the signal a config ``doctor`` needs to say "fix your CA bundle" rather than
    "host unreachable". Null-safe.
    """
    return isinstance(getattr(exc, "__cause__", None), requests.exceptions.SSLError)


def is_retry_error(exc: BaseException) -> bool:
    """True iff *exc* wraps a retry-exhausted 5xx response.

    :func:`build_retrying_session` force-lists 502/503/504, so a served-but-retryable 5xx that
    exhausts the retry budget surfaces as ``requests.exceptions.RetryError``, a RequestException
    that is NOT a ConnectionError and whose ``.response`` is ``None`` (so :func:`http_status` cannot
    read a code). It means the host DID answer (repeatedly, with a 5xx), so a config ``doctor``
    should report it as reachable-but-erroring, NOT "unreachable". Null-safe.
    """
    return isinstance(getattr(exc, "__cause__", None), requests.exceptions.RetryError)


#: What :func:`shown_url` prints when the address cannot be shown without its credentials. The same
#: token :func:`url_without_credentials` already returns, deliberately, rather than a fifth dialect:
#: a reader who meets it in a doctor line and in an error message meets one word, not two.
_ADDRESS_UNPARSEABLE = "(unparseable)"

#: What :func:`_shown_cause` prints for a configured value that is not a usable HTTP address. It
#: names the defect and echoes NOTHING, because the value is exactly what cannot be shown: urllib3
#: refuses these spellings, so :func:`url_without_userinfo` cannot prove any redaction of them, and
#: requests' own text for this family quotes the URL twice (once as a helpful suggestion).
_URL_SHAPE_CAUSE = (
    "the configured URL is not a usable HTTP address (no scheme, a scheme this client cannot use, "
    "or a malformed host). The value is not echoed here, because a credential can hide in a "
    "spelling the parser refuses; read it back from this process's environment"
)


def shown_url(url: str) -> str:
    """*url* as a client-facing MESSAGE may carry it: no userinfo, no query, no fragment.

    The message spelling of :func:`url_without_userinfo`'s answer, and it delegates to that one
    rather than to :func:`url_without_credentials` for a measured reason. The rebuilding sibling
    normalises, and on one real spelling it does worse than normalise: for
    ``https://svc:p@ss/w0rd@host/x`` urllib3 parses host ``ss`` and path ``/w0rd@host/x``, so the
    rebuild prints ``https://ss/w0rd@host/x``, a fragment of the password, in the path, carrying no
    ``@`` for any structural check to catch. A fallback that can leak is not a fallback, so there is
    none: what the deleting sibling cannot PROVE is printed as :data:`_ADDRESS_UNPARSEABLE`.

    The query and fragment go beyond what that sibling promises, and the cut is safe BECAUSE of what
    it already proved: the result parses and agrees with the original on all six components, so the
    first literal ``?`` or ``#`` is the real delimiter. A token in a query string is a normal thing
    to configure, and a message has no use for one: every caller in this module passes its query as
    ``params`` to requests, so a query in the printed string can only have come from the configured
    base URL. ``epics-pv://config`` deliberately KEEPS the query, because that surface exists to be
    compared character for character; this one exists to name an address.

    A cut that leaves nothing is not an address either, so it also yields the marker.
    """
    shown = url_without_userinfo(url)
    if shown is None:
        return _ADDRESS_UNPARSEABLE
    cut = min((at for at in (shown.find("?"), shown.find("#")) if at != -1), default=len(shown))
    return shown[:cut] or _ADDRESS_UNPARSEABLE


def _status_phrase(status: int) -> str:
    """`` Not Found`` for 404, and an empty string for a status no IANA table names.

    The phrase comes from :class:`http.HTTPStatus`, a CLIENT-side table, never from
    ``response.reason``. Two reasons, and the first is the one that always holds: ``reason`` is the
    responding server's own status line, so printing it lets a foreign host choose the text of a
    message this module promises is credential-free. The second is determinism: a proxy answering
    520 or 599 has no entry here at all, and ``HTTPStatus(599)`` raises rather than returning one.
    """
    try:
        return f" {HTTPStatus(status).phrase}"
    except ValueError:
        return ""


def shown_cause(exc: BaseException) -> str:
    """Why a request failed, in a text that provably carries no userinfo.

    Measured, and the whole design follows from it: requests hands urllib3 only ``path_url``, so a
    TRANSPORT failure (refused, DNS, either timeout, TLS, a body that is not JSON) reads
    ``HTTPConnectionPool(host=..., port=P): ... url: /path`` and carries no credential at all. That
    text is also the ONLY place "refused" is distinguishable from "not resolved" from "timed out"
    from a TLS failure, and ``doctor._REMEDY`` is static per status, so nothing supplies it later.
    It is therefore passed through VERBATIM, and withholding it wholesale would delete diagnosis
    without closing any leak.

    Two families do carry it, and each gets its own branch above the pass-through:
    ``raise_for_status`` builds ``f"{code} ... for url: {self.url}"`` from the PREPARED url, which
    keeps its userinfo; and the URL-shape errors are raised inside ``prepare_url`` before a request
    exists, quoting the configured value twice.

    ⚠️ The branches are ordered by specificity because ``RequestException`` subclasses ``IOError``,
    which IS ``OSError``: an ``OSError`` arm anywhere above these two would swallow both leaking
    families and print them raw. An earlier draft of this function had exactly that arm, and the
    surrounding comments in ``olog_client``/``naming_client`` had already recorded the inheritance.

    The final ``@`` check is the output-side verification, and it is what makes this function safe
    against a family nobody enumerated. It needs no knowledge of the secret, which is the point:
    requests rewrites the userinfo in flight (``requote_uri(unquote_unreserved(...))``, measured:
    ``s%65cret`` prints as ``secret`` and ``hun@ter2`` as ``hun%40ter2``), so a search for the
    CONFIGURED value finds nothing and reports a clean message that carries the password. What
    cannot be rewritten is the SEPARATOR: ``prepare_url`` builds ``netloc = auth + "@" + host`` and
    only then requotes, and ``@`` is in requote's safe set. A surviving userinfo therefore always
    brings a literal ``@``, whatever its parts now look like.

    The withheld branch logs at WARNING on purpose. A net that silently repairs makes the defect it
    repaired invisible, and the site that produced an unexpected ``@`` is worth finding.
    """
    if isinstance(
        exc,
        (
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
        ),
    ):
        return _URL_SHAPE_CAUSE
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            return f"HTTP {status}{_status_phrase(status)}"
        # No response object (a hand-built HTTPError, and dozens of tests build one): fall through
        # to the check below rather than inventing a code, since such a text has no url to leak.
    text = str(exc)
    if "@" in text:
        logger.warning(
            "withheld the cause text of a %s: it carried an '@', which a redacted address cannot",
            type(exc).__name__,
        )
        return f"{type(exc).__name__} (message withheld: it would echo a credential)"
    return text


def shown_failure(url: str, exc: BaseException) -> str:
    """``"<address>: <cause>"``, the pair every failing REST site prints, both halves redacted.

    A composer rather than a fourth idea: the showability of the address and the showability of the
    cause are one decision at every call site, and the ten ``raise conn_exc/resp_exc`` sites, the
    four ``check_connectivity`` bodies and the naming client's own HTTPError catch all want exactly
    this shape. Same role as :func:`is_http_404` over :func:`http_status`.
    """
    return f"{shown_url(url)}: {shown_cause(exc)}"


def route_label(base_url: str, url: str) -> str:
    """*url*'s route relative to *base_url*, for a message that must not name a host.

    The listing errors and the level notes name the endpoint that was ACTUALLY requested, and that
    is a measured requirement rather than decoration: S31 replaced hand-written literals here
    precisely because a swapped route produces a correctly-worded error about the wrong address,
    and one of the two labels had already fallen back to its literal with no test noticing. So the
    label stays DERIVED from the same expression the request used, and only its host part goes.

    Deriving the route by removing the base is safer than parsing it out. ``urlsplit`` would put a
    password's tail into ``.path`` for the spelling ``https://svc:p@ss/w0rd@host/x`` (the authority
    ends at the first slash), which is the same class :func:`shown_url` refuses to print. A prefix
    removal cannot produce characters the base did not already cover, so what remains is exactly
    the part beyond the configured base URL.

    Fails closed: if *url* does not start with *base_url* the removal is a no-op and the answer
    would BE the full url, so it is withheld instead. Every caller builds *url* as
    ``f"{self.base_url}/..."``, so that branch is unreachable today; it is one line rather than a
    promise that it stays that way.
    """
    route = url.removeprefix(base_url)
    return route if route and route != url else "(route withheld)"
