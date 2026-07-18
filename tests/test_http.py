"""Tests for the shared REST substrate (services/_http) and the REST exception roots (C3).

Covers the M3/M12/L-Logger deduplication: one retry policy, one GET-and-translate, one root
exception hierarchy, and the single debug line that wakes the previously-dead REST logger.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url
from urllib3.util.retry import Retry

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services._http import (
    build_retrying_session,
    build_write_session,
    http_status,
    is_http_404,
    is_https_url,
    is_loopback_url,
    is_retry_error,
    is_ssl_error,
    rest_get_json,
    rest_put_json,
    url_host,
)
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverError,
    ArchiverResponseError,
)
from epics_pv_mcp.services.channelfinder_client import ChannelFinderClient
from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.rest_exceptions import (
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
    """tls_verify=False must actually disable verification — pinning trust_env off is what makes the
    escape hatch real even when REQUESTS_CA_BUNDLE is set in the environment."""
    session = build_retrying_session(verify=False)
    assert session.verify is False
    assert session.trust_env is False


def test_verify_resolves_ca_bundle_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no kwarg the chokepoint reads ITS OWN get_config
    (epics_pv_mcp.services._http.get_config, NOT checkers.get_config) → ca_bundle path wins and
    trust_env is pinned off."""
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    session = build_retrying_session()
    assert session.verify == "ca.pem"
    assert session.trust_env is False


def test_verify_resolves_tls_verify_false_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="", tls_verify=False),
    )
    session = build_retrying_session()
    assert session.verify is False
    assert session.trust_env is False


def test_verify_default_config_verifies_and_keeps_trust_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plain default (ca_bundle empty, tls_verify True) verifies against certifi and leaves
    trust_env on — the zero-code REQUESTS_CA_BUNDLE path stays available."""
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
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
    ``max_retries.total == 0`` on both schemes — a lost PUT surfaces as an error, never a retry."""
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
    """The write session must actually carry the auth header — the write NEEDS it. A silent drop
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
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    client = ChannelFinderClient("http://cf", auth_header=None)
    assert client.session.verify == "ca.pem"
    assert client.session.trust_env is False


def test_naming_client_session_inherits_ca_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-1 acceptance 3(c): the Naming client that diagnose._gather_naming constructs DIRECTLY
    (bypassing the checkers factory) still inherits the CA — the chokepoint covers it too."""
    monkeypatch.setattr(
        "epics_pv_mcp.services._http.get_config",
        lambda: EpicsConfig(ca_bundle="ca.pem"),
    )
    client = NamingServiceClient(base_url="http://naming")
    assert client.session.verify == "ca.pem"
    assert client.session.trust_env is False


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
        caplog.at_level(logging.DEBUG, logger="epics_pv_mcp.services._http"),
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
    host" — that is objectively false for a same-origin redirect (http→https, trailing slash). The
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


def test_rest_put_json_refuses_redirect_with_neutral_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F20 on the WRITE path: the same "different host" wording is equally wrong for a same-origin
    redirect. Behaviour (refuse the redirect) is unchanged — only the message text.

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
    # RetryError has no .response, so http_status can't read a code — hence is_retry_error exists.
    assert http_status(err) is None


def test_is_retry_error_false_for_others() -> None:
    conn = ArchiverConnectionError("x")
    conn.__cause__ = requests.exceptions.ConnectionError("refused")
    assert is_retry_error(conn) is False
    assert is_retry_error(ArchiverConnectionError("x")) is False  # no __cause__


# ----------------------------------------------------------------------------------------------
# is_loopback_url — the shared "is this a local test server?" primitive
#
# Extracted from the Olog write gate so the READ redaction can reuse the SAME hardened host
# extraction without reusing the gate's POLICY (`_url_write_allowed` also returns True for an
# allowlisted REMOTE host — see test_reads_a_url_the_write_gate_would_allow_remotely).
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
        "http://olog:8080/Olog",  # the docker-compose service name (used by the redaction tests)
        # RFC1918 PRIVATE is NOT loopback — a production service lives on a private network.
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
        # but urllib3 — the parser requests actually CONNECTS with — resolves evil.example.org.
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

    Same boolean direction as the write gate: False -> restrict. For a write that means "deny",
    for a read it means "redact" — fail-closed and fail-safe agree, so no inversion is needed.
    """
    assert is_loopback_url(url) is False


@pytest.mark.parametrize("url", ["http://./Olog", "http://.../Olog", "", "garbage", "http:///x"])
def test_url_host_returns_none_for_everything_unparseable(url: str) -> None:
    """url_host must return None — never "" — for anything without a usable host.

    Callers use ``url_host(url) is None`` as a hard veto (the write gate denies such a URL even when
    it is exactly allowlisted). An empty string slips past that identity check: "http://./Olog" has
    hostname "." which survives a falsiness test and only becomes "" after the trailing-dot strip.
    Normalise first, THEN decide emptiness.
    """
    assert url_host(url) is None


def test_url_host_agrees_with_the_parser_that_connects() -> None:
    """url_host must name the host requests would actually reach — not a different one.

    urllib3 is what requests connects with. Where the two parsers disagree (a backslash in the
    authority), a decision built on urlparse describes a server other than the one on the wire —
    so the primitive uses urllib3's answer, and refuses when they cannot agree.
    """
    hostile = "http://evil.example.org:8080\\@127.0.0.1/Olog"
    assert urlparse(hostile).hostname == "127.0.0.1"  # what urlparse claims…
    assert parse_url(hostile).host == "evil.example.org"  # …and where the connection would go
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
