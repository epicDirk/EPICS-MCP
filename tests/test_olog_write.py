"""Offline tests for the Olog WRITE surface — gate, URL boundary, allowlist, audit privacy, client.

No network. Covers the OlogWriteGate (env gate + test-server URL boundary + logbook allowlist +
rate limit + privacy-clean audit), the client PUT path (JSON shape, redaction, error mapping) and
the tool/service orchestration (disabled path, enabled path, audit ALLOW/FAILED). The person-name
regression (a person named in the free-text title/description NEVER reaches the audit) is the most
important test of the phase. All host/URL/person tokens are SYNTHETIC (facility-agnostic guard).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import epics_pv_mcp.config as config_module
import epics_pv_mcp.olog_safety as olog_safety_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import (
    EpicsConnectionError,
    EpicsError,
    OlogWriteDeniedError,
    RateLimitError,
    SafetyConfigError,
)
from epics_pv_mcp.olog_safety import OlogWriteGate
from epics_pv_mcp.services._http import basic_auth_header
from epics_pv_mcp.services.checkers import query_olog_create
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogResponseError
from epics_pv_mcp.services.redact import FREETEXT_WITHHELD
from epics_pv_mcp.tools.olog import _create_log_entry, _reply_to_log

_AUDIT_LOGGER = "epics_pv_mcp.olog_audit"


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset the config and Olog-write-gate singletons for each test (so each builds fresh)."""
    config_module._config = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    olog_safety_module._olog_safety = None


def _write_config(
    *,
    olog_url: str = "http://localhost:8080/Olog",
    allow_olog_write: bool = True,
    olog_write_logbooks: str = "Ops",
    olog_write_url_allowlist: str = "",
    olog_write_allow_remote: bool = False,
    olog_write_rate_limit: int = 5,
) -> EpicsConfig:
    """A config with Olog write enabled against the loopback sandbox, plus keyword overrides."""
    return EpicsConfig(
        olog_url=olog_url,
        allow_olog_write=allow_olog_write,
        olog_write_logbooks=olog_write_logbooks,
        olog_write_url_allowlist=olog_write_url_allowlist,
        olog_write_allow_remote=olog_write_allow_remote,
        olog_write_rate_limit=olog_write_rate_limit,
        olog_write_user="epics-pv-logbook-svc",
        olog_write_password="pw",
    )


def _resp(payload: object) -> Mock:
    # is_redirect is set explicitly: on a bare Mock every attribute is truthy, so a 2xx double would
    # look like a redirect to the client's redirect guard.
    resp = Mock(is_redirect=False)
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _resp_status(status: int) -> Mock:
    """A response whose raise_for_status raises an HTTPError with *status* (mirrors requests)."""
    http_error = requests.exceptions.HTTPError(str(status))
    http_error.response = Mock(status_code=status)
    resp = Mock(is_redirect=False)
    resp.raise_for_status.side_effect = http_error
    return resp


def _boom(*args: object, **kwargs: object) -> OlogClient:
    raise AssertionError("client must not be constructed on this path")


# ======================================================================================
# basic_auth_header (DoD-F1)
# ======================================================================================


class TestBasicAuthHeader:
    def test_encodes_basic_credentials(self) -> None:
        # base64("user:pass") == "dXNlcjpwYXNz"
        assert basic_auth_header("user", "pass") == "Basic dXNlcjpwYXNz"

    def test_empty_user_yields_none(self) -> None:
        assert basic_auth_header("", "pass") is None

    def test_empty_password_yields_none(self) -> None:
        assert basic_auth_header("user", "") is None


# ======================================================================================
# OlogWriteGate: env gate
# ======================================================================================


class TestWriteGate:
    def test_denied_when_disabled(self) -> None:
        gate = OlogWriteGate(_write_config(allow_olog_write=False))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_allowed_when_enabled_loopback_and_allowlisted(self) -> None:
        gate = OlogWriteGate(_write_config())
        gate.check_write_allowed(["Ops"])  # must not raise


