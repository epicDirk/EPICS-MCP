"""Offline tests for the vendored ESS Naming-Service client (mocked HTTP)."""

import json
from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.naming_exceptions import (
    NamingServiceConnectionError,
    NamingServiceNotFound,
    NamingServiceResponseError,
)
from epics_pv_mcp.services.naming_identity import (
    NAMING_SWAGGER_TITLE,
    probe_naming_identity,
)
from epics_pv_mcp.services.rest_exceptions import RestConnectionError, RestResponseError


def _resp(payload: object, *, status: int = 200) -> Mock:
    """Build a fake ``requests`` response with the given JSON payload and HTTP status.

    A status >= 400 makes ``raise_for_status`` raise an ``HTTPError`` carrying this response (as
    real requests does), so the client can read ``exc.response.status_code`` to split 404 apart.
    """
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = payload
    if status >= 400:
        err = requests.exceptions.HTTPError(str(status))
        err.response = resp
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _client_with(monkeypatch: pytest.MonkeyPatch, response: Mock) -> NamingServiceClient:
    """A NamingServiceClient whose every GET returns *response* (no network)."""
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(client.session, "get", Mock(return_value=response))
    return client


def _stub_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: bool = True,
    exc: Exception | None = None,
) -> Mock:
    """Stub the SECOND, INDEPENDENT seam — the swagger identity probe's ``rest_get_json`` — apart
    from the deviceNames GET (``client.session.get``). The S13 gate issues a second request the
    single-response ``_client_with`` mock cannot serve, so the two must be seamed separately.
    ``verified`` picks the correct vs a foreign ``info.title``; pass ``exc`` to simulate a probe
    that raises. Returns the mock so a caller can assert ``call_count`` (the per-instance cache)."""
    if exc is not None:
        seam: Mock = Mock(side_effect=exc)
    else:
        title = NAMING_SWAGGER_TITLE if verified else "Some other API"
        seam = Mock(return_value={"info": {"title": title}})
    monkeypatch.setattr("epics_pv_mcp.services.naming_identity.rest_get_json", seam)
    return seam


def _client_for_negative(monkeypatch: pytest.MonkeyPatch, status: int) -> NamingServiceClient:
    """A client whose deviceNames GET returns a CLEAN definitive negative (404 or 204). A clean
    negative is mandatory for an identity-gate test: a 500 / bad-JSON would withhold via the
    pre-existing DS-2 path with the gate DELETED, making the guard vacuous."""
    if status == 204:
        resp = _resp(None, status=204)
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
    else:
        resp = _resp({}, status=status)
    return _client_with(monkeypatch, resp)


@pytest.mark.parametrize("base", ["http://h/enotify-web", "http://h/enotify-web/"])
def test_base_url_normalised_so_urls_match_with_or_without_trailing_slash(base: str) -> None:
    """M10: base_url is rstripped like the other REST clients, so a URL configured
    with OR without a trailing slash yields identical, correct endpoints (no 404)."""
    client = NamingServiceClient(base_url=base)
    assert client.base_url == "http://h/enotify-web"
    assert client.names_url == "http://h/enotify-web/rest/deviceNames/"


