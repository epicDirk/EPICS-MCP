"""Offline tests for the Olog WRITE surface, gate, URL boundary, allowlist, audit privacy, client.

No network. Covers the OlogWriteGate (env gate + test-server URL boundary + logbook allowlist +
rate limit + privacy-clean audit), the client PUT path (JSON shape, error mapping) and
the tool/service orchestration (disabled path, enabled path, audit ALLOW/FAILED). The person-name
regression (a person named in the free-text title/description NEVER reaches the audit) is the most
important test of the phase. All host/URL/person tokens are SYNTHETIC (facility-agnostic guard).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock

import pytest
import requests

import epics_mcp.config as config_module
import epics_mcp.olog_safety as olog_safety_module
from epics_mcp.config import EpicsConfig
from epics_mcp.errors import (
    EpicsConnectionError,
    EpicsError,
    OlogWriteDeniedError,
    RateLimitError,
    SafetyConfigError,
)
from epics_mcp.olog_safety import OlogWriteGate
from epics_mcp.services._http import basic_auth_header
from epics_mcp.services.checkers import query_olog_create
from epics_mcp.services.olog_client import OlogClient
from epics_mcp.services.olog_exceptions import OlogResponseError
from epics_mcp.tools.olog import _create_log_entry, _reply_to_log

_AUDIT_LOGGER = "epics_mcp.olog_audit"


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

    def test_env_denial_details_name_what_the_caller_already_knew(self) -> None:
        """The env denial's ``details`` follows the caller's knowledge, not the method split.

        Splitting the env + URL checks into ``check_write_env_and_url`` (2026-08-01, so the
        round-tripping tools can deny BEFORE their pre-write read) reshaped this payload for EVERY
        caller, including ``create_log_entry``, whose gate that change was not about. No test and
        no doc read the field, so nothing went red. Both shapes are pinned here.

        RED-PROOF: drop the ``logbooks`` argument of ``check_write_env_and_url`` again, and the
        create half of this test fails.
        """
        gate = OlogWriteGate(_write_config(allow_olog_write=False))

        with pytest.raises(OlogWriteDeniedError) as create_denial:
            gate.check_write_allowed(["Ops"])
        assert create_denial.value.details == {"logbooks": ["Ops"]}

        # A round-tripping caller has NOT read its target yet, so it names itself instead: the
        # target's logbooks are exactly the thing this early denial refuses to go and fetch.
        with pytest.raises(OlogWriteDeniedError) as round_trip_denial:
            gate.check_write_env_and_url("add_log_attachment")
        assert round_trip_denial.value.details == {"caller": "add_log_attachment"}


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

    def test_boundary_refusal_carries_no_service_url(self) -> None:
        """The URL boundary refuses without echoing the URL it refused, in message OR details.

        This branch fires on the ORDINARY path: "remote and not allowlisted" is the gate's
        documented normal state, so nothing has to be broken for the refusal to be produced, and
        before this fix every such refusal handed the caller whatever the operator had spelled into
        ``EPICS_MCP_OLOG_URL``, userinfo included (BG-DERR-B).

        The exact message is asserted rather than "the password does not appear": a criterion built
        from the configured secret is blind wherever a transport re-encodes it (decision WY), and a
        substring check would also pass on a message that still printed the host. Its wire-level
        twin, the same text with the ``[OLOG_WRITE_DENIED]`` tag a caller sees, lives in
        ``tests/test_write_gate_contract.py``; both are literal on purpose.

        Why no redaction instead of no address, and the honest version of that reason: a redaction
        that is safe here does exist, ``shown_url``. What rules it out is not safety but posture,
        this server discloses the Olog target address to a caller on no surface at all,
        deliberately and in writing (``resources.py``: "olog as an enabled-boolean only (never the
        URL...)"), and a refusal is the same kind of surface kept by the same client.
        ⚠️ Neither of the other two would do, and this paragraph named the wrong one until
        2026-08-14. ``url_without_credentials`` REBUILDS from the parse, and on
        ``https://svc:p@ss/w0rd@host/Olog`` urllib3 reads host ``ss``, so the rebuild prints a
        fragment of the password in the path; that cost the ``epics-doctor`` write block a real
        leak. ``url_without_userinfo`` keeps a query-string token (``docs/known-limits.md`` 17).

        The escalation sentence is not decoration either. It mirrors ``safety.py``'s, and this
        branch needs it more than the PV gate does, because its remedy is a change to the server's
        own write configuration, which is exactly what an assistant must not make for a user.

        RED-PROOF: restore the pre-fix raise and every assertion here fails.
        """
        secret_url = "https://svc:s3cr3tP4ss@olog.example.org:8181/Olog"
        gate = OlogWriteGate(_write_config(olog_url=secret_url))

        with pytest.raises(OlogWriteDeniedError) as denial:
            gate.check_write_allowed(["Ops"])

        # Secret-agnostic first (see the WY note above), the wire contract second: written the
        # other way round the first would be unreachable behind the equality. The two service
        # schemes rather than a bare "://", because the message deliberately names the MCP resource
        # ``epics://health`` and a rule rejecting every scheme would forbid its own remedy.
        assert "http://" not in str(denial.value)
        assert "https://" not in str(denial.value)
        assert "@" not in str(denial.value)
        # Enough of the text to tell this branch from the gate's three other refusals, NOT the whole
        # string: the full wire contract is pinned once, at the boundary a caller actually reads it
        # from (``tests/test_write_gate_contract.py``). A second byte-for-byte copy here would be a
        # strictly weaker duplicate of that one and a third place to edit on every rewording.
        assert "the configured EPICS_MCP_OLOG_URL is not a permitted write target" in str(
            denial.value
        )
        # The escalation, asserted by its load-bearing phrases rather than by the whole string, so
        # this half stays meaningful if the sentence around it is reworded. Same shape as the PV
        # gate's guard in tests/test_safety.py.
        assert "Do NOT work around this" in str(denial.value)
        assert "repointing the server" in str(denial.value)
        assert "another logbook" in str(denial.value)
        assert "Report the refusal to the operator on duty" in str(denial.value)
        # The in-process copy follows the message. It reaches no client today (nothing in ``src/``
        # reads ``EpicsError.details``), so this is hygiene rather than a second leak closed, and
        # it is the shape the sibling env branch already uses for a round-tripping caller.
        assert denial.value.details == {"caller": "create_log_entry"}

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
        """SEC-2: an unparseable URL fails closed BEFORE the allowlist, which cannot save it.

        Regression guard for the shared-primitive refactor: the host extraction must stay an
        UP-FRONT veto. Rewriting the gate as "if is_loopback_url(): True; return allow_remote and
        url in allowlist" would let an unparseable-but-allowlisted URL through, because
        is_loopback_url() cannot distinguish "parsed fine, not loopback" from "did not parse".
        """
        gate = OlogWriteGate(
            _write_config(
                olog_url=url,
                olog_write_url_allowlist=url,  # exactly allowlisted...
                olog_write_allow_remote=True,  # ...and remote writes enabled
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
        # (Python 3.12+). It must be a clean, AUDITED OlogWriteDeniedError, not an uncaught crash.
        gate = OlogWriteGate(_write_config(olog_url="http://[::1]./Olog"))
        with (
            caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER),
            pytest.raises(OlogWriteDeniedError),
        ):
            gate.check_write_allowed(["Ops"])
        assert "event=DENY" in caplog.text

    def test_private_non_loopback_denied(self) -> None:
        # An RFC1918 private IP is NOT loopback: the ESS production Olog lives on a private net, so
        # "private = allowed" would defeat the prod NO-GO. Denied unless allowlisted + remote.
        gate = OlogWriteGate(_write_config(olog_url="http://10.0.0.5:8080/Olog"))
        with pytest.raises(OlogWriteDeniedError):
            gate.check_write_allowed(["Ops"])


# ======================================================================================
# OlogWriteGate: logbook allowlist (deny-all on empty, fail-closed like PV, in a DIFFERENT shape:
# PV refuses to START on an empty pattern, this gate constructs and denies at runtime. NOT an
# inverse, neither is fail-open; see the write-gate contract, point 2, in CLAUDE.md.)
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
        goes RED (admits==2) against the pre-S28 unlocked code, proven by the mutant on HEAD~1."""
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

    def test_the_bad_audit_path_refusal_names_the_variable_and_not_the_path(
        self, tmp_path: Path
    ) -> None:
        """BG-DPATH: the message a CALLER receives must not carry the local audit path.

        This gate is built LAZILY, on the first write, so unlike ``SafetyLayer``'s eager one its
        refusal is a tool ANSWER: measured through ``create_log_entry``, the caller used to receive
        ``[SAFETY_CONFIG_INVALID] Invalid EPICS_MCP_AUDIT_LOG_FILE`` plus the full path, handing
        a local account name to whoever called the tool. Same posture the URL boundary already
        takes for a value that can carry a credential.

        BOTH assertions are load-bearing and the second is the one that pays. The path stood TWICE
        in one f-string, once from ``!r`` and once inside ``{exc}``, because an ``OSError``
        stringifies as ``[Errno 2] No such file or directory: '<the path>'``. Dropping only the
        ``!r`` would have looked repaired and leaked exactly as before, so the segment is asserted
        against the WHOLE message rather than against the formatted prefix.

        Red-proof (each verified by mutating ``olog_safety.py``): put the ``!r`` back and the
        directory assertion fails; append ``{exc}`` and the same one fails; drop the variable NAME
        and the first assertion fails.

        ``details`` deliberately still carries the path: nothing in ``src/`` reads
        ``EpicsError.details``, and ``tool_errors.translate_epics_errors`` sends only the error code
        and ``str(exc)``, so it stays in-process for the operator who can act on it.
        """
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        secret_dir = tmp_path / "operator-account-name"
        try:
            cfg = EpicsConfig(audit_log_file=str(secret_dir / "audit.log"))
            with pytest.raises(SafetyConfigError) as excinfo:
                OlogWriteGate(cfg)
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

        message = str(excinfo.value)
        assert "EPICS_MCP_AUDIT_LOG_FILE" in message, (
            "the refusal must still NAME the variable, otherwise withholding the value leaves the "
            "reader with nothing to act on"
        )
        assert "operator-account-name" not in message, (
            f"the audit path leaked to the caller: {message!r}"
        )
        assert excinfo.value.details["audit_log_file"] == str(secret_dir / "audit.log"), (
            "the in-process detail must keep the path; it never crosses the tool boundary"
        )

    def test_audit_path_validated_on_repeated_construction(self, tmp_path: Path) -> None:
        # QA 2026-07-19 (OA1-QA #A3): the audit-path guard must not be skipped just because an
        # EARLIER gate already attached a handler to the process-global olog_audit logger. A later
        # OlogWriteGate with a broken audit path must STILL fail closed, mirrors
        # test_safety.py::test_audit_path_validated_on_repeated_construction (the 2026-07-17 fix
        # this makes OlogWriteGate symmetric to).
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            OlogWriteGate(EpicsConfig())  # first construction registers a stderr handler
            with pytest.raises(SafetyConfigError):
                OlogWriteGate(EpicsConfig(audit_log_file=str(tmp_path / "nope" / "audit.log")))
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_an_audit_path_that_is_not_a_path_fails_closed_too(self) -> None:
        """Symmetric to ``test_safety.py``: two broken paths used to escape the ``except``.

        A NUL byte raises ``ValueError`` out of the builtin ``open`` and a non-str raises
        ``TypeError`` out of ``os.fspath``, so a clause catching only ``OSError`` let both through
        and the gate died on a bare traceback where its own comment promises a SafetyConfigError.

        Red-proof: narrow the clause back to ``except OSError``.
        """
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            with pytest.raises(SafetyConfigError):  # ValueError out of the builtin open
                OlogWriteGate(EpicsConfig.model_construct(audit_log_file="audit\x00.log"))
            with pytest.raises(SafetyConfigError):  # TypeError out of os.fspath
                OlogWriteGate(EpicsConfig.model_construct(audit_log_file=3))  # type: ignore[arg-type]
        finally:
            audit.handlers.clear()
            audit.handlers.extend(saved)


