"""Live verification of the REMOTE-HTTPS Olog write path, positive + negative control.

The loopback live plan never exercised the remote-https multipart upload path (a non-loopback
https URL, the CA from ``EPICS_MCP_CA_BUNDLE``, the env-independent write session). This closes
that gap against a LOCAL self-signed TLS reverse proxy in front of the loopback Olog sandbox: no
real facility, a synthetic hostname (a ``*.localtest.me`` name that resolves to 127.0.0.1) and a
throwaway self-signed certificate.

⛔ **What no mock can answer, and why this module exists at all.** ``tests/test_http.py`` holds the
DECISIONS in memory: that a configured CA bundle is applied, that ``trust_env`` is pinned off, that
an empty bundle falls back to certifi. What it cannot hold is whether the assembled session then
completes a real TLS handshake against a server presenting that CA. A ``verify`` argument that
stops reaching requests, or a session that silently falls back to the system store, passes every
in-memory test in this repository.

* **Positive:** a create-with-attachment through the FULL gate over the https proxy URL succeeds
  (the remote lane: allowlist + ``olog_write_allow_remote`` + https; the write session trusts the
  CA from config, not from the environment). The uploaded bytes are then read back
  byte-identically through a SEPARATE loopback client, so "the https upload stored them" is a
  measurement rather than a 2xx.
* **Negative:** the SAME upload WITHOUT the CA bundle fails TLS verification, because the
  self-signed certificate is not in the system trust store. The CA, not the URL alone, is what
  makes the write succeed.

Opt-in: ``pytest -m live`` with the proxy and certificate wired through the environment (see the
gate fixture below). The rig is deliberately EXTERNAL rather than built here, and that is a
decision rather than an omission: building the certificate in-process would need a cryptography
dependency this package does not declare, which would move ``uv.lock`` and pull the dependency-pin
guards into the radius of a test module. What the rig has to provide is stated here; how it is
stood up is the operator's business, and a proxy of some fifty stdlib lines plus one ``openssl``
invocation is enough.

⚠️ This module was deleted with ``c05a93f`` and rebuilt on 2026-08-29. What had made the rebuild a
rewrite rather than a revert is gone: the byte cross-check used to lean on a read posture that no
longer exists, and reads are whole today, so an ordinary client reads the bytes back. Two further
drifts the rebuild absorbed: ``query_olog_create`` now lives in ``services.checkers_olog``, and
``OlogClient`` no longer takes the two posture keyword arguments.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

import epics_mcp.config as config_module
import epics_mcp.olog_safety as olog_safety_module
from epics_mcp.config import EpicsConfig
from epics_mcp.services._http import basic_auth_header, is_ssl_error
from epics_mcp.services.checkers_olog import query_olog_create
from epics_mcp.services.olog_client import AttachmentUpload, OlogClient
from epics_mcp.services.olog_exceptions import OlogConnectionError
from tests.live_gate import assert_live_available, live_demanded

#: The rig, read once at import so the gate and the bodies share one snapshot.
_PROXY = os.environ.get("OA1C_PROXY_URL")  # e.g. https://olog.localtest.me:8443/Olog
_CA = os.environ.get("OA1C_CA_BUNDLE")  # path to the throwaway cert.pem
_USER = os.environ.get("OA1C_WRITE_USER", "")
_PASS = os.environ.get("OA1C_WRITE_PASSWORD", "")
_LOGBOOK = os.environ.get("OA1C_LOGBOOK", "")
_LOOPBACK = os.environ.get("OA1C_LOOPBACK_URL", "http://localhost:8080/Olog")

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate: skip silently by default, fail loudly when a live run is DEMANDED
    (``EPICS_MCP_REQUIRE_LIVE=1``) and the proxy rig is not configured.

    The prerequisites are the import-time module constants, the same snapshot the bodies use; only
    the DEMAND is read fresh. ``EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK`` is not among them: this module
    writes only to its named target and lays down no deny artifacts.
    """
    assert_live_available(
        bool(_PROXY and _CA and _USER and _PASS and _LOGBOOK),
        "remote-https Olog upload needs a local TLS proxy: OA1C_PROXY_URL + OA1C_CA_BUNDLE + "
        "OA1C_WRITE_USER + OA1C_WRITE_PASSWORD + OA1C_LOGBOOK",
        demanded=live_demanded(os.environ),
    )