def test_validate_name_active(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(monkeypatch, _resp({"status": "ACTIVE"}))
    result = client.validate_name("DEV-TEST01:Ctrl-EVR-01")
    assert result["registered"] is True
    assert result["status"] == "ACTIVE"


def test_validate_name_obsolete_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with(monkeypatch, _resp({"status": "OBSOLETE"}))
    result = client.validate_name("DEV-TEST01:Ctrl-EVR-99")
    assert result["registered"] is False
    assert result["status"] == "OBSOLETE"


def test_validate_name_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine 404 on the deviceNames endpoint = the DEFINITIVE 'not registered'. S13 POSITIVE
    control: with the responder's identity VERIFIED (swagger beacon), the 404 still maps to a
    definitive registered=False — the gate must not OVER-withhold a real not-registered."""
    _stub_identity(monkeypatch, verified=True)
    client = _client_with(monkeypatch, _resp({}, status=404))
    result = client.validate_name("NOPE:nope")
    assert result["registered"] is False
    assert result["status"] == ""
    assert "not registered" in result["message"]


def test_validate_name_service_error_propagates_not_false_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-2 / audit S5: a NON-404 failure (here HTTP 500) must NOT collapse into a definitive
    ``registered=False`` (a false name_typo). It PROPAGATES as NamingServiceResponseError so the
    caller withholds. Distinguishes 'name not registered' from 'service/URL error'."""
    client = _client_with(monkeypatch, _resp({}, status=500))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


def test_validate_name_transport_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-HTTP GET failure (here a read Timeout during the deviceNames GET, after a successful
    reachability probe) also propagates as a service error, never a false 'not registered'."""
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.Timeout("read timeout"))
    )
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


# --- client: strict response schema (S11) — unreadable 2xx is NEVER a definitive answer ---
#
# Measured (ESS Naming, live 2026-07-16): GET /rest/deviceNames/{name} with Accept:
# application/json answers a dict that ALWAYS carries a string `status` (plus name/uuid/…);
# WITHOUT the Accept header the service answers XML (content-type application/xml) — so the
# client must ask for JSON explicitly. A nonexistent name answers HTTP 204 (No Content), NOT
# the 404 the old contract assumed (S16a).


def test_session_asks_for_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Premise pin (S11, measured live 2026-07-16): the real ESS Naming service serves **XML**
    to a plain ``Accept: */*`` GET — only ``Accept: application/json`` yields the JSON record
    this client parses. The shared ``build_retrying_session`` sets that header today; this pins
    it, because swapping the session builder would silently collapse EVERY live lookup into
    withheld (``resp.json()`` fails on XML). Mutant-red: without the header the requests default
    is ``*/*``."""
    client = NamingServiceClient(base_url="http://naming.example/")
    assert client.session.headers.get("Accept") == "application/json"


def test_validate_name_payload_without_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: a 2xx dict WITHOUT the measured anchor ``status`` must RAISE (→ callers withhold) —
    it used to become the definitive ``registered=False`` with status ''."""
    client = _client_with(monkeypatch, _resp({"unexpected": "shape"}))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


@pytest.mark.parametrize("status", [123, ""], ids=["non-str", "empty-str"])
def test_validate_name_unreadable_status_raises(
    status: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11 (plan-review finding A2 + diff-review): a non-string ``status`` used to be
    str()-minted ('123'), and an EMPTY string read as an unknown status — both became the
    definitive ``registered=False``. Neither is a measured server value; junk raises instead."""
    client = _client_with(monkeypatch, _resp({"status": status}))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


@pytest.mark.parametrize("payload", [["x"], "nope", 123], ids=["list", "string", "number"])
def test_validate_name_non_dict_payload_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: a non-dict 2xx payload used to escape as an uncaught AttributeError (crashing
    crossplane_check); it must be the plane's own ResponseError so every caller withholds."""
    client = _client_with(monkeypatch, _resp(payload))
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


def test_validate_name_204_is_definitively_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S16(a), measured live (ESS Naming 2026-07-16): a nonexistent name answers HTTP 204
    (No Content) — NOT 404. Before this, the empty body failed ``resp.json()`` and the lookup
    withheld (honest but needlessly vague); the measured 204 is the service's definitive
    "not registered" and maps to it. The 404 branch stays (a second definitive signal). S13
    POSITIVE control: identity VERIFIED, so the 204 still maps to definitive registered=False."""
    _stub_identity(monkeypatch, verified=True)
    resp = _resp(None, status=204)
    resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
    client = _client_with(monkeypatch, resp)
    result = client.validate_name("ZZZ-FAKE99:Ctrl-X-99")
    assert result["registered"] is False
    assert "not registered" in result["message"]


def test_check_connectivity_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(
        client.session, "head", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(NamingServiceConnectionError):
        client.check_connectivity()


def test_check_connectivity_wraps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """S8-5 contract guard: a requests.exceptions.Timeout must surface as
    NamingServiceConnectionError (a withheld transport signal). This is a forward guard — it holds
    today because RequestException ⊂ OSError, and it FAILS if the except arm is ever narrowed to
    only ConnectionError, re-opening the raw-escape/false-'not registered' hole."""
    client = NamingServiceClient(base_url="http://naming.example/")
    monkeypatch.setattr(client.session, "head", Mock(side_effect=requests.exceptions.Timeout()))
    with pytest.raises(NamingServiceConnectionError):
        client.check_connectivity()


def test_check_connectivity_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # G4: the reachability probe must honor the configured timeout (default 5 s),
    # not a hardcoded 1 s that falsely reports a slow-but-reachable service down.
    client = NamingServiceClient(base_url="http://naming.example/", timeout=7.5)
    head = Mock(return_value=Mock())
    monkeypatch.setattr(client.session, "head", head)
    assert client.check_connectivity() is True
    head.assert_called_once_with(client.base_url, timeout=7.5)


# ---------------------------------------------------------------------------
# S13 — swagger identity probe (naming_identity.probe_naming_identity) in isolation
# ---------------------------------------------------------------------------


def _probe_seam(
    monkeypatch: pytest.MonkeyPatch, result: object = None, *, raises: BaseException | None = None
) -> None:
    """Seam the probe's ``rest_get_json`` to return *result* or raise *raises* (independent of the
    deviceNames GET)."""
    seam = Mock(side_effect=raises) if raises is not None else Mock(return_value=result)
    monkeypatch.setattr("epics_pv_mcp.services.naming_identity.rest_get_json", seam)


def test_probe_verified_on_matching_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx swagger whose ``info.title`` equals the constant → ``verified``."""
    _probe_seam(monkeypatch, {"info": {"title": NAMING_SWAGGER_TITLE}})
    assert probe_naming_identity("http://naming.example") == "verified"


