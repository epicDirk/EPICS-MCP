"""Live verification of the REMOTE-HTTPS Olog write path (OA1c) — positive + negative control.

The OA1 live plan was loopback-only, so the remote-https multipart upload path (a non-loopback
https URL, CA from ``EPICS_MCP_CA_BUNDLE``, the env-independent ``_write_session``) was never
exercised. This closes that gap against a LOCAL self-signed TLS reverse proxy in front of the
loopback Olog sandbox — no real facility, a synthetic hostname (a ``*.localtest.me`` name that
resolves to 127.0.0.1) and a throwaway self-signed cert.

Opt-in: ``pytest -m live`` with the proxy + cert wired via env (see the module skipif below).

* **Positive:** a create-with-attachment through the FULL gate over the https proxy URL succeeds
  (the gate's remote lane: allowlist + ``OLOG_WRITE_ALLOW_REMOTE`` + https; the write session trusts
  the CA from config, not the env). The uploaded bytes are then read back byte-identically through a
  SEPARATE loopback whole-mode client — proving the https upload actually stored them.
* **Negative:** the SAME upload WITHOUT the CA bundle fails TLS verification (the self-signed cert
  is not in the system trust store) — the CA, not the URL alone, is what makes the write succeed.

⚠️ Download stays correctly REDACTED against the non-loopback URL (whole-mode needs loopback), so
this verifies the UPLOAD / write-TLS path; the byte cross-check reads back via loopback.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

import epics_pv_mcp.config as config_module
import epics_pv_mcp.olog_safety as olog_safety_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services._http import basic_auth_header, is_ssl_error
from epics_pv_mcp.services.checkers import query_olog_create
from epics_pv_mcp.services.olog_client import AttachmentUpload, OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError
from tests.live_gate import assert_live_available, live_demanded

_PROXY = os.environ.get("OA1C_PROXY_URL")  # e.g. https://olog.localtest.me:8443/Olog
_CA = os.environ.get("OA1C_CA_BUNDLE")  # path to the self-signed cert.pem
_USER = os.environ.get("OA1C_WRITE_USER", "")
_PASS = os.environ.get("OA1C_WRITE_PASSWORD", "")
_LOGBOOK = os.environ.get("OA1C_LOGBOOK", "")
_LOOPBACK = os.environ.get("OA1C_LOOPBACK_URL", "http://localhost:8080/Olog")

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is
    demanded (EPICS_MCP_REQUIRE_LIVE=1) and the proxy rig is not configured."""
    assert_live_available(
        bool(_PROXY and _CA and _USER and _PASS and _LOGBOOK),
        "remote-https Olog upload needs a local TLS proxy: OA1C_PROXY_URL + OA1C_CA_BUNDLE + "
        "OA1C_WRITE_USER + OA1C_WRITE_PASSWORD + OA1C_LOGBOOK",
        demanded=live_demanded(os.environ),
    )


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _remote_config(*, ca_bundle: str) -> EpicsConfig:
    """A config pointing Olog write at the non-loopback https proxy, CA from *ca_bundle* (may be
    empty to force system-trust-store verification — the negative control)."""
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
    """Reset the config + gate singletons before AND after each test (isolation)."""
    config_module._config = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    olog_safety_module._olog_safety = None


@pytest.mark.asyncio
async def test_https_upload_succeeds_with_ca_and_bytes_are_stored(tmp_path: Path) -> None:
    """POSITIVE: create-with-attachment over the https proxy through the full gate; then read the
    bytes back byte-identically via a loopback whole-mode client."""
    assert _CA is not None and _PROXY is not None
    config_module._config = _remote_config(ca_bundle=_CA)
    olog_safety_module._olog_safety = None

    probe = tmp_path / "oa1c.png"
    probe.write_bytes(_PNG)
    result = await query_olog_create(
        title="OA1c remote-https upload",
        logbooks=[_LOGBOOK],
        description="probe over https (offline test artifact)",
        attachments=[str(probe)],
    )
    assert result["created"] is True
    entry = result["entry"]
    assert isinstance(entry, dict)
    log_id = str(entry["id"])

    # Byte cross-check via a SEPARATE loopback whole-mode client: the https upload really stored the
    # bytes (a download against the non-loopback url would be correctly redacted).
    loopback = OlogClient(
        _LOOPBACK,
        timeout=15.0,
        auth_header=basic_auth_header(_USER, _PASS),
        assume_test_data=True,
        allow_attachment_download=True,
    )
    raw = loopback.get_raw_entry(log_id)
    assert raw is not None
    attachments = raw["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 1
    content, _n, _t = loopback.get_attachment(log_id, str(attachments[0]["filename"]))
    assert content == _PNG


@pytest.mark.asyncio
async def test_https_upload_fails_without_ca() -> None:
    """NEGATIVE: the same upload WITHOUT the CA bundle fails TLS verification — the self-signed cert
    is not trusted by the system store, so the write cannot proceed."""
    config_module._config = _remote_config(ca_bundle="")  # verify falls back to the system store
    olog_safety_module._olog_safety = None

    auth = basic_auth_header(_USER, _PASS)
    assert _PROXY is not None
    client = OlogClient(_PROXY, timeout=15.0, auth_header=auth)
    upload = AttachmentUpload(
        id=uuid.uuid4().hex, filename="neg.png", content=_PNG, content_type="image/png"
    )
    with pytest.raises(OlogConnectionError) as exc:
        client.create_log_entry(title="neg", logbooks=[_LOGBOOK], attachments=[upload])
    # not merely "unreachable" (the proxy IS up — the positive test connects) but a TLS/CA failure
    assert is_ssl_error(exc.value)