# ======================================================================================
# OlogWriteGate: test-server URL boundary (the critical check)
# ======================================================================================


class TestUrlBoundary:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/Olog",
            "http://127.0.0.1:8080/Olog",
            "http://[::1]:8080/Olog",
            "http://127.5.6.7:8080/Olog",  # anywhere in 127.0.0.0/8 is loopback
        ],
    )
    def test_loopback_allowed(self, url: str) -> None:
        gate = OlogWriteGate(_write_config(olog_url=url))
        gate.check_write_allowed(["Ops"])  # must not raise

    def test_non_loopback_denied_without_allowlist(self) -> None:
        gate = OlogWriteGate(_write_config(olog_url="https://olog.example.org/Olog"))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_non_loopback_denied_with_allowlist_but_no_remote_flag(self) -> None:
        # Allowlisted but EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE not set → still denied.
        gate = OlogWriteGate(
            _write_config(
                olog_url="https://olog.example.org/Olog",
                olog_write_url_allowlist="https://olog.example.org/Olog",
                olog_write_allow_remote=False,
            )
        )
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_non_loopback_allowed_with_allowlist_and_remote(self) -> None:
        gate = OlogWriteGate(
            _write_config(
                olog_url="https://olog.example.org/Olog",
                olog_write_url_allowlist="https://olog.example.org/Olog",
                olog_write_allow_remote=True,
            )
        )
        gate.check_write_allowed(["Ops"])  # must not raise (https remote = the positive control)

    def test_remote_http_write_refused_even_when_allowlisted(self) -> None:
        """S23/N03: an allowlisted REMOTE target must be https. A plain-http Basic-auth write to a
        real server exposes the service-account credentials on the wire (and to any inherited
        proxy). Loopback stays http-OK (the local sandbox); only the remote lane is tightened."""
        gate = OlogWriteGate(
            _write_config(
                olog_url="http://olog.example.org/Olog",
                olog_write_url_allowlist="http://olog.example.org/Olog",
                olog_write_allow_remote=True,
            )
        )
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    @pytest.mark.parametrize("url", ["garbage", "http://[::1]./Olog", ""])
    def test_sec2_unparseable_url_denied_even_when_allowlisted(self, url: str) -> None:
        """SEC-2: an unparseable URL fails closed BEFORE the allowlist — which cannot save it.

        Regression guard for the shared-primitive refactor: the host extraction must stay an
        UP-FRONT veto. Rewriting the gate as "if is_loopback_url(): True; return allow_remote and
        url in allowlist" would let an unparseable-but-allowlisted URL through, because
        is_loopback_url() cannot distinguish "parsed fine, not loopback" from "did not parse".
        """
        gate = OlogWriteGate(
            _write_config(
                olog_url=url,
                olog_write_url_allowlist=url,  # exactly allowlisted…
                olog_write_allow_remote=True,  # …and remote writes enabled
            )
        )
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_sec1_userinfo_at_bypass_is_denied(self) -> None:
        # SEC-1: the hostname is olog.example.org (userinfo 127.0.0.1@ is NOT the host) → denied.
        # A substring/netloc check would have wrongly treated this as loopback → a production write.
        gate = OlogWriteGate(_write_config(olog_url="http://127.0.0.1@olog.example.org/Olog"))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_sec2_garbage_url_denied_no_hostname(self) -> None:
        # SEC-2: a non-empty but hostless/garbage URL → hostname None/"" → fail-closed deny.
        gate = OlogWriteGate(_write_config(olog_url="garbage"))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_sec2_malformed_ipv6_url_denied_not_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # SEC-2 (QA-hardened): urlparse RAISES ValueError on a malformed bracketed-IPv6 authority
        # (Python 3.12+). It must be a clean, AUDITED OlogWriteDeniedError — not an uncaught crash.
        gate = OlogWriteGate(_write_config(olog_url="http://[::1]./Olog"))
        with (
            caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER),
            pytest.raises(OlogWriteDeniedError),
        ):
            gate.check_write_allowed(["Ops"])
        assert "event=DENY" in caplog.text

    def test_private_non_loopback_denied(self) -> None:
        # An RFC1918 private IP is NOT loopback — the ESS production Olog lives on a private net, so
        # "private = allowed" would defeat the prod NO-GO. Denied unless allowlisted + remote.
        gate = OlogWriteGate(_write_config(olog_url="http://10.0.0.5:8080/Olog"))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])


