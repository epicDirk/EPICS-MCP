"""Tests for the shared REST substrate (services/_http) and the REST exception roots (C3).

Covers the M3/M12/L-Logger deduplication: one retry policy, one GET-and-translate, one root
exception hierarchy, and the single debug line that wakes the previously-dead REST logger.
"""

from __future__ import annotations

import ast
import logging
import random
import time
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from unittest.mock import MagicMock, Mock
from urllib.parse import urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url
from urllib3.util.retry import Retry

from epics_mcp.config import EpicsConfig
from epics_mcp.errors import RateLimitError, ReadRateLimitError
from epics_mcp.services._http import (
    ReadThrottle,
    _authority_span,
    build_retrying_session,
    build_write_session,
    get_read_throttle,
    get_shared_session,
    http_status,
    is_ca_bundle_error,
    is_http_404,
    is_https_url,
    is_loopback_url,
    is_read_throttle_error,
    is_retry_error,
    is_ssl_error,
    reset_read_throttle,
    rest_get_bytes,
    rest_get_json,
    rest_put_json,
    route_label,
    shown_cause,
    shown_failure,
    shown_url,
    url_host,
    url_without_userinfo,
)
from epics_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverError,
    ArchiverResponseError,
)
from epics_mcp.services.channelfinder_client import ChannelFinderClient
from epics_mcp.services.naming_client import NamingServiceClient
from epics_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


def _resp(payload: object, *, ok: bool = True) -> Mock:
    """A fake requests response returning *payload*; ok=False makes raise_for_status raise HTTP."""
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- build_retrying_session ---


def test_session_mounts_the_single_3_retry_policy() -> None:
    """The shared session carries the 3-retry/502-503-504 policy on both http and https (M3)."""
    session = build_retrying_session()
    for scheme in ("http://x", "https://x"):
        adapter = session.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        retries = adapter.max_retries
        assert isinstance(retries, Retry)
        assert retries.total == 3
        assert set(retries.status_forcelist or ()) == {502, 503, 504}
    assert session.headers["accept"] == "application/json"
    assert "authorization" not in session.headers


def test_session_forwards_optional_auth_header() -> None:
    session = build_retrying_session(auth_header="Bearer tok")
    assert session.headers["authorization"] == "Bearer tok"


def test_session_retries_zero_yields_single_attempt_default_still_three() -> None:
    """Q2: a caller may ask for ``retries=0``, meaning exactly ONE attempt and no timeout
    multiplied by the retry count. That case ("one attempt, long timeout") could not be expressed
    before. ``retries=0`` mirrors the no-retry adapter form of :func:`build_write_session`
    (``max_retries.total == 0`` on both schemes). The default stays at 3, so existing callers keep
    the shared 3-retry policy unchanged."""
    single = build_retrying_session(retries=0)
    for scheme in ("http://x", "https://x"):
        adapter = single.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        retries = adapter.max_retries
        total = retries.total if isinstance(retries, Retry) else retries
        assert total == 0
    default = build_retrying_session()
    for scheme in ("http://x", "https://x"):
        adapter = default.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        assert isinstance(adapter.max_retries, Retry)
        assert adapter.max_retries.total == 3


def test_session_retries_and_backoff_are_honoured_when_overridden() -> None:
    """Both knobs are parameterisable; the values land on the mounted ``Retry`` policy, while the
    shared 502/503/504 ``status_forcelist`` is preserved."""
    session = build_retrying_session(retries=5, backoff_factor=1.5)
    for scheme in ("http://x", "https://x"):
        adapter = session.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        retries = adapter.max_retries
        assert isinstance(retries, Retry)
        assert retries.total == 5
        assert retries.backoff_factor == 1.5
        assert set(retries.status_forcelist or ()) == {502, 503, 504}


# --- TLS verify resolution at the single chokepoint (DS-1) ---


def test_verify_kwarg_true_keeps_default_and_trust_env() -> None:
    """An explicit ``verify=True`` mirrors the plain default: verify on, trust_env untouched so the
    zero-code REQUESTS_CA_BUNDLE env path still works."""
    session = build_retrying_session(verify=True)
    assert session.verify is True
    assert session.trust_env is True


def test_verify_kwarg_ca_path_sets_verify_and_pins_trust_env() -> None:
    """A CA-bundle path is applied AND trust_env is pinned off so a REQUESTS_CA_BUNDLE env var
    cannot override the explicit bundle via requests' per-request environment merge."""
    session = build_retrying_session(verify="ess-root-ca.pem")
    assert session.verify == "ess-root-ca.pem"
    assert session.trust_env is False


def test_verify_kwarg_false_disables_and_pins_trust_env() -> None:
    """tls_verify=False must actually disable verification, pinning trust_env off is what makes the
    escape hatch real even when REQUESTS_CA_BUNDLE is set in the environment."""
    session = build_retrying_session(verify=False)
    assert session.verify is False
    assert session.trust_env is False


def test_verify_resolves_ca_bundle_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no kwarg the chokepoint reads ITS OWN get_config
    (epics_mcp.services._http.get_config, NOT checkers.get_config) → ca_bundle path wins and
    trust_env is pinned off."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    session = build_retrying_session()
    assert session.verify == "ca.pem"
    assert session.trust_env is False


def test_verify_resolves_tls_verify_false_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="", tls_verify=False),
    )
    session = build_retrying_session()
    assert session.verify is False
    assert session.trust_env is False


def test_verify_default_config_verifies_and_keeps_trust_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plain default (ca_bundle empty, tls_verify True) verifies against certifi and leaves
    trust_env on, the zero-code REQUESTS_CA_BUNDLE path stays available."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="", tls_verify=True),
    )
    session = build_retrying_session()
    assert session.verify is True
    assert session.trust_env is True


# --- build_write_session (S23: the dedicated Olog write session) ---


def test_write_session_has_no_retry_adapter() -> None:
    """S23/F06: the Olog write session must NOT blindly retry. Olog ``PUT /logs`` is NOT idempotent
    (each PUT mints a new entry), so a request the server PROCESSED but whose response was lost
    would, under the read session's 3-retry policy, be replayed into a DUPLICATE log entry.
    ``max_retries.total == 0`` on both schemes, a lost PUT surfaces as an error, never a retry."""
    session = build_write_session()
    for scheme in ("http://x", "https://x"):
        adapter = session.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        retries = adapter.max_retries
        total = retries.total if isinstance(retries, Retry) else retries
        assert total == 0


def test_write_session_pins_trust_env_off_even_on_plain_default() -> None:
    """S23/N03: the write session is deliberately ENV-INDEPENDENT (no proxy / netrc /
    REQUESTS_CA_BUNDLE env) so an inherited proxy can never carry the Basic ``Authorization`` header
    outward. Unlike the READ factory (which keeps trust_env on at the plain default to preserve the
    zero-code REQUESTS_CA_BUNDLE path), trust_env is off even when ``verify is True``."""
    assert build_write_session(verify=True).trust_env is False


def test_write_session_carries_optional_auth_header() -> None:
    """The write session must actually carry the auth header, the write NEEDS it. A silent drop
    would 401 the server, and against an auth-less loopback sandbox it would pass unnoticed."""
    session = build_write_session(auth_header="Basic dXNlcjpwYXNz")
    assert session.headers["authorization"] == "Basic dXNlcjpwYXNz"
    assert session.headers["accept"] == "application/json"


def test_channelfinder_client_session_inherits_ca_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-1 acceptance 3(c): the CF client (which CFRegistryChecker builds for crossplane/coverage)
    inherits the configured CA via the build_retrying_session chokepoint. A future refactor that
    constructed its session another way would fail this anti-regression guard."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    client = ChannelFinderClient("http://cf", auth_header=None)
    assert client.session.verify == "ca.pem"
    assert client.session.trust_env is False


def test_naming_client_session_inherits_ca_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-1 acceptance 3(c): the Naming client that diagnose._gather_naming constructs DIRECTLY
    (bypassing the checkers factory) still inherits the CA, the chokepoint covers it too."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    client = NamingServiceClient(base_url="http://naming")
    assert client.session.verify == "ca.pem"
    assert client.session.trust_env is False


# --- get_shared_session (K5: connection reuse across per-call client instances) ---


def test_two_clients_reuse_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """K5: the five REST clients are rebuilt on EVERY tool-call (each in its own ``_run()`` thread),
    so a fresh ``requests.Session`` per ``__init__`` paid a new TCP/TLS handshake each time. Two
    independently constructed clients with the same ``(auth, verify)`` must now share ONE cached
    session, the point of the connection-reuse change. RED before K5 (each ``__init__`` built its
    own session, so the two were distinct instances)."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(),
    )
    first = ChannelFinderClient("http://cf", auth_header=None)
    second = ChannelFinderClient("http://cf", auth_header=None)
    assert first.session is second.session


def test_shared_session_differs_by_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different auth headers must NOT collapse onto one session, a client sending a Basic header
    and a no-auth client sharing one session would leak the credential to the wrong host."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(),
    )
    no_auth = get_shared_session(auth_header=None)
    with_auth = get_shared_session(auth_header="Basic dXNlcjpwYXNz")
    assert no_auth is not with_auth
    assert "authorization" not in no_auth.headers
    assert with_auth.headers["authorization"] == "Basic dXNlcjpwYXNz"


def test_shared_session_has_pooled_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached session's adapter is pooled to the executor width (32) so the ~32-thread REST
    fan-out reuses connections instead of discarding one per over-the-limit request (requests'
    default pool_maxsize is 10). Both schemes carry it."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(),
    )
    session = get_shared_session(auth_header=None)
    for scheme in ("http://x", "https://x"):
        adapter = session.get_adapter(scheme)
        assert isinstance(adapter, HTTPAdapter)
        assert adapter._pool_maxsize == 32


def test_shared_session_reresolves_verify_on_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache key uses the RESOLVED verify, so a config change selects a DIFFERENT entry rather
    than serving a session built under the old TLS trust, the one correctness trap of caching a
    config-derived object. Same call, two CA bundles → two sessions."""
    target = "epics_mcp.services._http.get_config"
    monkeypatch.setattr(target, lambda: EpicsConfig(ca_bundle="a.pem"))
    first = get_shared_session(auth_header=None)
    monkeypatch.setattr(target, lambda: EpicsConfig(ca_bundle="b.pem"))
    second = get_shared_session(auth_header=None)
    assert first is not second
    assert first.verify == "a.pem"
    assert second.verify == "b.pem"