class TestOlogAuditSink:
    """K1/K2 (symmetric to test_safety.py::TestAuditSink): the Olog audit FileHandler must encode
    UTF-8 AND stamp UTC. A non-ASCII character in an audit line (logbook name, level, title
    length, never free text) vanishes without trace under cp1252 unless ``encoding="utf-8"`` is
    set (``Handler.handleError`` swallows the ``UnicodeEncodeError``), and a local-time stamp is
    ambiguous when an incident is reconstructed.
    """

    def test_audit_file_handler_encodes_utf8(self, tmp_path: Path) -> None:
        # K1 (portable red proof): ``.encoding`` must be "utf-8" (without it, None).
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            gate = OlogWriteGate(EpicsConfig(audit_log_file=str(tmp_path / "olog-audit.log")))
            handler = gate._audit_handler
            assert isinstance(handler, logging.FileHandler)
            assert handler.encoding == "utf-8"
        finally:
            for h in audit.handlers[:]:
                h.close()
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_audit_line_with_unicode_units_survives(self, tmp_path: Path) -> None:
        # K1 (functional evidence): an audit line with non-ASCII characters reaches the file
        # unaltered. U+03A9 has no cp1252 mapping, so it is a safe red-proof trigger on Windows.
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            log_path = tmp_path / "olog-audit.log"
            gate = OlogWriteGate(EpicsConfig(audit_log_file=str(log_path)))
            probe = "OLOG_WRITE logbook=probe-50Ω title_len=12 μ äöü"
            gate._emit(probe)
            handler = gate._audit_handler
            assert handler is not None
            handler.flush()
            assert probe in log_path.read_text(encoding="utf-8")
        finally:
            for h in audit.handlers[:]:
                h.close()
            audit.handlers.clear()
            audit.handlers.extend(saved)

    def test_audit_formatter_stamps_utc(self, tmp_path: Path) -> None:
        # K2: the formatter converts to UTC (time.gmtime) and ends with a literal 'Z'.
        audit = logging.getLogger(_AUDIT_LOGGER)
        saved = audit.handlers[:]
        audit.handlers.clear()
        try:
            gate = OlogWriteGate(EpicsConfig(audit_log_file=str(tmp_path / "olog-audit.log")))
            handler = gate._audit_handler
            assert isinstance(handler, logging.FileHandler)
            formatter = handler.formatter
            assert formatter is not None
            assert formatter.converter is time.gmtime
            record = logging.LogRecord(_AUDIT_LOGGER, logging.INFO, __file__, 1, "m", None, None)
            assert formatter.formatTime(record, formatter.datefmt).endswith("Z")
        finally:
            for h in audit.handlers[:]:
                h.close()
            audit.handlers.clear()
            audit.handlers.extend(saved)