@pytest.mark.parametrize(
    "payload",
    [{"info": {"title": "Some other API"}}, {"info": {}}, {"nope": 1}, "html", 123],
    ids=["foreign-title", "no-title", "no-info", "string-body", "number-body"],
)
def test_probe_unverified_on_2xx_without_matching_title(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2xx that does not name itself as the Naming Service is honest ``unverified``
    (recognisable-but-unproven) — never a hard failure (matches epics-doctor since S14)."""
    _probe_seam(monkeypatch, payload)
    assert probe_naming_identity("http://naming.example") == "unverified"


def test_probe_unverified_on_unreadable_2xx_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A REACHED-but-unreadable 2xx (a non-JSON body → a ValueError/JSONDecodeError wrapped as
    ``__cause__`` on modern requests) is ``unverified`` — epics-doctor's
    _beacon_reached_but_unreadable split, kept in lockstep."""
    wrapped = RestResponseError("unreadable body")
    wrapped.__cause__ = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
    _probe_seam(monkeypatch, raises=wrapped)
    assert probe_naming_identity("http://naming.example") == "unverified"


def test_probe_unverified_on_raw_stdlib_jsondecodeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the requests<2.27 floor a bad body arrives as the RAW stdlib JSONDecodeError (a ValueError
    but not a RequestException, so rest_get_json does not wrap it). The probe checks the exception
    ITSELF too → ``unverified``, not a false ``probe_failed``."""
    _probe_seam(monkeypatch, raises=json.JSONDecodeError("Expecting value", "", 0))
    assert probe_naming_identity("http://naming.example") == "unverified"


@pytest.mark.parametrize(
    "exc",
    [RestResponseError("served 404/401/5xx"), RestConnectionError("unreachable")],
    ids=["served-non-2xx", "transport"],
)
def test_probe_failed_on_non_2xx_or_transport(
    exc: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A served non-2xx or a transport error never reached a 2xx body → ``probe_failed``."""
    _probe_seam(monkeypatch, raises=exc)
    assert probe_naming_identity("http://naming.example") == "probe_failed"


def test_probe_failed_on_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """S13 redirect guard: ``allow_redirects=False`` (origin integrity) makes a REDIRECTING swagger
    URL a probe failure, so a real deviceNames endpoint behind a redirecting swagger is WITHHELD,
    not verified. Exercises the real ``rest_get_json`` redirect refusal end-to-end."""
    redirect = Mock()
    redirect.is_redirect = True
    redirect.status_code = 302
    session = Mock()
    session.get = Mock(return_value=redirect)
    monkeypatch.setattr(
        "epics_pv_mcp.services.naming_identity.build_retrying_session",
        Mock(return_value=session),
    )
    assert probe_naming_identity("http://naming.example") == "probe_failed"


def test_probe_is_total_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S13 totality: the probe NEVER raises. ``crossplane_check`` catches only
    NamingServiceResponseError, so a raw escape from the identity path would crash a best-effort
    report — even an UNEXPECTED error must degrade to ``probe_failed``."""
    _probe_seam(monkeypatch, raises=RuntimeError("boom"))
    assert probe_naming_identity("http://naming.example") == "probe_failed"


# ---------------------------------------------------------------------------
# S13 — the definitive-negative identity gate wired into naming_client
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 204], ids=["404", "204"])
def test_definitive_negative_withheld_when_identity_foreign(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S13 RED-proof: a CLEAN deviceNames negative (404/204) from a host whose swagger answers a
    FOREIGN title is WITHHELD (NamingServiceResponseError), NOT a definitive registered=False.
    Mutant-red: deleting the ``_require_verified_identity()`` call flips it to registered=False."""
    _stub_identity(monkeypatch, verified=False)
    client = _client_for_negative(monkeypatch, status)
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


@pytest.mark.parametrize("status", [404, 204], ids=["404", "204"])
def test_definitive_negative_withheld_when_identity_probe_fails(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S13: a CLEAN deviceNames negative from a host whose swagger PROBE FAILS (served non-2xx /
    transport) is likewise WITHHELD (probe_failed → not verified)."""
    _stub_identity(monkeypatch, exc=RestResponseError("swagger 404"))
    client = _client_for_negative(monkeypatch, status)
    with pytest.raises(NamingServiceResponseError):
        client.validate_name("DEV-TEST01:Ctrl-EVR-01")


def test_definitive_negative_withhold_raises_parent_not_notfound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S13 (why the PARENT class): the withhold must raise NamingServiceResponseError, NOT its
    subclass NamingServiceNotFound — validate_name catches ONLY NotFound and would map it to a
    false registered=False. Pin that the raised type is not NotFound (mutant: raise NotFound
    instead → validate_name returns a false registered=False)."""
    _stub_identity(monkeypatch, verified=False)
    client = _client_for_negative(monkeypatch, 404)
    with pytest.raises(NamingServiceResponseError) as exc_info:
        client._get_device_name("DEV-TEST01:Ctrl-EVR-01")
    assert not isinstance(exc_info.value, NamingServiceNotFound)


def test_identity_probed_once_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """S13 cache: N negative lookups on ONE client issue the swagger identity probe EXACTLY once
    (the ``self._identity is None`` guard). Mutant-red: removing the cache makes call_count == N."""
    seam = _stub_identity(monkeypatch, verified=True)
    client = _client_for_negative(monkeypatch, 404)
    for name in ("A:a", "B:b", "C:c"):
        assert client.validate_name(name)["registered"] is False
    assert seam.call_count == 1


def test_doctor_and_client_share_one_swagger_title() -> None:
    """S13 single-source: epics-doctor's naming plane and the naming identity probe resolve the SAME
    swagger-title constant (imported from naming_identity), so the two identity surfaces cannot
    drift — a title reword is a ONE-line change in one module."""
    from epics_pv_mcp.services import doctor, naming_identity

    # Read via __dict__: the constant is imported into doctor's namespace (not re-exported), and
    # the point is that doctor resolves the SAME object as naming_identity (drift = a new object).
    assert doctor.__dict__["NAMING_SWAGGER_TITLE"] is naming_identity.NAMING_SWAGGER_TITLE


def test_identity_verdict_is_per_instance_not_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    """S13 cache SCOPE (QA F6): the identity verdict caches on the INSTANCE (``self._identity``),
    never shared across clients — so a SECOND client (possibly pointed at a different host) issues
    its OWN swagger probe and never inherits the first's ``verified``. The existing
    ``test_identity_probed_once_per_instance`` only pins ONE instance (call_count == 1), so a mutant
    that hoists the cache to a class-/module-level attribute would pass it; this pins TWO instances.
    Mutant-red: sharing the verdict across instances makes the seam called ONCE, not once per
    instance (assert 2 → 1)."""
    seam = _stub_identity(monkeypatch, verified=True)
    for _ in range(2):
        client = _client_for_negative(monkeypatch, 404)
        assert client.validate_name("X:x")["registered"] is False
    assert seam.call_count == 2


def test_definitive_negative_is_not_cached_refetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """S13 / DS-2 (QA F6): a definitive negative RAISES before the ``_names_cache`` write, so the
    SAME name is re-queried on every lookup — a name may get registered later, and a cached negative
    would go stale (worse: replay as an S11 no-status withhold). No existing test pins this: the
    cache test loops over DISTINCT names. Mutant-red: writing ``_names_cache`` before the raise
    makes the second same-name lookup a cache hit, so the deviceNames GET is issued once, not N
    times (assert 3 → 1)."""
    _stub_identity(monkeypatch, verified=True)
    client = NamingServiceClient(base_url="http://naming.example/")
    get = Mock(return_value=_resp({}, status=404))
    monkeypatch.setattr(client.session, "get", get)
    for _ in range(3):
        assert client.validate_name("SAME:name")["registered"] is False
    assert get.call_count == 3