# ======================================================================================
# OlogWriteGate: logbook allowlist (deny-all on empty — the INVERSE of PV)
# ======================================================================================


class TestLogbookAllowlist:
    def test_empty_allowlist_denies_all(self) -> None:
        gate = OlogWriteGate(_write_config(olog_write_logbooks=""))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])

    def test_logbook_not_in_allowlist_denied(self) -> None:
        gate = OlogWriteGate(_write_config(olog_write_logbooks="Ops"))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Controls"])

    def test_subset_allowed(self) -> None:
        gate = OlogWriteGate(_write_config(olog_write_logbooks="Ops,Controls"))
        gate.check_write_allowed(["Ops"])  # must not raise

    def test_sec3_empty_logbooks_denied_without_rate_token(self) -> None:
        # SEC-3: an empty logbooks list slips through `set() <= frozenset()` == True → guard first,
        # and it must NOT burn a rate token: with rate_limit=1, a valid write still succeeds after.
        gate = OlogWriteGate(_write_config(olog_write_rate_limit=1))
        for _ in range(3):
            with pytest.raises(OlogWriteDeniedError):
                gate.check_write_allowed([])
        gate.check_write_allowed(["Ops"])  # the single token is still available
        with pytest.raises(RateLimitError):
            gate.check_write_allowed(["Ops"])


# ======================================================================================
# OlogWriteGate: rate limit
# ======================================================================================


class TestRateLimit:
    def test_rate_limit_exceeded(self) -> None:
        gate = OlogWriteGate(_write_config(olog_write_rate_limit=2))
        gate.check_write_allowed(["Ops"])
        gate.check_write_allowed(["Ops"])
        with pytest.raises(RateLimitError):
            gate.check_write_allowed(["Ops"])

    def test_deny_consumes_no_rate_token(self) -> None:
        # Denied writes (logbook not in allowlist) must not consume tokens.
        gate = OlogWriteGate(_write_config(olog_write_logbooks="Ops", olog_write_rate_limit=2))
        for _ in range(3):
            with pytest.raises(OlogWriteDeniedError):
                gate.check_write_allowed(["Controls"])
        gate.check_write_allowed(["Ops"])
        gate.check_write_allowed(["Ops"])
        with pytest.raises(RateLimitError):
            gate.check_write_allowed(["Ops"])

    def test_rate_limit_token_acquisition_is_atomic(
        self, concurrent_admit_count: Callable[..., int]
    ) -> None:
        """S28: two concurrent create_log_entry run in DIFFERENT worker threads (this gate is called
        under asyncio.to_thread), so the purge->len-check->append MUST be atomic. With limit=1 the
        rendezvous forces the check->append interleaving; exactly ONE write is admitted. This test
        goes RED (admits==2) against the pre-S28 unlocked code — proven by the mutant on HEAD~1."""
        gate = OlogWriteGate(_write_config(olog_write_rate_limit=1))
        admits = concurrent_admit_count(gate, lambda: gate.check_write_allowed(["Ops"]))
        assert admits == 1


# ======================================================================================
# OlogWriteGate: audit records (privacy-clean)
# ======================================================================================