# ======================================================================================
# OlogClient.create_log_entry: JSON shape, whole response, error mapping
# ======================================================================================


class TestCreateClient:
    def test_builds_correct_json_and_returns_whole_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = OlogClient("http://olog:8080/Olog", auth_header="Basic dXNlcjpwYXNz")
        captured: dict[str, object] = {}

        def _put(url: str, **kwargs: object) -> Mock:
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["params"] = kwargs.get("params")
            captured["headers"] = kwargs.get("headers")
            # a FULL server response with owner + free text, returned whole
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
        assert headers["X-Olog-Client-Info"] == "epics-mcp"
        # auth rode on the dedicated WRITE session (where the PUT goes); the read session keeps it
        # too, byte-identical, a silent drop on either would 401 a secured server.
        assert client._write_session.headers.get("authorization") == "Basic dXNlcjpwYXNz"
        assert client.session.headers.get("authorization") == "Basic dXNlcjpwYXNz"
        # whole response: owner and free text in the clear, logbooks derived name-only
        assert entry["owner"] == "z.person"
        assert entry["title"] == "written by z.person"
        assert entry["description"] == "z.person did it"
        assert entry["logbooks"] == ["Ops"]

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
        # description is ALWAYS sent as a present string (empty here); Olog save path NPEs on null.
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
        """S11: a 2xx write response that is not a log entry must RAISE, any non-empty dict used
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
    """A fake OlogClient returning a canned create response (the shaping is pinned above)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def create_log_entry(self, **kwargs: object) -> dict[str, object]:
        return {"id": 99, "title": "Vacuum trip", "logbooks": ["Ops"]}

    def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
        # The create path checks a passed level against this before taking the rate token.
        return ["Info", "Problem", "Request"], "Info", None