def test_shared_session_blocks_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """K5 hardening: the shared session is hit concurrently from worker threads, and a requests
    cookie jar is the one per-request-MUTABLE shared state, a Set-Cookie mutating it while another
    thread iterates it in prepare_request races (RuntimeError: dictionary changed size). These are
    stateless REST reads, so the shared session blocks all cookie storage via a DefaultCookiePolicy
    with an EMPTY allowed_domains (set_ok is False for every domain). RED before the policy: a fresh
    jar's policy has allowed_domains=None (allow)."""
    monkeypatch.setattr("epics_mcp.services._http.get_config", lambda: EpicsConfig())
    session = get_shared_session(auth_header=None)
    policy = session.cookies._policy
    assert isinstance(policy, DefaultCookiePolicy)
    # allowed_domains() returns the empty tuple when blocking every domain; a default jar returns
    # None (allow), so this fails without the block-all policy.
    assert policy.allowed_domains() == ()


# --- rest_exceptions root ---


def test_per_service_errors_derive_from_the_shared_roots() -> None:
    """Each per-service error is a RestClientError; conn/resp map to the matching root (C3)."""
    assert isinstance(ArchiverConnectionError(""), RestClientError)
    assert isinstance(ArchiverConnectionError(""), RestConnectionError)
    assert isinstance(ArchiverResponseError(""), RestResponseError)
    assert isinstance(ArchiverResponseError(""), RestClientError)
    # The per-service base still catches only its own subclasses (no behaviour regression).
    assert isinstance(ArchiverConnectionError(""), ArchiverError)


# --- rest_get_json ---


def test_rest_get_json_returns_parsed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_retrying_session()
    monkeypatch.setattr(session, "get", Mock(return_value=_resp([{"ok": 1}])))
    data = rest_get_json(
        session,
        "http://x",
        None,
        1.0,
        conn_exc=ArchiverConnectionError,
        resp_exc=ArchiverResponseError,
    )
    assert data == [{"ok": 1}]