class TestGateAuditRecords:
    def test_deny_emits_deny_record_no_payload(self, caplog: pytest.LogCaptureFixture) -> None:
        gate = OlogWriteGate(_write_config(allow_olog_write=False))
        with (
            caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER),
            pytest.raises(OlogWriteDeniedError),
        ):
            gate.check_write_allowed(["Ops"], caller="create_log_entry")
        assert "event=DENY" in caplog.text
        assert "error_code=OLOG_WRITE_DENIED" in caplog.text
        assert "event=ALLOW" not in caplog.text

    def test_audit_write_records_metadata_only(self, caplog: pytest.LogCaptureFixture) -> None:
        gate = OlogWriteGate(_write_config())
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            gate.audit_write(
                entry_id="99",
                logbooks=["Ops"],
                level="Info",
                title_len=12,
                owner="epics-pv-logbook-svc",
            )
        assert "event=ALLOW" in caplog.text
        assert "logbooks=Ops" in caplog.text
        assert "entry_id=99" in caplog.text
        assert "owner=epics-pv-logbook-svc" in caplog.text
        assert "title_len=12" in caplog.text

    def test_audit_write_failed_has_no_entry_id_or_owner(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # SEC-5: a FAILED record carries no entry_id (none exists) and no owner.
        gate = OlogWriteGate(_write_config())
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            gate.audit_write_failed(
                logbooks=["Ops"], level=None, title_len=5, error_code="OLOG_HTTP_400"
            )
        assert "event=FAILED" in caplog.text
        assert "error_code=OLOG_HTTP_400" in caplog.text
        assert "entry_id=" not in caplog.text
        assert "owner=" not in caplog.text

    def test_reply_audit_carries_in_reply_to(self, caplog: pytest.LogCaptureFixture) -> None:
        gate = OlogWriteGate(_write_config())
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            gate.audit_write(
                entry_id="100",
                logbooks=["Ops"],
                level=None,
                title_len=3,
                owner="svc",
                in_reply_to="42",
                caller="reply_to_log",
            )
        assert "in_reply_to=42" in caplog.text
        assert "caller=reply_to_log" in caplog.text


# ======================================================================================
# OlogWriteGate: fail-closed config
# ======================================================================================


class TestGateConfigFailClosed:
    def test_bad_rate_limit_raises_safety_config_error(self) -> None:
        cfg = EpicsConfig.model_construct(olog_write_rate_limit=-1)
        with pytest.raises(SafetyConfigError):
            OlogWriteGate(cfg)

    def test_bad_audit_path_raises_safety_config_error(self, tmp_path: Path) -> None:
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            cfg = EpicsConfig(audit_log_file=str(tmp_path / "nope" / "audit.log"))
            with pytest.raises(SafetyConfigError):
                OlogWriteGate(cfg)
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)


# ======================================================================================
# OlogClient.create_log_entry: JSON shape, redaction, error mapping
# ======================================================================================