class TestToolOrchestration:
    @pytest.mark.asyncio
    async def test_create_tool_disabled_no_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_module._config = EpicsConfig(olog_url="")
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _boom)
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
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _boom)
        with pytest.raises(OlogWriteDeniedError):
            await _create_log_entry(title="t", logbooks="Ops")

    @pytest.mark.asyncio
    async def test_create_tool_enabled_surfaces_the_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_module._config = _write_config()
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _FakeClient)
        result = await _create_log_entry(title="Vacuum trip", logbooks="Ops", description="d")
        assert result["enabled"] is True
        assert result["created"] is True
        entry = result["entry"]
        assert isinstance(entry, dict)
        assert entry["id"] == 99
        assert entry["title"] == "Vacuum trip"

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
                return {"id": 7, "title": "re", "logbooks": ["Ops"]}

        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _Fake)
        result = await _reply_to_log(log_id="42", title="re", logbooks="Ops")
        assert result["created"] is True
        assert captured["in_reply_to"] == "42"

    @pytest.mark.asyncio
    async def test_audit_allow_is_privacy_clean(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The core regression: a person named in the free-text title/description NEVER reaches the
        # audit, audit_write only ever sees title_len, never the text.
        config_module._config = _write_config()
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _FakeClient)
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

        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _FailingClient)
        with (
            caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER),
            # S11 §8: the server ANSWERED (a served 400), since the split this surfaces as
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