def test_rest_get_json_connection_error_logs_once_and_raises_conn_exc(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ConnectionError → conn_exc, and EXACTLY ONE debug line is emitted (dead logger woke)."""
    session = build_retrying_session()
    monkeypatch.setattr(
        session, "get", Mock(side_effect=requests.exceptions.ConnectionError("down"))
    )
    with (
        caplog.at_level(logging.DEBUG, logger="epics_mcp.services._http"),
        pytest.raises(ArchiverConnectionError),
    ):
        rest_get_json(
            session,
            "http://x",
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )
    debug_lines = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug_lines) == 1


def test_rest_get_json_http_error_raises_resp_exc(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_retrying_session()
    monkeypatch.setattr(session, "get", Mock(return_value=_resp(None, ok=False)))
    with pytest.raises(ArchiverResponseError):
        rest_get_json(
            session,
            "http://x",
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )


def test_rest_get_json_refuses_redirect_with_neutral_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F20 (S21, under S12): a refused redirect (allow_redirects=False) must NOT claim "different
    host", that is objectively false for a same-origin redirect (http→https, trailing slash). The
    message names a redirect TARGET, not a host.

    Red-proof: match="redirect target" reds the pre-fix "different host" wording.
    """
    session = build_retrying_session()
    redirect = Mock()
    redirect.is_redirect = True
    redirect.status_code = 302
    monkeypatch.setattr(session, "get", Mock(return_value=redirect))
    with pytest.raises(ArchiverResponseError, match="redirect target"):
        rest_get_json(
            session,
            "http://x",
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
            allow_redirects=False,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://svc:p@ss/w0rd@service.example.org/CF",
        "https://svc:hun@ter2@service.example.org/CF",
        "https://loneuser@service.example.org/CF",
        "https://service.example.org/CF?token=t0ken",
    ],
    ids=["at-and-slash-in-password", "at-in-password", "bare-username", "query-token"],
)
def test_the_shared_barrier_leaks_no_secret_on_any_failing_route(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """The barrier every plane crosses, driven with a base URL that CARRIES a secret.

    ⚠️ This condition was not created by any test until 2026-08-14, and the omission mattered
    beyond coverage: four shipped documents cite this barrier as the reason ``epics-doctor`` no
    longer needs a redaction of its own. The property they lean on was argued from the call graph.
    Here it is exercised, on both failure shapes ``rest_get_json`` distinguishes, since they build
    their messages through different helpers (``shown_failure`` versus ``shown_url`` plus
    ``shown_cause``).

    The first row is the spelling that broke the REBUILDING redaction, kept here because this
    barrier delegates to the DELETING one and must not acquire the same hole by a later edit.

    Red-proof: point ``shown_url`` at ``url_without_credentials`` and row one fails on ``w0rd``;
    interpolate ``url`` instead of ``shown_url(url)`` at either raise site and every row fails.
    """
    secrets = ("p@ss", "w0rd", "hun@ter2", "ter2", "loneuser", "t0ken")
    session = build_retrying_session()

    for outcome in (
        Mock(side_effect=requests.exceptions.ConnectionError("refused")),
        Mock(return_value=_resp(None, ok=False)),
    ):
        monkeypatch.setattr(session, "get", outcome)
        with pytest.raises((ArchiverConnectionError, ArchiverResponseError)) as raised:
            rest_get_json(
                session,
                f"{base_url}/resources/channels",
                None,
                1.0,
                conn_exc=ArchiverConnectionError,
                resp_exc=ArchiverResponseError,
            )
        message = str(raised.value)
        for secret in secrets:
            assert secret not in message, f"{base_url} -> {message}"
        assert "@" not in message, f"an address-shaped '@' survived: {message}"


def test_rest_put_json_refuses_redirect_with_neutral_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F20 on the WRITE path: the same "different host" wording is equally wrong for a same-origin
    redirect. Behaviour (refuse the redirect) is unchanged, only the message text.

    Red-proof: match="redirect target" reds the pre-fix "different host" wording.
    """
    session = build_retrying_session()
    redirect = Mock()
    redirect.is_redirect = True
    redirect.status_code = 307
    monkeypatch.setattr(session, "put", Mock(return_value=redirect))
    with pytest.raises(ArchiverResponseError, match="redirect target"):
        rest_put_json(
            session,
            "http://x",
            {"a": 1},
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
            allow_redirects=False,
        )


# --- CA / HTTP-status cause predicates (E2 doctor classifier) ---


def test_is_ssl_error_detects_chained_sslerror() -> None:
    """A chained requests.SSLError (the CA-bundle signal) is recognised."""
    err = ArchiverResponseError("x")
    err.__cause__ = requests.exceptions.SSLError("self-signed certificate in chain")
    assert is_ssl_error(err) is True


def test_is_ssl_error_false_for_plain_connection_and_no_cause() -> None:
    """A plain ConnectionError (not SSL) and a cause-less error are NOT CA failures."""
    conn = ArchiverConnectionError("x")
    conn.__cause__ = requests.exceptions.ConnectionError("connection refused")
    assert is_ssl_error(conn) is False
    assert is_ssl_error(ArchiverConnectionError("x")) is False  # no __cause__


def test_an_unreadable_ca_bundle_is_recognised_from_the_real_library() -> None:
    """The one text-matching predicate in this module, held against ``requests`` itself.

    It matches TEXT because nothing else survives the raise: measured here, not assumed, the
    exception carries no ``errno``, no ``filename``, no ``__cause__``, and its type is plain
    ``OSError``. So the wording IS the contract, and this test is what makes that safe: it drives
    the real library rather than building the exception by hand, so an upgrade that rewords the
    message turns this red instead of silently reclassifying every https plane back to
    "unreachable".

    Red-proof: change one word of ``_CA_BUNDLE_MESSAGE`` and this fails; build the exception by
    hand instead and the test passes while the production path stops working, which is the whole
    reason it is written this way.
    """
    session = requests.Session()
    session.verify = "/home/svc@site.example/certs/ca.pem"
    with pytest.raises(OSError) as raised:
        session.get("https://service.example.org/probe", timeout=1)

    exc = raised.value
    assert not isinstance(exc, requests.exceptions.RequestException), (
        "the premise of the whole branch: this escapes rest_get_json's except clause"
    )
    assert (exc.errno, exc.filename, exc.__cause__) == (None, None, None)
    assert is_ca_bundle_error(exc) is True


def test_a_ca_bundle_failure_is_recognised_through_a_wrapper_and_names_no_path() -> None:
    """Both arrival shapes, and the redaction that has to hold for both.

    Two shapes because the planes differ and the difference is measured: the four whose transport
    probe is a direct ``session.head`` catch this under ``except OSError`` and re-raise their own
    exception ``from`` it, so it arrives on ``__cause__``; the archiver and archiver_retrieval
    planes reach the doctor with the bare ``OSError``, because ``rest_get_json`` catches
    ``RequestException`` only.

    The path must not travel in either. It is not a credential, it is the other disclosure class
    this repository forbids in output, an account name in a filesystem path, and the original
    ``OSError`` puts it in the message.

    ⚠️ TWO path shapes, and the second one is the whole reason this test is not satisfied by the
    guard that preceded it. A bundle path containing an ``@`` is withheld by the generic ``@``
    branch even with this branch deleted, so it proves the OLD net rather than the new one; only a
    path WITHOUT an ``@`` reaches the new branch as the only thing standing between the message and
    the output. Measured: with the branch removed, ``/etc/ssl/certs/ca-bundle.crt`` is echoed in
    full. The common shape of a real bundle path has no ``@`` at all, so testing only the ``@``
    shape would have been a guard that cannot go red for the case it exists for.

    Red-proof: drop the ``cause`` half of the predicate and the wrapper rows fail; delete the
    branch in ``shown_cause`` and the ``EPICS_MCP_CA_BUNDLE`` assertion fails on every row while
    the no-``@`` row additionally fails on its path assertion.
    """
    for path in ("/home/svc@site.example/certs/ca.pem", "/etc/ssl/certs/ca-bundle.crt"):
        raw = OSError(f"Could not find a suitable TLS CA certificate bundle, invalid path: {path}")
        wrapped = ArchiverConnectionError("Failed to connect to Archiver at http://arch:17665")
        wrapped.__cause__ = raw

        assert is_ca_bundle_error(raw) is True, path
        assert is_ca_bundle_error(wrapped) is True, path

        for exc in (raw, wrapped):
            shown = shown_cause(exc)
            assert path not in shown, shown
            assert "EPICS_MCP_CA_BUNDLE" in shown, shown

    assert is_ca_bundle_error(ArchiverConnectionError("timed out")) is False


def test_http_status_reads_chained_response_code() -> None:
    """A served non-2xx surfaces its status; is_http_404 stays a thin wrapper over it."""
    err = ArchiverResponseError("x")
    http_error = requests.exceptions.HTTPError("404")
    http_error.response = Mock(status_code=404)
    err.__cause__ = http_error
    assert http_status(err) == 404
    assert is_http_404(err) is True


def test_http_status_none_for_transport_failure() -> None:
    """A transport failure has no chained .response → None (unreachable, not a served error)."""
    conn = ArchiverConnectionError("x")
    conn.__cause__ = requests.exceptions.ConnectionError("refused")
    assert http_status(conn) is None
    assert is_http_404(conn) is False


def test_is_retry_error_detects_retryerror() -> None:
    """A retry-exhausted 5xx (502/503/504 force-listed) surfaces as a chained RetryError."""
    err = ArchiverResponseError("x")
    err.__cause__ = requests.exceptions.RetryError("too many 503 error responses")
    assert is_retry_error(err) is True
    # RetryError has no .response, so http_status can't read a code, hence is_retry_error exists.
    assert http_status(err) is None


def test_is_retry_error_false_for_others() -> None:
    conn = ArchiverConnectionError("x")
    conn.__cause__ = requests.exceptions.ConnectionError("refused")
    assert is_retry_error(conn) is False
    assert is_retry_error(ArchiverConnectionError("x")) is False  # no __cause__


# ----------------------------------------------------------------------------------------------
# is_loopback_url, the shared "is this a local test server?" primitive
#
# Extracted from the Olog write gate so a caller asking "is this a local test server?" gets the
# SAME hardened host extraction without inheriting the gate's POLICY (`_url_write_allowed` also
# returns True for an allowlisted REMOTE host, which is a write permission, not a locality claim).
# Its second consumer was the Olog read redaction, removed 2026-08-01 (decision PI); the write
# gate's loopback branch is what remains.
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/Olog",
        "http://LOCALHOST:8080/Olog",  # host is lowercased
        "http://localhost./Olog",  # a fully-qualified trailing dot still resolves to localhost
        "http://127.0.0.1:8080/Olog",
        "http://127.0.0.2/Olog",  # the whole 127.0.0.0/8 block is loopback
        "http://[::1]:8080/Olog",  # IPv6 loopback; brackets stripped by urlparse().hostname
        "https://localhost/Olog",
    ],
)
def test_is_loopback_url_true_for_local_test_servers(url: str) -> None:
    assert is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://olog.example.org/Olog",  # a plain hostname is not an IP literal
        "http://olog:8080/Olog",  # the docker-compose service name of the local sandbox
        # RFC1918 PRIVATE is NOT loopback: a production service lives on a private network.
        "http://10.0.0.5/Olog",
        "http://192.168.1.10/Olog",
        "http://172.16.0.1/Olog",
        "http://0.0.0.0/Olog",  # the wildcard bind address is not loopback either
    ],
)
def test_is_loopback_url_false_for_remote_hosts(url: str) -> None:
    assert is_loopback_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1@evil.example.org/Olog",  # userinfo: the HOST is evil.example.org
        "http://localhost@evil.example.org/Olog",
        # Backslash in the authority: urlparse splits at the LAST '@' and calls 127.0.0.1 the host,
        # but urllib3, the parser requests actually CONNECTS with, resolves evil.example.org.
        # Whoever decides must use the parser that connects, or the decision describes a different
        # server than the one on the wire.
        "http://evil.example.org:8080\\@127.0.0.1/Olog",
        "http://[::1]./Olog",  # malformed bracketed IPv6 -> urlparse raises ValueError
        "http://[::1/Olog",
        "",  # hostless / garbage
        "not-a-url",
        "http:///Olog",
        "///Olog",
    ],
)
def test_is_loopback_url_fails_closed_on_hostile_or_malformed(url: str) -> None:
    """Anything unparseable or spoofed resolves to NOT-loopback.

    Same boolean direction as the write gate: False -> restrict, i.e. for a write, "deny". A
    spoofed authority must never widen what the caller may do, so fail-closed and fail-safe agree
    here and no inversion is needed.
    """
    assert is_loopback_url(url) is False


@pytest.mark.parametrize("url", ["http://./Olog", "http://.../Olog", "", "garbage", "http:///x"])
def test_url_host_returns_none_for_everything_unparseable(url: str) -> None:
    """url_host must return None, never "", for anything without a usable host.

    Callers use ``url_host(url) is None`` as a hard veto (the write gate denies such a URL even when
    it is exactly allowlisted). An empty string slips past that identity check: "http://./Olog" has
    hostname "." which survives a falsiness test and only becomes "" after the trailing-dot strip.
    Normalise first, THEN decide emptiness.
    """
    assert url_host(url) is None


def test_url_host_agrees_with_the_parser_that_connects() -> None:
    """url_host must name the host requests would actually reach, not a different one.

    urllib3 is what requests connects with. Where the two parsers disagree (a backslash in the
    authority), a decision built on urlparse describes a server other than the one on the wire:
    so the primitive uses urllib3's answer, and refuses when they cannot agree.
    """
    hostile = "http://evil.example.org:8080\\@127.0.0.1/Olog"
    assert urlparse(hostile).hostname == "127.0.0.1"  # what urlparse claims...
    assert parse_url(hostile).host == "evil.example.org"  # ...and where the connection would go
    assert url_host(hostile) == "evil.example.org"  # we follow the connection, not the claim
    assert is_loopback_url(hostile) is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://olog.example.org/Olog", True),
        ("HTTPS://olog.example.org/Olog", True),  # scheme is lowercased
        ("http://olog.example.org/Olog", False),
        ("http://localhost:8080/Olog", False),  # loopback http is still not https
        ("ftp://x/y", False),
        ("garbage", False),  # fail-closed on unparseable
        ("", False),
    ],
)
def test_is_https_url(url: str, expected: bool) -> None:
    """The write gate's remote-lane scheme check: https only, fail-closed on unparseable input."""
    assert is_https_url(url) is expected


# --- ReadThrottle (S3: opt-in read rate limit at the shared GET chokepoint) ---


def test_rest_get_json_throttled_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 at the chokepoint: with ``read_rate_limit=2`` the 3rd ``rest_get_json`` in the window is
    DENIED with ``ReadRateLimitError``, protecting the facility from an unthrottled read burst. RED
    before the chokepoint is wired (rest_get_json ignored the throttle → the 3rd read passed)."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(read_rate_limit=2),
    )
    reset_read_throttle()
    session = Mock()
    session.get.return_value = _resp({"ok": True})

    def _call() -> object:
        return rest_get_json(
            session,
            "http://svc",
            None,
            5.0,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
        )

    _call()
    _call()
    # Still catchable as RateLimitError (ReadRateLimitError subclasses it, so existing handlers
    # keep working) but reported under its OWN code.
    with pytest.raises(RateLimitError) as exc_info:
        _call()
    assert isinstance(exc_info.value, ReadRateLimitError)
    # The read denial carries the machine-readable contract callers key on, and it is deliberately
    # NOT the write gates' RATE_LIMIT_EXCEEDED: this throttle is not a write gate, writes no audit
    # line, and sits on reads the Olog write tools perform BEFORE their gate is consulted. The
    # write-gate contract (docs/write-gate-contract.md, point 4) forbids a refusal raised outside a
    # gate from carrying that gate's code.
    assert exc_info.value.error_code == "READ_RATE_LIMIT_EXCEEDED"
    assert exc_info.value.details == {"limit": 2, "window_seconds": 60.0}


def test_rest_get_json_unthrottled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``read_rate_limit=0`` → the throttle is disabled → any number of reads pass. The
    posture is opt-in, so a facility's existing read behaviour is unchanged until an operator sets
    it."""
    monkeypatch.setattr("epics_mcp.services._http.get_config", lambda: EpicsConfig())
    reset_read_throttle()
    session = Mock()
    session.get.return_value = _resp({"ok": True})
    for _ in range(50):
        rest_get_json(
            session,
            "http://svc",
            None,
            5.0,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
        )
    assert session.get.call_count == 50  # all 50 reads went through, no throttling


def test_rest_get_bytes_shares_the_read_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OTHER GET chokepoint counts against the SAME budget, attachment/byte reads are throttled
    too, not just ``rest_get_json``. With the single token already spent, ``rest_get_bytes`` raises
    before it ever issues the request."""
    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(read_rate_limit=1),
    )
    reset_read_throttle()
    get_read_throttle().check()  # spend the one token
    session = Mock()
    with pytest.raises(RateLimitError):
        rest_get_bytes(
            session,
            "http://svc",
            5.0,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
        )
    session.get.assert_not_called()  # throttle fired before any network call