#: A one-pixel PNG. Small enough to compare byte for byte in a failure message, and a real image
#: type, so the multipart part carries a content type the server does not have to guess.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _remote_config(*, ca_bundle: str) -> EpicsConfig:
    """A config pointing the Olog write at the non-loopback https proxy.

    *ca_bundle* may be empty, which is the negative control: ``verify`` then falls back to the
    system trust store, where a throwaway self-signed certificate is not.
    """
    assert _PROXY is not None
    return EpicsConfig(
        olog_url=_PROXY,
        ca_bundle=ca_bundle,
        allow_olog_write=True,
        olog_write_allow_remote=True,
        olog_write_url_allowlist=_PROXY,
        olog_write_logbooks=_LOGBOOK,
        olog_write_user=_USER,
        olog_write_password=_PASS,
    )


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset the config and gate singletons before AND after each test, so neither this module nor
    its neighbours inherit a remote-write posture."""
    config_module._config = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    olog_safety_module._olog_safety = None


@pytest.mark.asyncio
async def test_https_upload_succeeds_with_ca_and_bytes_are_stored(tmp_path: Path) -> None:
    """POSITIVE: create-with-attachment over the https proxy, through the full gate, and the bytes
    are then read back byte-identically over loopback.

    The read-back is the half a status code cannot give: a 2xx says the server accepted the
    request, not that the attachment survived the multipart hop.
    """
    assert _CA is not None and _PROXY is not None
    config_module._config = _remote_config(ca_bundle=_CA)
    olog_safety_module._olog_safety = None

    probe = tmp_path / "remote-https-probe.png"
    probe.write_bytes(_PNG)
    result = await query_olog_create(
        title="remote-https upload probe (offline test artifact)",
        logbooks=[_LOGBOOK],
        description="probe over the local TLS proxy (offline test artifact)",
        attachments=[str(probe)],
    )
    assert result["created"] is True, f"the gated create did not report success: {result}"
    entry = result["entry"]
    assert isinstance(entry, dict)
    log_id = str(entry["id"])

    # The cross-check runs over LOOPBACK on purpose: it is a different client, a different session
    # and a different transport from the one under test, so a bug in the write path cannot also
    # fabricate the confirmation.
    loopback = OlogClient(_LOOPBACK, timeout=15.0, auth_header=basic_auth_header(_USER, _PASS))
    raw = loopback.get_raw_entry(log_id)
    assert raw is not None, f"entry {log_id} was reported created and cannot be read back"
    attachments = raw["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 1, (
        f"expected exactly one attachment on {log_id}, got {attachments!r}"
    )
    content, _name, _type = loopback.get_attachment(log_id, str(attachments[0]["filename"]))
    assert content == _PNG, "the https upload stored bytes that differ from the ones sent"


@pytest.mark.asyncio
async def test_https_upload_fails_without_ca() -> None:
    """NEGATIVE: the SAME upload without the CA bundle fails TLS verification.

    Without this direction the positive test alone would also pass against a proxy whose
    certificate the system store happens to trust, which would say nothing about the bundle.
    """
    config_module._config = _remote_config(ca_bundle="")  # verify falls back to the system store
    olog_safety_module._olog_safety = None

    assert _PROXY is not None
    client = OlogClient(_PROXY, timeout=15.0, auth_header=basic_auth_header(_USER, _PASS))
    upload = AttachmentUpload(
        id=uuid.uuid4().hex, filename="neg.png", content=_PNG, content_type="image/png"
    )
    with pytest.raises(OlogConnectionError) as exc:
        client.create_log_entry(
            title="remote-https negative control (offline test artifact)",
            logbooks=[_LOGBOOK],
            attachments=[upload],
        )
    # Not merely "unreachable": the proxy IS up, the positive test connects to it through the same
    # address. What is asserted is that the failure is a TLS/CA one.
    assert is_ssl_error(exc.value), (
        f"the write failed, but not on TLS verification: {exc.value!r}. A plain connection error "
        "here would mean the proxy was down, and the negative control would prove nothing."
    )