# ======================================================================================
# Create: level vocabulary (OQ1), the write-side counterpart to the update path's checks
#
# The refusals below are only correct while the SERVER does not validate the level itself, and
# that premise cannot be settled in memory. It is pinned live by
# tests/test_olog_write_live.py::test_server_does_not_validate_a_written_level, this module's
# live half. Named here because the pointer used to exist in one direction only: whoever
# arrived from the offline side found no trace of the live pin at all.
# ======================================================================================


class _LevelCountingClient:
    """Counts /levels lookups so a create that passes no level can be shown to make none."""

    levels_calls: ClassVar[int] = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
        _LevelCountingClient.levels_calls += 1
        return ["Info", "Problem", "Request"], "Info", None

    def create_log_entry(self, **kwargs: object) -> dict[str, object]:
        return {"id": 99, "title": "t", "logbooks": ["Ops"]}


class TestCreateLevelVocabulary:
    @pytest.mark.asyncio
    async def test_unknown_level_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF: create had the same hole as update, `level="Urgnet"` was stored verbatim and
        # the entry then matched no level filter. Checking only update would move the asymmetry.
        config_module._config = _write_config()
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _LevelCountingClient)
        with pytest.raises(EpicsError) as exc:
            await query_olog_create(title="t", logbooks=["Ops"], level="Urgnet")
        assert exc.value.error_code == "INVALID_INPUT"
        assert "Urgnet" in str(exc.value)

    @pytest.mark.asyncio
    async def test_blank_level_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_module._config = _write_config()
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _LevelCountingClient)
        with pytest.raises(EpicsError) as exc:
            await query_olog_create(title="t", logbooks=["Ops"], level="")
        assert exc.value.error_code == "INVALID_INPUT"
        assert "empty" in str(exc.value)

    @pytest.mark.asyncio
    async def test_bad_level_does_not_burn_a_rate_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE ordering property: the vocabulary check sits BEFORE check_write_allowed, so a typo
        # costs no token. This is the gate's own documented rule, not a local choice:
        # OlogWriteGate's docstring states that a denial never consumes a token, and the attachment
        # size cap (step 3b) sits ahead of the rate limit for exactly this reason. Same idiom as
        # test_sec3_empty_logbooks_denied_without_rate_token: with rate_limit=1, a valid create must
        # still succeed after repeated refusals.
        config_module._config = _write_config(olog_write_rate_limit=1)
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _LevelCountingClient)
        for _ in range(3):
            with pytest.raises(EpicsError):
                await query_olog_create(title="t", logbooks=["Ops"], level="Urgnet")
        result = await query_olog_create(title="t", logbooks=["Ops"], level="Problem")
        assert result["created"] is True  # the single token was still there
        with pytest.raises(RateLimitError):
            await query_olog_create(title="t", logbooks=["Ops"], level="Problem")

    @pytest.mark.asyncio
    async def test_create_without_level_makes_no_levels_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cost containment: a create that takes the server's default level must still make
        # exactly ONE HTTP call. Without this, OQ1 would have doubled the request count of the
        # most-used write tool for every caller, including those that never pass a level.
        config_module._config = _write_config()
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _LevelCountingClient)
        _LevelCountingClient.levels_calls = 0
        result = await query_olog_create(title="t", logbooks=["Ops"])
        assert result["created"] is True
        assert _LevelCountingClient.levels_calls == 0

    @pytest.mark.asyncio
    async def test_denied_write_never_builds_a_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The client moved AHEAD of the rate token, but it must stay BEHIND the cheap precondition
        # checks: a caller the gate rejects still gets no level-vocabulary oracle.
        config_module._config = EpicsConfig(
            olog_url="http://localhost:8080/Olog", allow_olog_write=False
        )
        monkeypatch.setattr("epics_mcp.services.checkers_olog.OlogClient", _boom)
        with pytest.raises(OlogWriteDeniedError):
            await query_olog_create(title="t", logbooks=["Ops"], level="Urgnet")


def test_write_session_is_distinct_from_the_shared_read_session() -> None:
    """K5 hardening pin: the READ session is now a PROCESS-SHARED cached session, but the Olog WRITE
    session must stay a SEPARATE per-instance session (build_write_session: no retries, trust_env
    off, a non-idempotent PUT must never be replayed, and must not inherit the read pool).
    A refactor collapsing write onto the shared read session would leak the read pool + 3-retry
    policy into writes; this goes red on that mutant."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    client = OlogClient("http://olog:8080/Olog")
    assert client._write_session is not client.session  # distinct objects
    # ...and it is a no-retry write session, not the shared 3-retry read session
    adapter = client._write_session.get_adapter("http://x")
    assert isinstance(adapter, HTTPAdapter)
    retries = adapter.max_retries
    total = retries.total if isinstance(retries, Retry) else retries
    assert total == 0