def test_is_read_throttle_error_recognises_the_shape_the_throttle_actually_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BG-DTHR: the predicate is held against a refusal the CHOKEPOINT produced, not a fixture.

    That matters more than it looks. The whole reason a predicate exists instead of a translating
    ``except`` clause is that ``ReadRateLimitError`` is not a ``RequestException``, so it leaves
    ``rest_get_json`` UNWRAPPED, and a fixture-built exception would prove nothing about that
    claim. The first assertion pins the claim itself; a day when the throttle starts wrapping its
    refusal turns the second one red rather than leaving a caller quietly unable to recognise it.

    Red-proof: make ``ReadRateLimitError`` a ``RequestException`` subclass -> assertion 1;
    invert the predicate -> assertion 2.
    """
    assert not issubclass(ReadRateLimitError, requests.exceptions.RequestException)

    monkeypatch.setattr(
        "epics_mcp.services._http.get_config",
        lambda: EpicsConfig(read_rate_limit=1),
    )
    reset_read_throttle()
    get_read_throttle().check()  # spend the one token, the next read is refused
    session = Mock()
    with pytest.raises(ReadRateLimitError) as excinfo:
        rest_get_json(
            session,
            "http://svc",
            None,
            5.0,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
        )
    assert is_read_throttle_error(excinfo.value)


def test_is_read_throttle_error_also_reads_a_chained_refusal() -> None:
    """The cause direction, the shape ``is_ca_bundle_error`` uses. No caller chains this refusal
    today, and that is exactly why the direction is pinned: a future one that does must not drop
    out of the answer silently, which is the failure mode this whole ticket is about."""
    throttled = ReadRateLimitError("Read rate limit exceeded (1 reads per 60s). Try again later.")
    try:
        raise RestResponseError("Request failed") from throttled
    except RestResponseError as chained:
        assert is_read_throttle_error(chained)


@pytest.mark.parametrize(
    ("label", "exc"),
    [
        ("a write gate's rate-limit denial", RateLimitError("Rate limit exceeded")),
        ("a transport failure", requests.exceptions.ConnectionError("refused")),
        ("a TLS failure", requests.exceptions.SSLError("bad certificate")),
        ("an unreadable CA bundle", OSError("Could not find a suitable TLS CA certificate bundle")),
        ("an unreadable body", ValueError("Expecting value")),
    ],
)
def test_is_read_throttle_error_is_false_for_every_other_failure(
    label: str, exc: Exception
) -> None:
    """The negative half, and the FIRST row is the one that pays for the rest.

    ``ReadRateLimitError`` SUBCLASSES ``RateLimitError`` (so an existing ``except RateLimitError``
    keeps working), which makes the write gates' own denial the one exception that could be read as
    this one by an ``isinstance`` written in the wrong direction. Reading it that way would report
    an audited write-gate DENY as a throttled read, precisely the conflation the two separate error
    codes exist to prevent.

    Red-proof: swap the ``isinstance`` arguments, or widen it to ``RateLimitError`` -> row 1.
    """
    assert not is_read_throttle_error(exc), label


def test_read_throttle_window_slides() -> None:
    """The window slides, a read older than 60 s is purged, so the budget refills over time rather
    than being a permanent counter. Seed one stale timestamp directly (deterministic, no clock
    mocking): the next check purges it and admits, and only then is the window full again."""
    throttle = ReadThrottle(1)
    throttle._timestamps.append(time.monotonic() - throttle._WINDOW_SECONDS - 1.0)
    throttle.check()  # stale token purged → admitted
    with pytest.raises(RateLimitError):
        throttle.check()  # window is full again


def test_a_disabled_throttle_refuses_nothing_and_counts_nothing() -> None:
    """BG-DTHR: the opt-in posture reaches the counter too.

    ``denials`` exists so a caller can ask "did this run get everything it asked for?", and the
    answer on the shipping default (``read_rate_limit=0``) has to be a flat no-refusals, or every
    such caller would have to special-case the disabled throttle itself.

    Red-proof: count before the ``limit <= 0`` early return.
    """
    throttle = ReadThrottle(0)
    for _ in range(50):
        throttle.check()
    assert throttle.denials == 0


def test_the_throttle_counts_a_refusal_and_only_a_refusal() -> None:
    """BG-DTHR: ``denials`` moves on the deny path and stands still on the admit path.

    Both halves in one test on purpose: a counter that moved on every check would satisfy any
    assertion that only ever looks at the denied case, and it would then report a healthy run as
    incomplete, which is the false alarm the doctor's own status sets are shaped to avoid.

    Red-proof: increment on the admit path -> the first assertion; drop the increment -> the
    second and third.
    """
    throttle = ReadThrottle(2)
    throttle.check()
    throttle.check()
    assert throttle.denials == 0, "an admitted read must not count as a refusal"

    with pytest.raises(ReadRateLimitError):
        throttle.check()
    assert throttle.denials == 1

    with pytest.raises(ReadRateLimitError):
        throttle.check()
    assert throttle.denials == 2, "each refusal counts once"


def test_the_denial_count_is_monotonic_so_a_caller_reads_a_delta() -> None:
    """BG-DTHR: the counter is never reset, so a caller brackets its own work and subtracts.

    This is the contract ``run_doctor`` depends on. A resettable counter would be shared mutable
    state two callers could clear from under each other; a delta needs no coordination. The window
    sliding is what makes the second half measurable without a clock: the stale token is purged, so
    the read AFTER it is admitted while the count from before stays put.

    Red-proof: reset the counter in ``check``, or in ``_purge_old``.
    """
    throttle = ReadThrottle(1)
    throttle.check()
    with pytest.raises(ReadRateLimitError):
        throttle.check()
    before = throttle.denials
    assert before == 1

    throttle._timestamps.clear()
    throttle._timestamps.append(time.monotonic() - throttle._WINDOW_SECONDS - 1.0)
    throttle.check()  # stale token purged → admitted, and the count must not move
    assert throttle.denials - before == 0


# ----------------------------------------------------------------------------------------------
# url_without_userinfo, the redaction epics-pv://config prints its three service URLs through
#
# The sibling url_without_credentials is tested in test_doctor.py, under "the printed Olog target".
# It has NO caller any more, and the difference between the two is why: that one rebuilds (and
# normalises), this one deletes (and withholds what it cannot prove), because a payload a client
# compares against its own configuration must not be reworded. The rebuild turned out to carry a
# password fragment into the PATH on the spelling ``https://svc:p@ss/w0rd@host/x`` (host parses as
# ``ss``), so its last caller, the Olog write-gate block, moved to ``shown_url``. Its test keeps
# that row pinned; do not give it a new caller without reading it.
#
# That last sentence was prose and nothing enforced it (BG-DEAD). The two guards below do, and the
# reason the function is guarded rather than deleted is written where it lives, in its own
# docstring: five red-proof RECIPES in four test files name it as their measured counter-example.
# ⚠️ Recipes, not assertions. Measured over the whole repository, the only line that CALLS this
# function is the assertion of its own table test; the five are docstring sentences a reader
# re-cooks by editing source. That is still a reason to keep it (a recipe that says "point X at
# this function" needs the function to exist), but it is a weaker one than "five executable
# instructions", which is what this comment and the commit that added it first claimed.
# ----------------------------------------------------------------------------------------------


#: What ``src/`` may name but must not CALL. A tuple with one entry today, so that a second retired
#: function costs a line here rather than a second guard.
_RETIRED_WITHOUT_CALLER: tuple[str, ...] = ("url_without_credentials",)

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "epics_mcp"


def _code_references(source: str, name: str) -> list[tuple[int, str]]:
    """Every place *source* refers to *name* AS CODE, as ``(line, form)``.

    Three node kinds, because each is a way a caller could arrive: a bare ``Name`` (a call, or an
    alias assignment that defers one), an ``Attribute``
    (``_http.url_without_credentials(...)``), and an import ``alias``, which catches the
    ``as``-spelling too because that one binds under ``alias.name``. An import is included
    deliberately even though importing is not calling: outside the defining module nothing has a
    reason to import a function it may not use, and inside it the ``Name`` arm is what fires.

    ⚠️ ``alias.asname`` is deliberately NOT matched, and it was, until the post-build QA asked for
    a control that proved it. Measured, matching it is a FALSE-POSITIVE source rather than extra
    coverage: ``from foo import bar as url_without_credentials`` binds a completely different
    function under this name and would have been reported as a caller, while the case it was meant
    to catch is already covered twice over, once by ``alias.name`` at the import and once by the
    ``Name`` node at the call.

    A ``def`` of that name contributes nothing, and that is a property of the grammar rather than
    a special case here: ``FunctionDef`` carries its name as a plain string, so the definition
    site produces no node this walk can see.

    ⚠️ Prose is invisible to it, and the whole no-allowlist design rests on that: the FIVE
    docstring and comment cross-references to the current entry live inside ``ast.Constant`` and
    ``#`` lines, so they are not findings and do not need excusing. The detector's own test
    ``test_the_no_caller_detector_sees_code_and_not_prose`` pins both halves. (Five, not four: the
    commit that wrote this guard added the fifth itself, in that function's own docstring, and
    left the count from before it.)

    ⚠️ Two honest limits, neither of them a reason to prefer a grep. A reference assembled from a
    STRING is invisible (``getattr(_http, "url_without_credentials")``, ``globals()[...]``); that
    is simply not covered, and calling it unrealistic would be a prediction rather than a
    measurement. And the ``Attribute`` arm compares only ``node.attr``, never the object it hangs
    off, so a SECOND entry whose name is also a common method name would report attribute hits
    that are not this function at all: measured in ``src/``, ``_get`` has 17, ``_emit`` 10 and
    ``check_connectivity`` 6, against 0 for every top-level name in this module. Adding such an
    entry means qualifying the arm, not silencing it.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == name:
            found.append((node.lineno, "name"))
        elif isinstance(node, ast.Attribute) and node.attr == name:
            found.append((node.lineno, "attribute"))
        elif isinstance(node, ast.alias) and node.name == name:
            found.append((node.lineno, "import"))
    return found