class TestCreateClient:
    def test_builds_correct_json_and_redacts_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = OlogClient("http://olog:8080/Olog", auth_header="Basic dXNlcjpwYXNz")
        captured: dict[str, object] = {}

        def _put(url: str, **kwargs: object) -> Mock:
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["params"] = kwargs.get("params")
            captured["headers"] = kwargs.get("headers")
            # a FULL server response with owner + free text — must be redacted before return
            return _resp(
                {
                    "id": 5,
                    "owner": "z.person",
                    "title": "written by z.person",
                    "description": "z.person did it",
                    "logbooks": [{"name": "Ops", "owner": "z.person"}],
                }
            )

        monkeypatch.setattr(client._write_session, "put", _put)
        entry = client.create_log_entry(
            title="Vacuum trip",
            logbooks=["Ops"],
            description="details",
            level="Info",
            tags=["vacuum"],
        )
        assert captured["url"] == "http://olog:8080/Olog/logs"
        body = captured["json"]
        assert isinstance(body, dict)
        assert body["title"] == "Vacuum trip"
        assert body["logbooks"] == [{"name": "Ops"}]
        assert body["description"] == "details"
        assert body["level"] == "Info"
        assert body["tags"] == [{"name": "vacuum"}]
        assert captured["params"] is None  # a create sends no query params
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["X-Olog-Client-Info"] == "epics-pv-mcp"
        # auth rode on the dedicated WRITE session (where the PUT goes); the read session keeps it
        # too, byte-identical — a silent drop on either would 401 a secured server.
        assert client._write_session.headers.get("authorization") == "Basic dXNlcjpwYXNz"
        assert client.session.headers.get("authorization") == "Basic dXNlcjpwYXNz"
        # redaction: owner dropped, free text withheld, logbook name-only, NO person name leaks
        assert "owner" not in entry
        assert entry["title"] == FREETEXT_WITHHELD
        assert entry["description"] == FREETEXT_WITHHELD
        assert entry["logbooks"] == ["Ops"]
        assert "z.person" not in str(entry)

    def test_reply_sends_in_reply_to_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = OlogClient("http://olog:8080/Olog")
        captured: dict[str, object] = {}

        def _put(url: str, **kwargs: object) -> Mock:
            captured["params"] = kwargs.get("params")
            captured["json"] = kwargs.get("json")
            return _resp({"id": 6, "title": "t", "logbooks": [{"name": "Ops"}]})

        monkeypatch.setattr(client._write_session, "put", _put)
        client.create_log_entry(title="re", logbooks=["Ops"], in_reply_to="42")
        assert captured["params"] == {"inReplyTo": "42"}
        # description is ALWAYS sent as a present string (empty here) — Olog save path NPEs on null.
        body = captured["json"]
        assert isinstance(body, dict)
        assert body["description"] == ""

    def test_http_400_maps_to_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = OlogClient("http://olog:8080/Olog")
        monkeypatch.setattr(client._write_session, "put", Mock(return_value=_resp_status(400)))
        with pytest.raises(OlogResponseError, match="400"):
            client.create_log_entry(title="t", logbooks=["Nope"])

    def test_http_401_maps_to_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = OlogClient("http://olog:8080/Olog")
        monkeypatch.setattr(client._write_session, "put", Mock(return_value=_resp_status(401)))
        with pytest.raises(OlogResponseError, match="401"):
            client.create_log_entry(title="t", logbooks=["Ops"])

    def test_http_500_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = OlogClient("http://olog:8080/Olog")
        monkeypatch.setattr(client._write_session, "put", Mock(return_value=_resp_status(500)))
        with pytest.raises(OlogResponseError) as exc_info:
            client.create_log_entry(title="t", logbooks=["Ops"])
        assert "400" not in str(exc_info.value)  # NOT re-labelled as a 400

    def test_empty_response_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = OlogClient("http://olog:8080/Olog")
        monkeypatch.setattr(client._write_session, "put", Mock(return_value=_resp({})))
        with pytest.raises(OlogResponseError):
            client.create_log_entry(title="t", logbooks=["Ops"])

    def test_response_without_entry_identity_is_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S11: a 2xx write response that is not a log entry must RAISE — any non-empty dict used
        to be PROJECTED as the created entry (a fabricated write confirmation). The measured
        entry record always carries ``id``."""
        client = OlogClient("http://olog:8080/Olog")
        monkeypatch.setattr(
            client._write_session, "put", Mock(return_value=_resp({"unexpected": "shape"}))
        )
        with pytest.raises(OlogResponseError):
            client.create_log_entry(title="t", logbooks=["Ops"])


# ======================================================================================
# Service/tool orchestration: disabled, enabled, audit end-to-end
# ======================================================================================


class _FakeClient:
    """A fake OlogClient returning an already-redacted create response (redaction pinned above)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def create_log_entry(self, **kwargs: object) -> dict[str, object]:
        return {"id": 99, "title": FREETEXT_WITHHELD, "logbooks": ["Ops"]}


