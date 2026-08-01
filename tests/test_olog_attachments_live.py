"""Live round-trip for Olog ATTACHMENTS (OA1), the differential a mock cannot carry.

Opt-in: ``pytest -m live`` against a WRITABLE loopback Olog sandbox with attachment download on.
Uploads a real PNG + a non-image file via multipart, downloads each back (by name AND by GridFS
id), and asserts the bytes are BYTE-IDENTICAL, the one thing that proves the real server's
multipart parsing, filename↔metadata pairing, GridFS storage and streaming download all agree with
this client (no mock ever sees the server side). All content/tokens are synthetic.
"""

from __future__ import annotations

import base64
import os
import uuid

import pytest

from epics_mcp.services._http import basic_auth_header
from epics_mcp.services.olog_client import AttachmentUpload, OlogClient
from tests.live_gate import assert_live_available, live_demanded

_URL = os.environ.get("EPICS_MCP_OLOG_URL")
_WRITE = os.environ.get("EPICS_MCP_ALLOW_OLOG_WRITE", "").lower() == "true"
_LOGBOOKS = os.environ.get("EPICS_MCP_OLOG_WRITE_LOGBOOKS", "")
_DOWNLOAD = os.environ.get("EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD", "").lower() == "true"
_WRITE_USER = os.environ.get("EPICS_MCP_OLOG_WRITE_USER")
_WRITE_PASSWORD = os.environ.get("EPICS_MCP_OLOG_WRITE_PASSWORD")
# A SECOND principal, distinct from the write service account. Only with two accounts can the
# server's owner re-stamp become visible at all (see test_add_attachment_restamps_the_owner).
_OTHER_USER = os.environ.get("EPICS_MCP_OLOG_TEST_OTHER_USER")
_OTHER_PASSWORD = os.environ.get("EPICS_MCP_OLOG_TEST_OTHER_PASSWORD")

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is
    demanded (EPICS_MCP_REQUIRE_LIVE=1) and the plane is not configured.

    QA fix: the gate must include the WRITE CREDS, the client fixture reads them with
    ``os.environ[...]``, so a missing credential used to KeyError at setup instead of
    skipping (undemanded) / failing with the reason (demanded). The prerequisites are the
    import-time module constants (the same values the test bodies use, one consistent
    snapshot); only the DEMAND is read fresh.
    """
    assert_live_available(
        bool(_URL and _WRITE and _LOGBOOKS and _DOWNLOAD and _WRITE_USER and _WRITE_PASSWORD),
        "live attachment round-trip needs a WRITABLE loopback Olog with attachment download "
        "enabled: EPICS_MCP_OLOG_URL + _ALLOW_OLOG_WRITE + _WRITE_LOGBOOKS + "
        "_ALLOW_ATTACHMENT_DOWNLOAD + write creds (_WRITE_USER/_WRITE_PASSWORD)",
        demanded=live_demanded(os.environ),
    )


# A real 1x1 transparent PNG (so the server sees genuine image bytes) + an arbitrary non-image blob.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_BLOB = b"<display version='2.0.0'><name>oa1-probe</name></display>"


@pytest.fixture
def client() -> OlogClient:
    """A write+download-capable client for the loopback sandbox."""
    auth = basic_auth_header(
        os.environ["EPICS_MCP_OLOG_WRITE_USER"], os.environ["EPICS_MCP_OLOG_WRITE_PASSWORD"]
    )
    assert _URL is not None  # guarded by the module gate
    return OlogClient(_URL, timeout=15.0, auth_header=auth, allow_attachment_download=True)


def _upload(content: bytes, name: str, content_type: str | None) -> AttachmentUpload:
    """Build an upload with a fresh UUID + the id-prefixed unique filename (as the checker does)."""
    uid = uuid.uuid4().hex
    return AttachmentUpload(
        id=uid, filename=f"{uid}_{name}", content=content, content_type=content_type
    )


def test_attachment_round_trip_is_byte_identical(client: OlogClient) -> None:
    """Create-with-attachments → download each back → bytes must match exactly.

    Covers both file types (image + non-image), both download routes (by log+filename and by GridFS
    id), and the entry read surfacing both attachments. A byte mismatch here would mean
    the multipart framing, the filename↔part pairing, or the download encoding is wrong, none of
    which an offline mock can observe.
    """
    logbook = _LOGBOOKS.split(",")[0].strip()
    png = _upload(_PNG, "oa1-probe.png", "image/png")
    blob = _upload(_BLOB, "oa1-probe.bob", None)

    entry = client.create_log_entry(
        title="OA1 attachment round-trip",
        logbooks=[logbook],
        description="probe upload (offline test artifact)",
        attachments=[png, blob],
    )
    log_id = str(entry["id"])

    # by-name download of each attachment: the bytes must come back exactly as sent
    for attachment in (png, blob):
        content, _server_name, _content_type = client.get_attachment(log_id, attachment["filename"])
        assert content == attachment["content"], f"byte mismatch for {attachment['filename']}"

    # by-id download of the image (the inline-image route) must also be byte-identical
    by_id, _server_name, _content_type = client.get_attachment_by_id(png["id"])
    assert by_id == _PNG

    # the entry read surfaces both stored attachments
    fetched = client.get_log_entry(log_id)
    assert fetched is not None
    raw = fetched.get("attachments")
    assert isinstance(raw, list)
    assert len(raw) == 2


def test_download_is_withheld_without_the_flag() -> None:
    """The download opt-in, live: a client WITHOUT the flag refuses to hand back bytes even
    against the same sandbox; the flag unlocks byte egress."""
    from epics_mcp.services.olog_exceptions import OlogAttachmentDownloadDenied

    assert _URL is not None
    no_flag = OlogClient(_URL, timeout=15.0, allow_attachment_download=False)
    with pytest.raises(OlogAttachmentDownloadDenied):
        no_flag.get_attachment("1", "whatever.png")


def test_add_attachment_is_additive_and_byte_identical(client: OlogClient) -> None:
    """OA1b: create (1 attachment) → add_attachment (a 2nd) → the entry KEEPS both attachments AND
    its title/body/logbooks, and both download byte-identically.

    This is the differential no mock can carry: the server's POST /logs/multipart runs a destructive
    updateLog (retainAll-prunes any attachment not resubmitted, overwrites the fields). Only a real
    round-trip against a real server proves the attach is purely additive. All content is synthetic.
    """
    logbook = _LOGBOOKS.split(",")[0].strip()
    first = _upload(_PNG, "oa1b-first.png", "image/png")
    entry = client.create_log_entry(
        title="OA1b additive attach",
        logbooks=[logbook],
        description="original **body** (offline test artifact)",
        attachments=[first],
    )
    log_id = str(entry["id"])

    # attach a SECOND file to the EXISTING entry via the round-trip
    raw = client.get_raw_entry(log_id)
    assert raw is not None
    second = _upload(_BLOB, "oa1b-second.bob", None)
    client.add_attachment(log_id, raw, [second])

    # the entry now carries BOTH attachments, existing preserved (anti-retainAll), new added
    after = client.get_raw_entry(log_id)
    assert after is not None
    attachments = after["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 2
    names = {a["filename"] for a in attachments}
    assert first["filename"] in names and second["filename"] in names

    # and every CONTENT field is unchanged (updateLog would wipe a field not round-tripped):
    # anti-overwrite. NOT every field: `owner` IS re-stamped by the server on every call to this
    # endpoint. This test cannot see that, because it creates and attaches as the SAME account, so
    # before and after are identical either way, see
    # test_add_attachment_restamps_the_owner, which uses a second principal to make it visible.
    assert after["title"] == "OA1b additive attach"
    assert "original" in str(after["source"])
    logbooks = after["logbooks"]
    assert isinstance(logbooks, list)
    assert [lb["name"] for lb in logbooks] == [logbook]

    # and both attachments are byte-identical
    b1, _n1, _t1 = client.get_attachment(log_id, first["filename"])
    b2, _n2, _t2 = client.get_attachment(log_id, second["filename"])
    assert b1 == _PNG
    assert b2 == _BLOB


def test_add_attachment_restamps_the_owner(client: OlogClient) -> None:
    """OQ4: attaching a file REWRITES the entry's author. Measured, not read off the source.

    ``POST /logs/multipart`` delegates straight to the server's ``updateLog``, which calls
    ``persistedLog.setOwner(principal.getName())`` UNCONDITIONALLY, so a caller who only meant to
    add a file also takes over authorship of someone else's entry. The original author then exists
    only in the server-side archived version, which this server cannot read.

    A mock can never show this: it knows what the client SENDS, never what the server DOES. And the
    sibling additive test cannot show it either, because it creates and attaches as the same
    account. Hence the second principal: the entry is created as somebody else, so a re-stamp is the
    only thing that can change the owner.
    """
    assert_live_available(
        bool(_OTHER_USER and _OTHER_PASSWORD),
        "the owner re-stamp is only visible with a SECOND principal: set "
        "EPICS_MCP_OLOG_TEST_OTHER_USER + _OTHER_PASSWORD to an account that is not the write "
        "service account (a stock Olog ships 'user' and 'admin')",
        demanded=live_demanded(os.environ),
    )
    logbook = _LOGBOOKS.split(",")[0].strip()
    assert _URL is not None  # guarded by the module gate
    other = OlogClient(
        _URL,
        timeout=15.0,
        auth_header=basic_auth_header(_OTHER_USER or "", _OTHER_PASSWORD or ""),
    )
    created = other.create_log_entry(
        title="OQ4 owner re-stamp probe",
        logbooks=[logbook],
        description="written by the OTHER principal (synthetic test artifact)",
    )
    log_id = str(created["id"])
    author_before = str(created["owner"])
    assert author_before != os.environ["EPICS_MCP_OLOG_WRITE_USER"], (
        "the probe is vacuous unless the two principals actually differ"
    )

    # the write service account attaches ONE file and touches nothing else
    raw = other.get_raw_entry(log_id)
    assert raw is not None
    client.add_attachment(log_id, raw, [_upload(_BLOB, "oq4-owner.bob", None)])

    after = client.get_raw_entry(log_id)
    assert after is not None
    assert after["owner"] == os.environ["EPICS_MCP_OLOG_WRITE_USER"], (
        "the server did NOT re-stamp the owner, if this ever fails, the caveat in the guide, the "
        "README, the tool descriptions and the server instructions is wrong and must be revisited"
    )
    assert after["owner"] != author_before  # authorship really moved
    # ...while the CONTENT the attach never touched is still intact (the caveat is about owner ONLY)
    assert after["title"] == "OQ4 owner re-stamp probe"
    attachments = after["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 1