def _definition_sites(source: str, name: str) -> list[int]:
    """The lines of *source* that ``def`` *name*, ``async def`` included.

    ⚠️ ``AsyncFunctionDef`` is a separate class, not a subclass, and leaving it out was a real
    defect rather than a hypothetical one: ``src/`` holds 104 async definitions, so a second entry
    that happens to be one would have made the guard report "defined nowhere" and tell its reader
    that somebody had deleted a function that is right there. A wrong diagnosis is worse than none.
    """
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]


def test_the_retired_redactor_exists_and_has_no_caller_in_src() -> None:
    """BG-DEAD: ``url_without_credentials`` stays callable and stays uncalled, and both are checked.

    It leaks on one spelling (``https://svc:p@ss/w0rd@host/x``, where urllib3 reads host ``ss`` and
    the rebuild carries a fragment of the password into the PATH), which is why its last caller
    moved to ``shown_url``. Nothing noticed that it had none: there is no dead-code detector among
    the pre-commit hooks, so a new caller would have shipped green.

    BOTH assertions are load-bearing and the FIRST is the one that is easy to leave out. A guard
    that only counts references passes trivially once the function is deleted, and would then read
    as "still safe" while the five red-proof recipes that name it had quietly become
    un-re-cookable. So the definition is pinned first, and the caller count second.

    Existence, not a count of one: the first assertion asks for AT LEAST one definition, because
    what it defends against is deletion, and a second entry sharing a name with some method
    elsewhere in the tree would fail an ``== 1`` while nothing was wrong. Measured in ``src/``,
    39 function names are already defined more than once (``_run`` 26 times, ``check_connectivity``
    5).

    Measured 2026-08-20: one definition, zero references. The five cross-references to it in
    ``src/`` are prose and invisible to this walk, which is why there is no allowlist to keep in
    step with them.

    ⚠️ Scope is ``src/epics_mcp``, and that is the whole delivered surface rather than a shortcut:
    ``src/`` holds exactly this one package, and none of the nine modules under ``scripts/``
    imports ``epics_mcp`` (measured), nor is ``scripts/`` packaged.

    Red proof, run in both directions: insert ``url_without_credentials(url)`` into any module
    under ``src/epics_mcp`` and the second assertion names the file and line; rename the ``def``
    away and the first one fires instead of the guard silently passing.
    """
    modules = sorted(_SRC_DIR.rglob("*.py"))
    assert modules, f"no modules found under {_SRC_DIR}, check the path"
    sources = {path: path.read_text(encoding="utf-8") for path in modules}

    for name in _RETIRED_WITHOUT_CALLER:
        # Relative keys on BOTH dicts. An absolute path here would print the operator's account
        # name into a failure log, which is the disclosure class ``_CA_BUNDLE_CAUSE`` refuses to
        # echo four hundred lines further down.
        definitions = {
            path.relative_to(_SRC_DIR).as_posix(): lines
            for path, source in sources.items()
            if (lines := _definition_sites(source, name))
        }
        assert definitions, (
            f"{name} is defined nowhere under {_SRC_DIR.name}; this guard asserts that NOTHING "
            "calls it, which is vacuously true once it is gone, so its existence is pinned here "
            "and whoever removes it removes this entry and the red-proof recipes that name it"
        )
        callers = {
            path.relative_to(_SRC_DIR).as_posix(): references
            for path, source in sources.items()
            if (references := _code_references(source, name))
        }
        assert not callers, (
            f"{name} has acquired a caller in src/: {callers}. It is kept as a measured "
            "counter-example, not as a helper, and it prints a fragment of the password for "
            "https://svc:p@ss/w0rd@host/x; read its docstring, then reach for shown_url (for a "
            "message) or url_without_userinfo (for a value a client compares)"
        )


def test_the_no_caller_detector_sees_code_and_not_prose() -> None:
    """Positive and negative control in one probe, because a detector that finds nothing is only
    good news once it has been shown to find something.

    The negative half carries the design: the five cross-references in ``src/`` are docstring
    sentences and comment lines, so if this walk saw prose the guard above would need an allowlist,
    and an allowlist is a second list free to drift from the first.

    ⚠️ The two rows at the end were added by the post-build QA, and each closes a hole the first
    version left. The ``as``-import row proved nothing about ``alias.asname``, because it matches
    on ``alias.name`` first; the mutation ``name in (node.name, node.asname)`` to
    ``node.name == name`` survived both guards. Measured, that mutation is a FIX rather than a
    hole, so what is pinned now is the false positive it removes. And ``async def`` was invisible
    to ``_definition_sites`` entirely.
    """
    name = "url_without_credentials"

    # Positive: every form a caller could arrive in.
    assert _code_references(f"from epics_mcp.services._http import {name}\n", name)
    assert _code_references(f"from epics_mcp.services._http import {name} as redact\n", name)
    assert _code_references(f"shown = {name}(url)\n", name)
    assert _code_references(f"shown = _http.{name}(url)\n", name)
    assert _code_references(f"handler = {name}\n", name)

    # Negative: the shapes the five src cross-references actually take.
    assert not _code_references(f'"""Prefer shown_url over {name} for a message."""\n', name)
    assert not _code_references(f"# {name}(url) is what this used to do\n", name)
    assert not _code_references(f"def {name}(url):\n    return url\n", name)

    # Negative, and this row is the one the QA asked for: a DIFFERENT function bound under this
    # name is not a caller of this one. Matching ``alias.asname`` reported it as one, and dropping
    # that half loses nothing, because the call it enables carries a ``Name`` node of its own.
    assert not _code_references(f"from foo import bar as {name}\n", name)
    assert _code_references(f"from foo import bar as {name}\n{name}(url)\n", name)

    # And the definition detector is the mirror image of the third negative row, for BOTH spellings
    # of a definition.
    assert _definition_sites(f"def {name}(url):\n    return url\n", name) == [1]
    assert _definition_sites(f"async def {name}(url):\n    return url\n", name) == [1]
    assert not _definition_sites(f"shown = {name}(url)\n", name)


#: Every spelling this redaction has been measured against, with the exact answer it must give.
#: A table rather than a handful of asserts because the failures that matter here are all in the
#: SPELLINGS, not in the branches. Measured over a mutation sweep, and stated by row because an
#: earlier version of this comment said "seven of nine mutants, one row each, no two on the same
#: row" and all three halves of that were wrong: the four NARROW mutants (cut at the first ``@``,
#: rebuild from the parse, drop the "an @ survives" check, drop the component comparison) die on
#: 1, 1, 3 and 2 rows and those sets are disjoint, while the identity mutant dies on 29 and
#: therefore overlaps every one of them.
_REDACTION_TABLE: tuple[tuple[str, str | None], ...] = (
    # No "@" at all. Passed through character for character, whatever the string looks like,
    # because there is nothing in it that could be a userinfo.
    ("http://cf.example.org:8080/ChannelFinder", "http://cf.example.org:8080/ChannelFinder"),
    (
        "http://CF.Example.ORG:8080/Channel Finder?x=1#f",
        "http://CF.Example.ORG:8080/Channel Finder?x=1#f",
    ),
    ("(disabled)", "(disabled)"),
    ("", ""),
    ("not a url", "not a url"),
    # A userinfo urllib3 recognises. Removed, and nothing else moves: the host keeps its case, the
    # path keeps its space, the query and the fragment stay. A rebuild from the parse would return
    # a lower-cased host and a percent-encoded space here, which is what row four is for.
    ("https://svc:hunter2@cf.example.org:8080/CF", "https://cf.example.org:8080/CF"),
    ("https://svc:hun@ter2@cf.example.org:8080/CF", "https://cf.example.org:8080/CF"),
    ("https://svc@cf.example.org/CF", "https://cf.example.org/CF"),
    (
        "https://SVC:PW@CF.Example.ORG/Channel Finder?q=1#f",
        "https://CF.Example.ORG/Channel Finder?q=1#f",
    ),
    ("https://svc:p%40w@cf.example.org/CF", "https://cf.example.org/CF"),
    ("https://svc:pw@cf.example.org", "https://cf.example.org"),
    ("http://svc:pw@cf.example.org?token=ab", "http://cf.example.org?token=ab"),
    ("https://svc:pw@cf.example.org#note", "https://cf.example.org#note"),
    ("https://svc:pw@[::1]:8080/Olog", "https://[::1]:8080/Olog"),
    # An EMPTY userinfo, both spellings, and they differ. urllib3 reads no userinfo at all in the
    # first (auth is None) while a delimiter scan sees an @ in the authority, so it is withheld
    # rather than silently rewritten; the second IS a userinfo to the parser and is cut. This pair
    # is the reach of the auth-is-None clause, which a docstring once called reachless.
    ("https://@cf.example.org/CF", None),
    ("https://:@cf.example.org/CF", "https://cf.example.org/CF"),
    # Withheld: a delimiter inside the password makes urllib3 refuse the whole URL, so there is no
    # boundary to trust. Echoing the string back is what an earlier draft of this function did,
    # and it printed the password.
    ("https://svc:s3cr3t/x@cf.example.org/CF", None),
    ("https://svc:s3cr3t?x@cf.example.org/CF", None),
    ("https://svc:s3cr3t#x@cf.example.org/CF", None),
    # Withheld: no address a socket could follow. urllib3 reads no host at all here (one slash too
    # few, a leading space, a scheme-shaped prefix), so its "no userinfo" verdict says nothing.
    ("https:/svc:s3cr3t@al.example.org/alarm", None),
    (" https://svc:s3cr3t@cf.example.org/CF", None),
    ("svc:s3cr3t@cf.example.org/CF", None),
    ("http:svc:s3cr3t@cf.example.org/CF", None),
    ("svc@//user:s3cr3t@cf.example.org/CF", None),
    ("@@", None),
    # Withheld: urllib3 reads the "@" differently than a person writing a credential does. A
    # backslash or a slash inside the USER NAME ends the authority for the parser, so the password
    # lands in the path and the host is a fragment of the user name (measured: "domain", "user").
    ("https://DOMAIN\\svc:s3cr3t@cf.example.org/CF", None),
    ("https://user/name:s3cr3t@cf.example.org/CF", None),
    ("https://svc:p@ss/w0rd@cf.example.org/CF", None),
    # Withheld: a backslash in the AUTHORITY. urllib3 connects to the host in front of it, a
    # delimiter scan would cut past it and name the host behind it, so the two readings disagree
    # and the address is not shown. Both spellings, with and without a userinfo.
    ("http://svc:s3cr3t@evil.example.org:8080\\@127.0.0.1/Olog", None),
    ("http://evil.example.org:8080\\@127.0.0.1/Olog", None),
    ("https://svc:s3cr3t@cf.example.org\\@evil.example.org/CF", None),
    # Withheld: the parser refuses a malformed IPv6 literal.
    ("https://svc:pw@[::1/Olog", None),
    # Withheld: a second "@" survives the cut. Nothing distinguishes "a path that contains an @"
    # from "a password written in a spelling that put its tail into the path", so both go.
    ("https://svc:pw@cf.example.org/x@y/CF", None),
    ("https://svc:pw@cf.example.org/x@cf.example.org/CF", None),
    # Withheld although harmless, and this is the deliberate price: an "@" outside the userinfo is
    # withheld too. A service ROOT carrying one is not an address this server has had to print.
    ("http://cf.example.org:8080/CF?mail=a@b", None),
    ("http://cf.example.org:8080/p@th/CF", None),
    # Withheld with NO "@" in it at all: the parser refuses this one, and the credential is there
    # with a percent-encoded separator. The "@"-free fast path used to print it in full.
    ("https://svc:s3cr3t%40cf.example.org/ChannelFinder", None),
)