class TestToolOrchestration:
    @pytest.mark.asyncio
    async def test_create_tool_disabled_no_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_module._config = EpicsConfig(olog_url="")
        monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
        result = await _create_log_entry(title="t", logbooks="Ops")
        assert result["enabled"] is False
        assert result["created"] is False

    @pytest.mark.asyncio
    async def test_create_tool_denied_when_write_gate_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # URL set (not the disabled path) but ALLOW_OLOG_WRITE off → gate denies, no client built.
        config_module._config = EpicsConfig(
            olog_url="http://localhost:8080/Olog", allow_olog_write=False
        )
        monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _boom)
        with pytest.raises(OlogWriteDeniedError):
            await _create_log_entry(title="t", logbooks="Ops")

    @pytest.mark.asyncio
    async def test_create_tool_enabled_surfaces_redacted_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_module._config = _write_config()
        monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeClient)
        result = await _create_log_entry(title="Vacuum trip", logbooks="Ops", description="d")
        assert result["enabled"] is True
        assert result["created"] is True
        entry = result["entry"]
        assert isinstance(entry, dict)
        assert entry["id"] == 99
        assert entry["title"] == FREETEXT_WITHHELD

    @pytest.mark.asyncio
    async def test_reply_tool_threads_and_bad_id_is_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_module._config = _write_config()
        captured: dict[str, object] = {}

        class _Fake:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            def create_log_entry(self, **kwargs: object) -> dict[str, object]:
                captured["in_reply_to"] = kwargs.get("in_reply_to")
                return {"id": 7, "title": FREETEXT_WITHHELD, "logbooks": ["Ops"]}

        monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _Fake)
        result = await _reply_to_log(log_id="42", title="re", logbooks="Ops")
        assert result["created"] is True
        assert captured["in_reply_to"] == "42"

    @pytest.mark.asyncio
    async def test_audit_allow_is_privacy_clean(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The core regression: a person named in the free-text title/description NEVER reaches the
        # audit — audit_write only ever sees title_len, never the text.
        config_module._config = _write_config()
        monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FakeClient)
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            result = await query_olog_create(
                title="Vacuum trip found by z.person",
                logbooks=["Ops"],
                description="z.person restarted the IOC; ask y.person",
            )
        assert result["created"] is True
        assert "event=ALLOW" in caplog.text
        assert "logbooks=Ops" in caplog.text
        assert "entry_id=99" in caplog.text
        assert "owner=epics-pv-logbook-svc" in caplog.text
        # NEGATIVE: neither the free text nor any person name leaks into the audit
        assert "z.person" not in caplog.text
        assert "y.person" not in caplog.text
        assert "Vacuum trip" not in caplog.text
        assert "restarted" not in caplog.text

    @pytest.mark.asyncio
    async def test_audit_failed_is_privacy_clean(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_module._config = _write_config()

        class _FailingClient:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            def create_log_entry(self, **kwargs: object) -> dict[str, object]:
                raise OlogResponseError("Olog rejected the entry (HTTP 400)")

        monkeypatch.setattr("epics_pv_mcp.services.checkers.OlogClient", _FailingClient)
        with (
            caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER),
            # S11 §8: the server ANSWERED (a served 400) — since the split this surfaces as
            # EpicsError, no longer relabelled EpicsConnectionError ("cannot reach Olog", which
            # sent the operator retrying against an outage that was not happening).
            pytest.raises(EpicsError) as excinfo,
        ):
            await query_olog_create(
                title="trip by z.person",
                logbooks=["Ops"],
                description="z.person did it",
            )
        assert not isinstance(excinfo.value, EpicsConnectionError)
        assert "event=FAILED" in caplog.text
        assert "logbooks=Ops" in caplog.text
        assert "error_code=" in caplog.text
        # NEGATIVE: no free text, no person name, no entry_id, no owner in the FAILED record
        assert "z.person" not in caplog.text
        assert "trip by" not in caplog.text
        assert "entry_id=" not in caplog.text
        assert "owner=" not in caplog.text