#: The secrets planted in :data:`_REDACTION_TABLE`, for the leak scan below. Declared rather than
#: derived: a scan that reads its own needles off the inputs cannot fail. ⚠️ The first version of
#: this tuple named the exotic secrets only, so 13 of the 29 rows carrying an "@" held a password
#: (``pw``, ``PW``) or a user name (``svc``) that no needle covered, and the per-needle positive
#: control below could not see it, because it asks whether a NEEDLE occurs anywhere rather than
#: whether a ROW is covered. Both axes are checked now.
_PLANTED_SECRETS: tuple[str, ...] = (
    "s3cr3t",
    "hunter2",
    "hun@ter2",
    "ter2",
    "w0rd",
    "p%40w",
    "pw",
    "PW",
    "svc",
)

#: Userinfo spellings that carry no secret to look for, so the row-axis control below skips them.
#: Declared rather than pattern-matched: "it has no letters" would also excuse a real leak written
#: in digits.
_USERINFO_WITHOUT_A_SECRET: frozenset[str] = frozenset({":", "%40"})


@pytest.mark.parametrize(("url", "expected"), _REDACTION_TABLE)
def test_the_service_url_redaction_is_pinned_row_by_row(url: str, expected: str | None) -> None:
    """The exact answer per spelling, because every failure this function has had was a spelling.

    Red-proof, by mutant, since this function did not exist before: the IDENTITY (what the payload
    did, ``cfg.<x>_url or "(disabled)"``) fails every cut and every withheld row. The narrower
    mutants each die on their own rows, measured: cutting at the FIRST ``@``, rebuilding from the
    parse instead of deleting, dropping the "an @ survives" check, dropping the component
    comparison. Node ids and counts are in the commit that introduced this table.
    """
    assert url_without_userinfo(url) == expected


def test_no_row_of_the_table_leaks_its_planted_secret() -> None:
    """The table pins answers; this asks the question the answers exist for, over the whole table.

    TWO positive controls, on two axes, because one of them was measured to be blind. The needle
    axis proves each needle really is in an input, so a typo in a fixture cannot make the absence
    below vacuously true. The ROW axis proves each row that carries a userinfo carries a needle
    too, which the needle axis cannot see: with the first needle tuple, 13 of the 29 rows with an
    "@" had a secret no needle named, and every one of them was scanned for nothing.
    """
    inputs = "\n".join(url for url, _ in _REDACTION_TABLE)
    for secret in _PLANTED_SECRETS:
        assert secret in inputs, f"{secret} is not planted anywhere, so its absence proves nothing"

    for url, _expected in _REDACTION_TABLE:
        userinfo = _userinfo_of(url)
        if userinfo is None or userinfo in _USERINFO_WITHOUT_A_SECRET:
            continue
        assert any(secret in userinfo for secret in _PLANTED_SECRETS), (
            f"{url} carries the userinfo {userinfo!r}, which no needle names, so scanning its "
            "answer proves nothing about it"
        )

    for url, expected in _REDACTION_TABLE:
        shown = url_without_userinfo(url) or ""
        for secret in _PLANTED_SECRETS:
            assert secret not in shown, f"{url} leaked {secret}"
        assert shown == (expected or "")  # and the leak scan ran on the value the table pins


def _userinfo_of(url: str) -> str | None:
    """The userinfo urllib3 reads out of *url*, or None if it reads none / refuses the URL."""
    try:
        return parse_url(url).auth
    except (LocationParseError, ValueError):
        return None


def _deleted_spans(original: str, shown: str) -> list[str]:
    """Every contiguous piece of *original* whose removal yields *shown* (empty list: not one)."""
    return [
        original[head:tail]
        for head in range(len(original) + 1)
        for tail in range(head, len(original) + 1)
        if shown == original[:head] + original[tail:]
    ]


def test_a_shown_address_is_the_original_minus_one_userinfo_span() -> None:
    """The structural half of "it does not normalise", and it does not read the expected column.

    An assertion per row can only refuse the normalisations someone thought of. This one refuses
    all of them at once: whatever comes back has to be the input with ONE contiguous piece cut out,
    so a lower-cased host, a percent-encoded space or a dropped query cannot be produced at all,
    since those introduce characters the input never had in that order.

    ⚠️ "One deletion" alone is too weak, measured: a function returning the EMPTY STRING satisfies
    it on every row (head 0, tail the whole length) and passed an earlier version of this test. So
    the removed piece also has to END WITH the delimiter it is named after, which is what makes the
    assertion say "a userinfo was removed" rather than "something was".

    Red-proof: rebuild the result from ``urllib3.util.parse_url``, KEEPING query and fragment so
    the verification still accepts it, and the case-and-space row fails. A rebuild that drops them
    is refused upstream and reddens this test only through the positive control below, which is
    what the first version of this docstring mistook for the property firing.
    """
    cut_rows = 0
    for url, _expected in _REDACTION_TABLE:
        shown = url_without_userinfo(url)
        if shown is None:
            continue
        spans = _deleted_spans(url, shown)
        assert spans, f"{url} came back as more than a deletion: {shown}"
        assert any(span == "" or span.endswith("@") for span in spans), (
            f"{url} lost {spans!r}, which is not a userinfo"
        )
        cut_rows += shown != url

    # Positive control: the identity function would satisfy the assertions above on every row, so
    # the table has to contain rows where something really was removed.
    assert cut_rows >= 10, "the table lost its cut rows, so the property above is vacuous"


def test_the_property_holds_on_inputs_the_table_never_saw() -> None:
    """The table is a pin; this is the sweep, and the difference matters.

    Every other test here reads :data:`_REDACTION_TABLE`, so all of them go green together and none
    of them can find a spelling nobody thought of. This one generates its own, from a seed, over an
    alphabet of exactly the characters that decide the boundary. It asserts the CONTRACT rather
    than an expected value, which is the only thing one can assert about an input one did not
    choose: no planted secret survives, the answer is the input minus one userinfo span, and
    running it twice changes nothing.

    Seeded, so it is a deterministic test and not a lottery: the same 2000 inputs every run. The
    original sweep behind the implementation was far larger (a per-character sweep over the
    printable ASCII in both positions, plus 800000 random lines, all with zero leaks) and lived in
    a scratchpad, which is to say it is gone. This is the part of it that stays.

    Red-proof: any mutant that leaks also fails here, and unlike the table this test finds the
    spelling itself. Measured on the first-``@`` mutant, it goes red without a table row.
    """
    rng = random.Random(20260813)
    alphabet = ":/?#@.%[]" + chr(92) + " abAB01"
    prefixes = ("https://", "http://", "//", "https:/", "", "HTTPS://")
    secret = "SEKRET"

    for _ in range(2000):
        noise = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 12)))
        url = rng.choice(prefixes) + f"user{noise}:{secret}{noise}@cf.example.org/CF"
        shown = url_without_userinfo(url)
        if shown is None:
            continue
        assert secret not in shown, f"{url!r} leaked its secret as {shown!r}"
        spans = _deleted_spans(url, shown)
        assert any(span == "" or span.endswith("@") for span in spans), f"{url!r} lost {spans!r}"
        assert url_without_userinfo(shown) == shown, f"{url!r} is not idempotent"


def test_the_authority_span_starts_at_zero_when_there_is_no_scheme_separator() -> None:
    """``_authority_span`` is used by one caller that can never hand it such a string, and its
    fallback would still be wrong if it read ``find("//") + 2`` on a miss (that is 1, not 0).

    The public function refuses a URL without a scheme or host before the span is computed, so this
    branch is unreachable from outside and is asserted here instead of being left as a claim.
    """
    assert _authority_span("cf.example.org/CF") == (0, len("cf.example.org"))
    assert _authority_span("//cf.example.org/CF") == (2, len("//cf.example.org"))


def test_a_password_that_contains_an_at_sign_loses_its_whole_tail() -> None:
    """The one property a "the secret is not in the output" assertion cannot see.

    urllib3, the parser ``requests`` connects through, splits the authority at the LAST ``@``; a
    regex stops at the first. Under the first-``@`` reading ``svc:hun@ter2@host`` keeps ``ter2`` in
    the clear, and an assertion that only looks for the WHOLE secret passes anyway, because
    ``hun@ter2`` no longer occurs as one string. Hence the assertion on the exact answer.

    ⚠️ The follow-up assertion on the tail is belt and braces, not the discriminator: after the
    equality above it cannot fire on its own. What it documents is the string a reviewer should
    look for when this test ever does go red. And the first-``@`` mutant does not actually leak
    here, measured: it produces a candidate that still carries an ``@``, which the verification
    refuses, so it answers None. The leak it would cause without that verification is what the
    table row for ``svc:p@ss/w0rd@host`` pins.

    Red-proof: swap ``rfind`` for ``find`` in ``url_without_userinfo`` and this fails.
    """
    shown = url_without_userinfo("https://svc:hun@ter2@cf.example.org:8080/CF")

    assert shown == "https://cf.example.org:8080/CF"
    assert shown is not None and "ter2" not in shown  # the half a first-@ split would leave behind


# --- The address and the cause as a client-facing MESSAGE may carry them (BG-DERR-A) ------------

#: Every spelling :func:`shown_url` has been measured against, with the exact answer it must give.
#: A table for the same reason the redaction table above is one: the failures that matter here are
#: in the SPELLINGS, not in the branches. Two things are pinned that its delegate does not promise,
#: and both have their own rows: a query and a fragment are cut, and an unprovable answer becomes a
#: MARKER rather than ``None``, because an f-string has no null to print.
_SHOWN_URL_TABLE: tuple[tuple[str, str], ...] = (
    # Nothing to redact: passed through character for character, so an operator comparing the line
    # against their own EPICS_MCP_*_URL reads their own string back.
    ("http://cf.example.org:8080/ChannelFinder", "http://cf.example.org:8080/ChannelFinder"),
    ("http://CF.Example.ORG:8080/Channel Finder", "http://CF.Example.ORG:8080/Channel Finder"),
    # A userinfo urllib3 recognises, including the spelling a first-@ rule gets wrong.
    ("https://svc:hunter2@cf.example.org:8080/CF", "https://cf.example.org:8080/CF"),
    ("https://svc:hun@ter2@cf.example.org:8080/CF", "https://cf.example.org:8080/CF"),
    ("https://svc@cf.example.org/CF", "https://cf.example.org/CF"),
    ("https://svc:pw@[::1]:8080/Olog", "https://[::1]:8080/Olog"),
    # The query and the fragment go, with and without a userinfo. A token in a query string is a
    # normal thing to configure and a message has no use for one: every caller in this module
    # passes its query as ``params``, so a query in the printed string came from the base URL.
    ("http://archiver:17665/mgmt?apikey=s3cr3t", "http://archiver:17665/mgmt"),
    ("http://archiver:17665/mgmt#note", "http://archiver:17665/mgmt"),
    ("https://svc:pw@cf.example.org/CF?token=s3cr3t", "https://cf.example.org/CF"),
    ("https://svc:pw@cf.example.org/CF#f", "https://cf.example.org/CF"),
    # Withheld. Each is a row the delegate refuses, and the marker is what a message prints
    # instead. The third kills "just rebuild from the parse": that answer would be
    # "https://ss/w0rd@cf.example.org/CF", a fragment of the password, in the path, with no "@"
    # left for any structural check to catch.
    ("https://@cf.example.org/CF", "(unparseable)"),
    ("svc:s3cr3t@cf.example.org/CF", "(unparseable)"),
    ("https://svc:p@ss/w0rd@cf.example.org/CF", "(unparseable)"),
    ("https://svc:s3cr3t%40cf.example.org/ChannelFinder", "(unparseable)"),
    ("http://svc:s3cr3t@evil.example.org:8080\\@127.0.0.1/Olog", "(unparseable)"),
    # A cut that leaves nothing is not an address either.
    ("?token=s3cr3t", "(unparseable)"),
)


#: Rows carrying an ``@`` that plant no secret at all, so the row axis below skips them instead of
#: demanding a needle they cannot have. Declared by their whole spelling rather than pattern-
#: matched, for the reason the sibling exemption above states: "it has no letters" would excuse a
#: real leak written in digits. One row so far: an EMPTY userinfo is a userinfo to a delimiter scan
#: and to nobody else.
_ROWS_WITHOUT_A_SECRET: frozenset[str] = frozenset({"https://@cf.example.org/CF"})


@pytest.mark.parametrize(("url", "expected"), _SHOWN_URL_TABLE)
def test_the_shown_address_is_pinned_row_by_row(url: str, expected: str) -> None:
    """The exact answer per spelling.

    Red-proof, by mutant: returning ``url`` fails every redacted row; delegating to
    ``url_without_credentials`` instead of ``url_without_userinfo`` fails the ``svc:p@ss/w0rd`` row
    with a password fragment in the path; dropping the query cut fails the four query/fragment
    rows; returning the delegate's answer unguarded puts ``None`` where a marker belongs.
    """
    assert shown_url(url) == expected


def test_no_row_of_the_shown_address_table_leaks_its_planted_secret() -> None:
    """The table pins answers; this asks the question the answers exist for.

    Two axes, the pair the redaction table above already needs and for the same measured reason:
    the needle axis proves each needle is really planted, so a typo in a fixture cannot make the
    absence vacuously true, and the row axis proves every row carrying a userinfo or a query
    carries a needle at all.
    """
    # ``svc`` and ``pw`` are needles in their own right, not filler: a BARE user name is the whole
    # credential material of the ``https://svc@host/CF`` row, and the row axis found that row
    # scanned for nothing the first time this list was written without them.
    needles = ("s3cr3t", "hunter2", "hun@ter2", "ter2", "w0rd", "svc", "pw")
    inputs = "\n".join(url for url, _ in _SHOWN_URL_TABLE)
    for needle in needles:
        assert needle in inputs, f"the needle {needle!r} is planted in no row"
    for url, _ in _SHOWN_URL_TABLE:
        if url in _ROWS_WITHOUT_A_SECRET:
            continue
        if "@" in url or "?" in url:
            assert any(needle in url for needle in needles), f"{url!r} is scanned for nothing"

    outputs = "\n".join(shown_url(url) for url, _ in _SHOWN_URL_TABLE)
    for needle in needles:
        assert needle not in outputs, f"{needle!r} survived into a shown address"


def test_a_served_status_is_named_from_the_client_side_table_not_the_servers_reason() -> None:
    """The cause of a served failure says what the STATUS was, in this process's own words.

    Requests' own text is ``f"{code} Client Error: {reason} for url: {self.url}"``, where
    ``self.url`` is the PREPARED url and keeps its userinfo, and ``reason`` is the responding
    server's status line. Printing either lets a foreign host write part of a message this module
    promises is credential-free.

    Red-proof: returning ``str(exc)`` for an ``HTTPError`` fails this assertion.
    """
    response = requests.Response()
    response.status_code = 401
    response.reason = "c3ZjOnB3"  # what a hostile server can put in its own status line
    response.url = "http://svc:pw@cf.example.org/CF"
    exc = requests.exceptions.HTTPError(
        "401 Client Error: c3ZjOnB3 for url: http://svc:pw@cf.example.org/CF", response=response
    )

    assert shown_cause(exc) == "HTTP 401 Unauthorized"


def test_a_status_no_table_names_still_reports_its_number() -> None:
    """``HTTPStatus(599)`` RAISES, and a proxy really does answer 520 and 599.

    Red-proof: an unguarded ``HTTPStatus(status).phrase`` raises ValueError on this row.
    """
    response = requests.Response()
    response.status_code = 599
    assert shown_cause(requests.exceptions.HTTPError("599", response=response)) == "HTTP 599"


def test_an_http_error_without_a_response_falls_through_rather_than_inventing_a_code() -> None:
    """A hand-built ``HTTPError`` has no ``.response``, and dozens of tests in this tree build one.

    Such a text carries no url either, so the pass-through is safe, and an ``AttributeError`` here
    would be a red with nothing to do with the leak.

    Red-proof: reading ``exc.response.status_code`` unguarded raises AttributeError on this row.
    """
    assert shown_cause(requests.exceptions.HTTPError("500")) == "500"


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.MissingSchema("Invalid URL 'svc:pw@h/x': No scheme supplied."),
        requests.exceptions.InvalidSchema("No connection adapters were found for 'x://svc:pw@h'"),
        requests.exceptions.InvalidURL("Failed to parse: https://svc:s3cr3t%40host/x"),
    ],
)
def test_a_url_shape_failure_names_the_defect_and_echoes_nothing(exc: Exception) -> None:
    """These three are raised inside ``prepare_url``, before a request exists, and they quote the
    configured value: ``MissingSchema`` twice, the second time as a helpful suggestion that
    reconstructs it WITH its credential. There is nothing to redact either, because the parser
    that would prove a redaction is the one that just refused the string.

    Red-proof: falling through to ``str(exc)`` puts ``pw``/``s3cr3t`` into the answer.
    """
    shown = shown_cause(exc)

    assert "not a usable HTTP address" in shown
    assert "pw" not in shown
    assert "s3cr3t" not in shown


@pytest.mark.parametrize(
    ("cause", "marker"),
    [
        ("HTTPConnectionPool(host='h', port=9): Max retries (ConnectionRefusedError)", "Refused"),
        ("HTTPConnectionPool(host='h', port=9): Max retries (NameResolutionError)", "NameResolut"),
        ("HTTPConnectionPool(host='h', port=9): Read timed out. (read timeout=1)", "timed out"),
        ("HTTPSConnectionPool(host='h', port=443): CERTIFICATE_VERIFY_FAILED", "CERTIFICATE"),
    ],
)
def test_a_transport_cause_travels_verbatim_and_stays_distinguishable(
    cause: str, marker: str
) -> None:
    """The four transport failures read differently, and this text is the ONLY place they do.

    Measured: requests hands urllib3 only ``path_url``, so none of these carries a userinfo, and
    withholding them would delete diagnosis without closing a leak. ``doctor._REMEDY`` is static
    per status and cannot supply the distinction afterwards.

    Red-proof: withholding the cause wholesale (the class name for every family) fails all four
    rows, and so does a per-family fixed phrase.
    """
    shown = shown_cause(requests.exceptions.ConnectionError(cause))

    assert shown == cause
    assert marker in shown


def test_an_unexpected_text_carrying_an_at_is_withheld_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The output-side check, and the input that proves the branch can fire.

    The criterion needs no knowledge of the secret, which is the point: requests rewrites a
    userinfo in flight, so a search for the CONFIGURED value finds nothing and reports a clean
    message that carries the password. What survives every rewrite is the SEPARATOR.

    It logs at WARNING because a net that silently repairs makes the defect it repaired invisible.

    Red-proof: dropping the ``"@" not in text`` check returns the raw text and fails both the
    absence assertion and the log assertion.
    """
    with caplog.at_level(logging.WARNING, logger="epics_mcp.services._http"):
        shown = shown_cause(ValueError("boom at http://svc:s3cr3t@cf.example.org/CF"))

    assert shown == "ValueError (message withheld: it would echo a credential)"
    assert "s3cr3t" not in shown
    assert any("withheld the cause text" in record.message for record in caplog.records)


def test_shown_failure_pairs_the_two_redacted_halves() -> None:
    """The composite every failing site prints, both halves redacted, in the punctuation the sites
    used before, so a reader's eye finds the same shape.

    Red-proof: composing with ``url`` or with ``str(exc)`` puts the credential back.
    """
    response = requests.Response()
    response.status_code = 404
    exc = requests.exceptions.HTTPError("404 ... for url: http://svc:pw@h/x", response=response)

    assert (
        shown_failure("http://svc:pw@olog:8080/Olog", exc)
        == "http://olog:8080/Olog: HTTP 404 Not Found"
    )


def test_requests_still_puts_the_prepared_url_in_the_httperror_message() -> None:
    """The PREMISE the whole redaction rests on, pinned ONCE instead of inside every absence test.

    Two measured facts about a library this repository does not own: a prepared request keeps the
    userinfo in its url, and ``raise_for_status`` quotes that url. If a future requests stops doing
    either, THIS test goes red and says so, rather than N absence tests going red together and
    reading as "our redaction broke".

    The last two lines pin the transcoding that killed the obvious design: requests requotes the
    userinfo, so ``s%65cret`` arrives as ``secret``. A check that searches a message for the
    CONFIGURED secret is therefore blind, and ``shown_cause`` looks for the separator instead,
    which requote's safe set never touches.
    """
    prepared = requests.Request("GET", "http://svc:s3cr3t@cf.example.org/CF").prepare()
    assert prepared.url is not None
    assert "svc:s3cr3t@" in prepared.url

    response = requests.Response()
    response.status_code = 401
    response.url = prepared.url
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        response.raise_for_status()
    assert "svc:s3cr3t@" in str(excinfo.value)

    transcoded = requests.Request("GET", "http://svc:s%65cret@cf.example.org/CF").prepare()
    assert transcoded.url is not None
    assert "svc:secret@" in transcoded.url


@pytest.mark.parametrize(
    ("factory", "service"),
    [
        ("epics_mcp.services.channelfinder_client:ChannelFinderClient", "ChannelFinder"),
        ("epics_mcp.services.alarm_client:AlarmClient", "Alarm Logger"),
        ("epics_mcp.services.olog_client:OlogClient", "Olog"),
        ("epics_mcp.services.naming_client:NamingServiceClient", "Naming Service"),
    ],
)
def test_every_connect_failure_names_a_redacted_address(
    factory: str, service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four ``check_connectivity`` bodies, which had no text assertion of any kind before.

    They are the same six lines four times, and each printed ``{self.base_url}: {exc}``. Nothing in
    the tree asserted their text, so reverting any one of them would have gone unnoticed; that gap
    is what this test closes, and it is why the four are checked together rather than one by one.

    The failure is forced at the client's OWN session rather than at the class, so the real body
    runs and only the socket is replaced.

    Red-proof against the pre-fix code: the secret assertion fails for every one of the four.
    """
    import importlib

    module_name, class_name = factory.split(":")
    client_class = getattr(importlib.import_module(module_name), class_name)
    client = client_class("http://svc:s3cr3t@svc.example.org:8080/base")
    monkeypatch.setattr(
        client.session,
        "head",
        Mock(side_effect=requests.exceptions.ConnectionError("HTTPConnectionPool(host='x'): no")),
    )

    with pytest.raises(RestConnectionError) as excinfo:  # the shared root of all four
        client.check_connectivity()

    message = str(excinfo.value)
    assert f"Failed to connect to {service} at http://svc.example.org:8080/base" in message
    assert "s3cr3t" not in message


@pytest.mark.parametrize(
    ("base", "url", "expected"),
    [
        ("http://olog", "http://olog/logbooks", "/logbooks"),
        ("http://svc:s3cr3t@olog", "http://svc:s3cr3t@olog/levels", "/levels"),
        ("http://cf/CF", "http://cf/CF/resources/tags", "/resources/tags"),
        # Fails closed. The removal is a no-op when the prefix does not match, and the answer
        # would then BE the full url, credential included, so it is withheld instead. No caller
        # can reach this today (every one builds the url from the base), which is exactly why it
        # is pinned here rather than left as a branch no input proves.
        ("http://olog", "http://other:s3cr3t@host/logbooks", "(route withheld)"),
        ("http://olog", "http://olog", "(route withheld)"),
    ],
)
def test_the_route_label_keeps_the_route_and_never_the_host(
    base: str, url: str, expected: str
) -> None:
    """Red-proof: returning ``url`` fails the first three rows with a host in the answer and the
    fourth with a credential in it; returning ``url.removeprefix(base)`` unguarded fails the last
    two."""
    assert route_label(base, url) == expected


_CREDENTIALLED = "http://svc:s3cr3t@arch.example.org:17665/mgmt"


def test_the_substrate_connect_error_redacts_both_halves(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rest_get_json``'s own connect branch, which no test asserted the text of before.

    Red-proof: reverting the branch to ``f"Failed to connect to {url}: {exc}"`` fails both the
    address and the secret assertion.
    """
    session = build_retrying_session()
    monkeypatch.setattr(
        session,
        "get",
        Mock(side_effect=requests.exceptions.ConnectionError("HTTPConnectionPool(host='a'): no")),
    )

    with pytest.raises(ArchiverConnectionError) as excinfo:
        rest_get_json(
            session,
            _CREDENTIALLED,
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )

    message = str(excinfo.value)
    assert "Failed to connect to http://arch.example.org:17665/mgmt" in message
    assert "HTTPConnectionPool(host='a'): no" in message  # the transport cause stays verbatim
    assert "s3cr3t" not in message


def test_the_substrate_redirect_refusal_redacts_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refused redirect names the address it refused to leave, and that address was raw.

    Red-proof: reverting to ``from {url} (HTTP ...)`` fails the secret assertion.
    """
    session = build_retrying_session()
    redirect = Mock()
    redirect.is_redirect = True
    redirect.status_code = 302
    monkeypatch.setattr(session, "get", Mock(return_value=redirect))

    with pytest.raises(ArchiverResponseError) as excinfo:
        rest_get_json(
            session,
            _CREDENTIALLED,
            None,
            1.0,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
            allow_redirects=False,
        )

    message = str(excinfo.value)
    assert "redirect target" in message  # the wording F20 pinned, unchanged
    assert "from http://arch.example.org:17665/mgmt" in message
    assert "s3cr3t" not in message


def test_the_substrate_size_cap_redacts_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size-cap refusal names the address it stopped reading from.

    Red-proof: reverting to ``Response body from {url}`` fails the secret assertion.
    """
    session = build_retrying_session()
    # MagicMock, not Mock: rest_get_bytes streams, so it uses the response as a context
    # manager and a plain Mock has no __exit__.
    oversized = MagicMock()
    oversized.__enter__.return_value = oversized
    oversized.is_redirect = False
    oversized.status_code = 200
    oversized.headers = {"Content-Length": "999999999", "Content-Type": "application/octet-stream"}
    oversized.raise_for_status.return_value = None
    monkeypatch.setattr(session, "get", Mock(return_value=oversized))

    with pytest.raises(ArchiverResponseError) as excinfo:
        rest_get_bytes(
            session,
            _CREDENTIALLED,
            1.0,
            max_bytes=1024,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )

    message = str(excinfo.value)
    assert "size cap" in message  # the wording test_olog_attachments pins, unchanged
    assert "Response body from http://arch.example.org:17665/mgmt" in message
    assert "s3cr3t" not in message
