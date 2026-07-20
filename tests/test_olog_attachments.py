"""Offline tests for the Olog ATTACHMENT surface (OA1) — transport, prep, client, gate, service.

No network. Covers the two new transport helpers (multipart PUT + streaming byte GET), the pure
attachment-prep helpers (plan / read / write — anti-DoS stat-before-read, deterministic injected
UUIDs), the client upload/download paths + the download-privacy backstop, the write-gate size cap +
audit, and the service orchestration — including red-proofs for the four new guards:

* download bytes WITHHELD unless whole-mode AND the explicit opt-in flag,
* an over-size upload DENIED by the gate,
* a download output path outside EPICS_MCP_ALLOWED_ROOTS refused,
* attachment FILENAMES withheld in redacted mode.

All host/URL/person/file tokens are SYNTHETIC (facility-agnostic guard).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

import epics_pv_mcp.config as config_module
import epics_pv_mcp.olog_safety as olog_safety_module
import epics_pv_mcp.services.checkers as checkers_module
import epics_pv_mcp.services.olog_client as olog_client_module
from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsError, OlogWriteDeniedError
from epics_pv_mcp.olog_safety import OlogWriteGate
from epics_pv_mcp.services import _http
from epics_pv_mcp.services.checkers import (
    query_olog_add_attachment,
    query_olog_create,
    query_olog_download,
    query_olog_list_attachments,
)
from epics_pv_mcp.services.olog_attachments import plan_attachments, read_uploads, write_download
from epics_pv_mcp.services.olog_client import AttachmentUpload, OlogClient
from epics_pv_mcp.services.olog_exceptions import (
    OlogAttachmentDownloadDenied,
    OlogConnectionError,
    OlogResponseError,
    OlogRoundTripUnsafe,
    OlogWholeModeRequired,
)

_AUDIT_LOGGER = "epics_pv_mcp.olog_audit"
_LOOPBACK = "http://localhost:8080/Olog"
_REMOTE = "http://olog.example.org/Olog"


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset the config + Olog-write-gate singletons for each test (so each builds fresh)."""
    config_module._config = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    olog_safety_module._olog_safety = None


def _write_config(
    *,
    olog_url: str = _LOOPBACK,
    allow_olog_write: bool = True,
    olog_write_logbooks: str = "Ops",
    olog_write_rate_limit: int = 5,
    olog_attach_max_bytes: int = 52_428_800,
) -> EpicsConfig:
    """A config with Olog write enabled against the loopback sandbox (facility-agnostic tokens)."""
    return EpicsConfig(
        olog_url=olog_url,
        allow_olog_write=allow_olog_write,
        olog_write_logbooks=olog_write_logbooks,
        olog_write_rate_limit=olog_write_rate_limit,
        olog_attach_max_bytes=olog_attach_max_bytes,
        olog_write_user="epics-pv-logbook-svc",
        olog_write_password="pw",
    )


def _set_config(**overrides: object) -> None:
    """Install a fresh config singleton (paths helpers + client read from get_config())."""
    config_module._config = EpicsConfig(**overrides)  # type: ignore[arg-type]


def _ok_resp(payload: object) -> MagicMock:
    """A 2xx mock response: is_redirect explicitly False (a bare Mock is all-truthy)."""
    resp = MagicMock(is_redirect=False, status_code=200)
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ======================================================================================
# Transport: rest_put_multipart
# ======================================================================================


class TestMultipartTransport:
    def test_passes_files_and_never_sets_content_type(self) -> None:
        session = MagicMock()
        session.request.return_value = _ok_resp({"id": 1})
        files: _http.MultipartFiles = [
            ("logEntry", (None, "{}", "application/json")),
            ("files", ("uid_a.png", b"PNG", "image/png")),
        ]
        out = _http.rest_put_multipart(
            session,
            f"{_LOOPBACK}/logs/multipart",
            files,
            5.0,
            headers={"X-Olog-Client-Info": "epics-pv-mcp"},
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
        )
        assert out == {"id": 1}
        args, kwargs = session.request.call_args
        assert args[0] == "PUT"  # create rides the PUT verb
        assert kwargs["files"] == files  # a LIST of tuples, not a dict
        headers = kwargs.get("headers") or {}
        # requests sets Content-Type (with the boundary) from files=; we must never pass our own.
        assert not any(key.lower() == "content-type" for key in headers)

    def test_post_multipart_uses_the_post_verb(self) -> None:
        # OA1b: attach-to-existing rides POST /logs/multipart (the server's updateLog), the ONLY
        # transport difference from create — same body, same redirect refusal.
        session = MagicMock()
        session.request.return_value = _ok_resp({"id": 7})
        out = _http.rest_post_multipart(
            session,
            f"{_LOOPBACK}/logs/multipart",
            [("logEntry", (None, "{}", "application/json"))],
            5.0,
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
        )
        assert out == {"id": 7}
        args, _ = session.request.call_args
        assert args[0] == "POST"

    def test_refuses_redirect(self) -> None:
        session = MagicMock()
        session.request.return_value = MagicMock(is_redirect=True, status_code=302)
        with pytest.raises(OlogResponseError, match="redirect"):
            _http.rest_put_multipart(
                session,
                f"{_LOOPBACK}/logs/multipart",
                [("logEntry", (None, "{}", "application/json"))],
                5.0,
                conn_exc=OlogConnectionError,
                resp_exc=OlogResponseError,
            )


# ======================================================================================
# Transport: rest_get_bytes + Content-Disposition parsing
# ======================================================================================


class TestByteDownloadTransport:
    def test_returns_content_filename_and_type(self) -> None:
        session = MagicMock()
        resp = MagicMock(is_redirect=False)
        resp.content = b"PNGDATA"
        resp.headers = {
            "Content-Disposition": 'attachment; filename="plot.png"',
            "Content-Type": "image/png",
        }
        resp.raise_for_status.return_value = None
        session.get.return_value.__enter__.return_value = resp
        content, filename, content_type = _http.rest_get_bytes(
            session,
            f"{_LOOPBACK}/attachment/uid",
            5.0,
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
        )
        assert content == b"PNGDATA"
        assert filename == "plot.png"
        assert content_type == "image/png"

    def test_refuses_redirect(self) -> None:
        session = MagicMock()
        session.get.return_value.__enter__.return_value = MagicMock(
            is_redirect=True, status_code=302
        )
        with pytest.raises(OlogResponseError, match="redirect"):
            _http.rest_get_bytes(
                session,
                f"{_LOOPBACK}/attachment/uid",
                5.0,
                conn_exc=OlogConnectionError,
                resp_exc=OlogResponseError,
            )

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ('attachment; filename="a b.png"', "a b.png"),
            ("attachment; filename=plain.txt", "plain.txt"),
            (None, None),
            ("attachment", None),
        ],
    )
    def test_filename_parsing(self, header: str | None, expected: str | None) -> None:
        assert _http._filename_from_content_disposition(header) == expected

    def _resp_stream(self, chunks: list[bytes], headers: dict[str, str]) -> MagicMock:
        resp = MagicMock(is_redirect=False)
        resp.headers = headers
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = iter(chunks)
        return resp

    # --- RED-PROOF (review fix B): an over-cap body is refused before/while reading ---
    def test_refuses_oversize_by_content_length(self) -> None:
        session = MagicMock()
        session.get.return_value.__enter__.return_value = self._resp_stream(
            [b"x"], {"Content-Length": "999999"}
        )
        with pytest.raises(OlogResponseError, match="size cap"):
            _http.rest_get_bytes(
                session,
                f"{_LOOPBACK}/attachment/uid",
                5.0,
                max_bytes=100,
                conn_exc=OlogConnectionError,
                resp_exc=OlogResponseError,
            )

    def test_refuses_oversize_by_stream_when_length_missing(self) -> None:
        # no Content-Length (or a lying one): the streamed accumulation must still refuse over-cap
        session = MagicMock()
        session.get.return_value.__enter__.return_value = self._resp_stream(
            [b"a" * 80, b"b" * 80], {}
        )
        with pytest.raises(OlogResponseError, match="size cap"):
            _http.rest_get_bytes(
                session,
                f"{_LOOPBACK}/attachment/uid",
                5.0,
                max_bytes=100,
                conn_exc=OlogConnectionError,
                resp_exc=OlogResponseError,
            )

    def test_within_cap_streams_the_body(self) -> None:
        session = MagicMock()
        session.get.return_value.__enter__.return_value = self._resp_stream(
            [b"PNG", b"XYZ"], {"Content-Length": "6", "Content-Type": "image/png"}
        )
        content, _fn, ctype = _http.rest_get_bytes(
            session,
            f"{_LOOPBACK}/attachment/uid",
            5.0,
            max_bytes=100,
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
        )
        assert content == b"PNGXYZ"
        assert ctype == "image/png"


# ======================================================================================
# Attachment prep: plan_attachments / read_uploads / write_download
# ======================================================================================


class TestAttachmentPrep:
    def test_plan_sizes_and_prefixes_a_file(self, tmp_path: Path) -> None:
        _set_config()
        f = tmp_path / "plot.png"
        f.write_bytes(b"PNGDATA")
        ids = iter(["uid1", "uid2"])
        plan = plan_attachments([str(f)], None, lambda: next(ids))
        assert plan.total_bytes == 7  # stat sum
        spec = plan.specs[0]
        assert spec.id == "uid1"
        assert spec.filename == "uid1_plot.png"  # id-prefix (CS-Studio convention)
        assert spec.content_type == "image/png"
        assert spec.inline_bytes is None
        # read is deferred until read_uploads
        uploads = read_uploads(plan.specs, max_total_bytes=1024)
        assert uploads[0]["content"] == b"PNGDATA"
        assert uploads[0]["filename"] == "uid1_plot.png"

    def test_plan_embeds_a_base64_image(self, tmp_path: Path) -> None:
        _set_config()
        data = b"IMGBYTES"
        plan = plan_attachments(None, base64.b64encode(data).decode(), lambda: "uidX")
        assert plan.total_bytes == len(data)
        assert plan.inline_markup == "\n\n![](attachment/uidX)"
        spec = plan.specs[0]
        assert spec.content_type == "image/png"
        assert spec.inline_bytes == data
        assert spec.filename == "uidX.png"
        assert read_uploads(plan.specs, max_total_bytes=1024)[0]["content"] == data

    def test_read_uploads_refuses_a_file_grown_past_the_cap(self, tmp_path: Path) -> None:
        """QA (TOCTOU): plan_attachments sizes by ``stat``, the gate checks that sum —
        but a file can grow (or be swapped) between stat and read. read_uploads used to
        ``read_bytes()`` unconditionally, materialising AND uploading past the cap; the
        thrice-documented "an over-limit file is never loaded" promise hung on filesystem
        timing. It now re-checks while reading (at most one byte over budget is ever
        read) and refuses with the gate's own error code."""
        _set_config()
        f = tmp_path / "grow.bin"
        f.write_bytes(b"x" * 10)
        plan = plan_attachments([str(f)], None, lambda: "uidG")  # stat: 10 bytes
        f.write_bytes(b"x" * 100)  # grows past the budget AFTER the stat/gate
        with pytest.raises(EpicsError) as excinfo:
            read_uploads(plan.specs, max_total_bytes=50)
        assert excinfo.value.error_code == "OLOG_ATTACH_TOO_LARGE"

    def test_read_uploads_budget_is_cumulative(self, tmp_path: Path) -> None:
        """Two files that each fit but together exceed the budget are refused — the cap
        is the TOTAL upload, mirroring the gate's ``plan.total_bytes`` semantics."""
        _set_config()
        a, b = tmp_path / "a.bin", tmp_path / "b.bin"
        a.write_bytes(b"x" * 30)
        b.write_bytes(b"x" * 30)
        ids = iter(["u1", "u2"])
        plan = plan_attachments([str(a), str(b)], None, lambda: next(ids))
        with pytest.raises(EpicsError) as excinfo:
            read_uploads(plan.specs, max_total_bytes=50)
        assert excinfo.value.error_code == "OLOG_ATTACH_TOO_LARGE"

    def test_plan_rejects_bad_base64(self) -> None:
        _set_config()
        with pytest.raises(EpicsError) as excinfo:
            plan_attachments(None, "!!!not-base64!!!", lambda: "x")
        assert excinfo.value.error_code == "INVALID_INPUT"

    def test_plan_rejects_missing_file(self, tmp_path: Path) -> None:
        _set_config()
        with pytest.raises(EpicsError) as excinfo:
            plan_attachments([str(tmp_path / "nope.bob")], None, lambda: "x")
        assert excinfo.value.error_code == "INVALID_INPUT"

    def test_write_download_writes_and_returns_path(self, tmp_path: Path) -> None:
        _set_config()
        target = tmp_path / "out.bin"
        written = write_download(str(target), b"DATA")
        assert Path(written).read_bytes() == b"DATA"

    # --- RED-PROOF (guard 3): a download target outside ALLOWED_ROOTS is refused ---
    def test_write_download_rejects_outside_allowed_roots(self, tmp_path: Path) -> None:
        inside = tmp_path / "inside"
        inside.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _set_config(allowed_roots=str(inside))
        with pytest.raises(EpicsError) as excinfo:
            write_download(str(outside / "f.bin"), b"x")
        assert excinfo.value.error_code == "PATH_OUTSIDE_WORKSPACE"
        # inside the boundary is allowed (the positive control)
        written = write_download(str(inside / "f.bin"), b"y")
        assert Path(written).read_bytes() == b"y"

    # --- RED-PROOF (review fix A): write_download refuses to OVERWRITE an existing target ---
    def test_write_download_refuses_existing_target(self, tmp_path: Path) -> None:
        _set_config()
        target = tmp_path / "exists.bin"
        target.write_bytes(b"original")
        with pytest.raises(EpicsError) as excinfo:
            write_download(str(target), b"new-content")
        assert excinfo.value.error_code == "FILE_EXISTS"
        assert target.read_bytes() == b"original"  # NOT overwritten (no silent data loss)

    # --- RED-PROOF (review fix A): a pre-existing symlink target is refused, not followed out ---
    def test_write_download_refuses_symlink_target(self, tmp_path: Path) -> None:
        _set_config()
        outside = tmp_path / "outside.bin"
        link = tmp_path / "link.bin"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this host")
        with pytest.raises(EpicsError) as excinfo:
            write_download(str(link), b"data")
        assert excinfo.value.error_code == "INVALID_INPUT"  # is_symlink rejects it (cross-platform)
        assert not outside.exists()  # the symlink was not followed out of the parent


# ======================================================================================
# Client: upload (multipart building) + no-attachment path unchanged
# ======================================================================================


class TestClientUpload:
    def test_create_with_attachments_builds_multipart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_multipart(
            session: object,
            url: str,
            files: _http.MultipartFiles,
            timeout: float,
            *,
            params: dict[str, str] | None = None,
            headers: dict[str, str] | None = None,
            conn_exc: type[Exception],
            resp_exc: type[Exception],
            allow_redirects: bool = False,
        ) -> object:
            captured.update(url=url, files=files, params=params)
            return {"id": 42, "title": "t", "logbooks": ["Ops"]}

        monkeypatch.setattr(olog_client_module, "rest_put_multipart", fake_multipart)
        client = OlogClient(_LOOPBACK, assume_test_data=True)
        upload = AttachmentUpload(
            id="uid1", filename="uid1_plot.png", content=b"PNG", content_type="image/png"
        )
        non_image = AttachmentUpload(
            id="uid2", filename="uid2_data.bob", content=b"<display/>", content_type=None
        )
        client.create_log_entry(title="t", logbooks=["Ops"], attachments=[upload, non_image])

        assert captured["url"].endswith("/logs/multipart")
        assert captured["params"]["markup"] == "commonmark"
        files = captured["files"]
        # part 0 is the logEntry JSON with the attachments array (id/filename/metadata)
        name0, (fn0, body0, ct0) = files[0]
        assert name0 == "logEntry" and fn0 is None and ct0 == "application/json"
        log_json = json.loads(body0)
        assert log_json["attachments"] == [
            {"id": "uid1", "filename": "uid1_plot.png", "fileMetadataDescription": "image"},
            {"id": "uid2", "filename": "uid2_data.bob", "fileMetadataDescription": "file"},
        ]
        # one files part per attachment, filename == uniqueFilename, octet-stream fallback
        assert files[1] == ("files", ("uid1_plot.png", b"PNG", "image/png"))
        assert files[2] == ("files", ("uid2_data.bob", b"<display/>", "application/octet-stream"))

    def test_no_attachments_uses_json_path_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, str] = {}

        def fake_json(session: object, url: str, *a: object, **k: object) -> object:
            seen["url"] = url
            return {"id": 5, "title": "t", "logbooks": ["Ops"]}

        def boom_multipart(*a: object, **k: object) -> object:
            raise AssertionError("multipart must not be used without attachments")

        monkeypatch.setattr(olog_client_module, "rest_put_json", fake_json)
        monkeypatch.setattr(olog_client_module, "rest_put_multipart", boom_multipart)
        client = OlogClient(_LOOPBACK, assume_test_data=True)
        client.create_log_entry(title="t", logbooks=["Ops"])
        assert seen["url"].endswith("/logs")  # the plain JSON endpoint, not /logs/multipart


# ======================================================================================
# Client: download posture (attachment_bytes_allowed / whole_mode) + URL encoding + backstop
# ======================================================================================


class TestClientDownload:
    def test_posture_flags(self) -> None:
        # whole-mode requires loopback + assume_test_data; bytes additionally require the flag.
        redacted = OlogClient(_REMOTE, assume_test_data=True, allow_attachment_download=True)
        assert redacted.whole_mode is False
        assert redacted.attachment_bytes_allowed is False  # not loopback → redacted
        whole_no_flag = OlogClient(
            _LOOPBACK, assume_test_data=True, allow_attachment_download=False
        )
        assert whole_no_flag.whole_mode is True
        assert whole_no_flag.attachment_bytes_allowed is False  # flag off
        allowed = OlogClient(_LOOPBACK, assume_test_data=True, allow_attachment_download=True)
        assert allowed.attachment_bytes_allowed is True

    def test_by_name_url_is_percent_encoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        def fake_get_bytes(
            session: object, url: str, timeout: float, **k: object
        ) -> tuple[bytes, str | None, str | None]:
            captured["url"] = url
            return (b"DATA", "plot.png", "image/png")

        monkeypatch.setattr(olog_client_module, "rest_get_bytes", fake_get_bytes)
        client = OlogClient(_LOOPBACK, assume_test_data=True, allow_attachment_download=True)
        content, _fn, _ct = client.get_attachment("12", "my plot.png")
        assert content == b"DATA"
        # space → %20 (a single path segment), matching CS-Studio's URLEncoder + '+'→'%20'
        assert captured["url"] == f"{_LOOPBACK}/logs/attachments/12/my%20plot.png"

    def test_by_id_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        def fake_get_bytes(
            session: object, url: str, timeout: float, **k: object
        ) -> tuple[bytes, str | None, str | None]:
            captured["url"] = url
            return (b"X", None, None)

        monkeypatch.setattr(olog_client_module, "rest_get_bytes", fake_get_bytes)
        client = OlogClient(_LOOPBACK, assume_test_data=True, allow_attachment_download=True)
        client.get_attachment_by_id("abc-123")
        assert captured["url"] == f"{_LOOPBACK}/attachment/abc-123"

    # --- RED-PROOF (guard 1): the download backstop raises when bytes may not leave ---
    def test_backstop_raises_when_redacted(self) -> None:
        client = OlogClient(_REMOTE, allow_attachment_download=True)  # not loopback → redacted
        with pytest.raises(OlogAttachmentDownloadDenied):
            client.get_attachment("1", "a.png")
        with pytest.raises(OlogAttachmentDownloadDenied):
            client.get_attachment_by_id("x")

    def test_backstop_raises_when_flag_off(self) -> None:
        client = OlogClient(_LOOPBACK, assume_test_data=True, allow_attachment_download=False)
        with pytest.raises(OlogAttachmentDownloadDenied):
            client.get_attachment("1", "a.png")


# ======================================================================================
# Write gate: attachment size cap (RED-PROOF guard 2) + audit metadata
# ======================================================================================


class TestGateSizeCap:
    def test_denies_oversize_upload(self) -> None:
        gate = OlogWriteGate(_write_config(olog_attach_max_bytes=100))
        with pytest.raises(OlogWriteDeniedError, match="exceeds"):
            gate.check_write_allowed(["Ops"], attachment_bytes=101)

    def test_allows_upload_within_cap(self) -> None:
        gate = OlogWriteGate(_write_config(olog_attach_max_bytes=100))
        gate.check_write_allowed(["Ops"], attachment_bytes=100)  # exactly at the cap → allowed

    def test_oversize_denial_is_audited_and_burns_no_rate_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate = OlogWriteGate(_write_config(olog_attach_max_bytes=10, olog_write_rate_limit=1))
        with (
            caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER),
            pytest.raises(OlogWriteDeniedError),
        ):
            gate.check_write_allowed(["Ops"], attachment_bytes=11)
        assert "OLOG_ATTACH_TOO_LARGE" in caplog.text
        # the size denial ran BEFORE the rate limit → the one token is still free
        gate.check_write_allowed(["Ops"], attachment_bytes=0)

    def test_audit_line_carries_counts_never_filenames(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate = OlogWriteGate(_write_config())
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            gate.audit_write(
                entry_id="7",
                logbooks=["Ops"],
                level=None,
                title_len=5,
                owner="epics-pv-logbook-svc",
                attachment_count=2,
                attachment_bytes=2048,
            )
        assert "attachments=2 attach_bytes=2048" in caplog.text

    def test_audit_line_omits_attachment_fields_for_plain_write(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate = OlogWriteGate(_write_config())
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            gate.audit_write(entry_id="7", logbooks=["Ops"], level=None, title_len=5, owner="svc")
        assert "attachments=" not in caplog.text  # byte-identical to the pre-OA1 audit line


# ======================================================================================
# Service: create-with-attachments (gate size + audit + attachments_uploaded withholding)
# ======================================================================================


class _CaptureClient:
    """A fake OlogClient recording the create call and echoing a create response.

    ``whole_mode`` is set by the caller so the filename-withholding red-proof can flip it.
    """

    whole: ClassVar[bool] = True
    calls: ClassVar[dict[str, Any]] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    @property
    def whole_mode(self) -> bool:
        return _CaptureClient.whole

    def create_log_entry(self, **kwargs: object) -> dict[str, object]:
        _CaptureClient.calls = dict(kwargs)
        return {"id": 99, "title": "withheld", "logbooks": ["Ops"]}


class TestServiceCreate:
    @pytest.mark.asyncio
    async def test_attachment_bytes_reach_the_gate_and_audit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_module._config = _write_config()
        _CaptureClient.whole = True
        monkeypatch.setattr(checkers_module, "OlogClient", _CaptureClient)
        f = tmp_path / "plot.png"
        f.write_bytes(b"PNGDATA")  # 7 bytes
        ids = iter(["uid1"])
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            result = await query_olog_create(
                title="t",
                logbooks=["Ops"],
                attachments=[str(f)],
                id_factory=lambda: next(ids),
            )
        assert result["created"] is True
        # the audit records the count + byte total (metadata only), no filename
        assert "attachments=1 attach_bytes=7" in caplog.text
        assert "plot.png" not in caplog.text
        # the client received the built upload list
        uploads = _CaptureClient.calls["attachments"]
        assert uploads[0]["filename"] == "uid1_plot.png"

    @pytest.mark.asyncio
    async def test_oversize_upload_denied_before_client(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_module._config = _write_config(olog_attach_max_bytes=3)
        monkeypatch.setattr(checkers_module, "OlogClient", _CaptureClient)
        _CaptureClient.calls = {}
        f = tmp_path / "big.bin"
        f.write_bytes(b"0123456789")  # 10 > 3
        with pytest.raises(OlogWriteDeniedError, match="exceeds"):
            await query_olog_create(
                title="t", logbooks=["Ops"], attachments=[str(f)], id_factory=lambda: "uid"
            )
        assert _CaptureClient.calls == {}  # denied before the client was even called

    # --- RED-PROOF (review fix C): the cheap gate denial fires BEFORE any attachment stat ---
    @pytest.mark.asyncio
    async def test_gate_denied_before_touching_the_filesystem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Write disabled → the precondition check denies FIRST. If plan_attachments (stat) ran
        # before the gate, this NON-EXISTENT path would raise EpicsError(INVALID_INPUT) instead — so
        # a clean OlogWriteDeniedError proves the "deny before any I/O" ordering.
        config_module._config = _write_config(allow_olog_write=False)
        monkeypatch.setattr(checkers_module, "OlogClient", _CaptureClient)
        missing = tmp_path / "never_stat_me.bin"  # deliberately does not exist
        with pytest.raises(OlogWriteDeniedError):
            await query_olog_create(
                title="t", logbooks=["Ops"], attachments=[str(missing)], id_factory=lambda: "uid"
            )

    @pytest.mark.asyncio
    async def test_embed_appends_inline_markup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_module._config = _write_config()
        _CaptureClient.whole = True
        monkeypatch.setattr(checkers_module, "OlogClient", _CaptureClient)
        await query_olog_create(
            title="t",
            logbooks=["Ops"],
            description="see below",
            embed_image_base64=base64.b64encode(b"IMG").decode(),
            id_factory=lambda: "uidZ",
        )
        assert _CaptureClient.calls["description"] == "see below\n\n![](attachment/uidZ)"

    # --- RED-PROOF (guard 4): filenames withheld in redacted mode, ids always ---
    @pytest.mark.asyncio
    async def test_uploaded_filenames_withheld_when_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_module._config = _write_config()
        monkeypatch.setattr(checkers_module, "OlogClient", _CaptureClient)
        f = tmp_path / "secret_name.bob"
        f.write_bytes(b"x")

        _CaptureClient.whole = False  # redacted
        redacted = await query_olog_create(
            title="t", logbooks=["Ops"], attachments=[str(f)], id_factory=lambda: "uid1"
        )
        uploaded_redacted = redacted["attachments_uploaded"]
        assert uploaded_redacted == [{"id": "uid1"}]  # NO filename leaks

        _CaptureClient.whole = True  # whole-mode: filename surfaced (positive control)
        whole = await query_olog_create(
            title="t", logbooks=["Ops"], attachments=[str(f)], id_factory=lambda: "uid2"
        )
        assert whole["attachments_uploaded"] == [{"id": "uid2", "filename": "uid2_secret_name.bob"}]


# ======================================================================================
# Service: download (disabled / withheld red-proof / written / base64) + list
# ======================================================================================


def _download_client(
    *, allowed: bool, result: tuple[bytes, str | None, str | None] = (b"DATA", "a.png", "image/png")
) -> Callable[..., object]:
    """A fake-OlogClient factory for query_olog_download: posture + the bytes it would return."""

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        @property
        def attachment_bytes_allowed(self) -> bool:
            return allowed

        def get_attachment(
            self, log_id: str, filename: str, *, max_bytes: int | None = None
        ) -> tuple[bytes, str | None, str | None]:
            return result

        def get_attachment_by_id(
            self, attachment_id: str, *, max_bytes: int | None = None
        ) -> tuple[bytes, str | None, str | None]:
            return result

    return _Fake


class TestServiceDownload:
    @pytest.mark.asyncio
    async def test_disabled_without_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(olog_url="")
        result = await query_olog_download(log_id="1", filename="a.png", as_base64=True)
        assert result["enabled"] is False
        assert result["downloaded"] is False

    @pytest.mark.asyncio
    async def test_requires_an_identity(self) -> None:
        _set_config(olog_url=_LOOPBACK)
        with pytest.raises(EpicsError):
            await query_olog_download(as_base64=True)

    # --- RED-PROOF (guard 1): bytes withheld, and NO byte fetch, when posture forbids ---
    @pytest.mark.asyncio
    async def test_withheld_when_posture_forbids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(olog_url=_LOOPBACK)
        monkeypatch.setattr(checkers_module, "OlogClient", _download_client(allowed=False))
        result = await query_olog_download(log_id="1", filename="a.png", as_base64=True)
        assert result["downloaded"] is False
        assert result["withheld"] is True
        assert "content_base64" not in result  # nothing fetched

    @pytest.mark.asyncio
    async def test_writes_to_output_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _set_config(olog_url=_LOOPBACK)
        monkeypatch.setattr(
            checkers_module,
            "OlogClient",
            _download_client(allowed=True, result=(b"BYTES", "a.png", None)),
        )
        target = tmp_path / "dl.bin"
        result = await query_olog_download(log_id="1", filename="a.png", output_path=str(target))
        assert result["downloaded"] is True
        assert result["size_bytes"] == 5
        assert target.read_bytes() == b"BYTES"

    @pytest.mark.asyncio
    async def test_returns_base64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(olog_url=_LOOPBACK)
        monkeypatch.setattr(
            checkers_module,
            "OlogClient",
            _download_client(allowed=True, result=(b"XY", "a.png", None)),
        )
        result = await query_olog_download(attachment_id="abc", as_base64=True)
        assert result["content_base64"] == base64.b64encode(b"XY").decode()

    # --- RED-PROOF (OA1-QA #A1): output_path AND as_base64 both set is a contradiction and refused
    # up front, so as_base64 can never silently drop an explicit output_path (nor apply its smaller
    # cap to a would-be file handover). ---
    @pytest.mark.asyncio
    async def test_refuses_both_output_path_and_base64(self, tmp_path: Path) -> None:
        _set_config(olog_url=_LOOPBACK)
        with pytest.raises(EpicsError) as exc:
            await query_olog_download(
                log_id="1", filename="a.png", output_path=str(tmp_path / "x.bin"), as_base64=True
            )
        assert exc.value.error_code == "INVALID_INPUT"


def _list_client(entry: dict[str, object] | None) -> Callable[..., object]:
    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_log_entry(self, log_id: str) -> dict[str, object] | None:
            return entry

    return _Fake


class TestServiceList:
    @pytest.mark.asyncio
    async def test_whole_mode_surfaces_filenames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(olog_url=_LOOPBACK)
        entry = {
            "id": 12,
            "attachments": [
                {"id": "uid1", "filename": "uid1_plot.png", "fileMetadataDescription": "image"}
            ],
        }
        monkeypatch.setattr(checkers_module, "OlogClient", _list_client(entry))
        result = await query_olog_list_attachments("12")
        assert result["found"] is True
        assert result["attachments"] == [
            {"id": "uid1", "filename": "uid1_plot.png", "fileMetadataDescription": "image"}
        ]

    @pytest.mark.asyncio
    async def test_redacted_withholds_filenames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(olog_url=_LOOPBACK)
        # a redacted entry has no raw `attachments` list, only the synthesised count
        entry: dict[str, object] = {"id": 12, "attachment_count": 3}
        monkeypatch.setattr(checkers_module, "OlogClient", _list_client(entry))
        result = await query_olog_list_attachments("12")
        assert result["withheld"] is True
        assert result["attachments"] == []
        assert result["attachment_count"] == 3


# ======================================================================================
# OA1b — add_log_attachment: client round-trip + whole-mode + service gating
# ======================================================================================

# A representative RAW whole-mode entry (as measured from the live sandbox: source + properties
# present, logbooks a list-of-structs, attachments carry id/filename/metadata/checksum).
_RAW_ENTRY: dict[str, object] = {
    "id": 17,
    "title": "existing title",
    "description": "rendered body",
    "source": "raw **body**",
    "level": "Info",
    "state": "Active",
    "logbooks": [{"name": "Ops", "owner": None, "state": "Active"}],
    "tags": [{"name": "shift"}],
    "properties": [],
    "attachments": [
        {
            "id": "old1",
            "filename": "old1_a.png",
            "fileMetadataDescription": "image",
            "checksum": None,
        }
    ],
}


class TestClientAddAttachment:
    def test_round_trips_existing_and_appends_new(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard c): the destructive POST /logs/multipart prunes any attachment not
        # resubmitted, so the round-trip MUST re-list the existing attachment AND carry every
        # overwrite-field. A mutant that sent only the new attachment (or dropped title/logbooks)
        # would drop the existing id / fields here → red.
        captured: dict[str, Any] = {}

        def fake_post(
            session: object,
            url: str,
            files: _http.MultipartFiles,
            timeout: float,
            *,
            params: dict[str, str] | None = None,
            headers: dict[str, str] | None = None,
            conn_exc: type[Exception],
            resp_exc: type[Exception],
            allow_redirects: bool = False,
        ) -> object:
            captured.update(url=url, files=files, params=params)
            return {"id": 17, "title": "withheld", "logbooks": ["Ops"]}

        monkeypatch.setattr(olog_client_module, "rest_post_multipart", fake_post)
        client = OlogClient(_LOOPBACK, assume_test_data=True)
        new = AttachmentUpload(
            id="new1", filename="new1_x.bob", content=b"<display/>", content_type=None
        )
        client.add_attachment("17", _RAW_ENTRY, [new])

        assert captured["url"].endswith("/logs/multipart")
        assert captured["params"]["markup"] == "commonmark"
        name0, (fn0, body0, ct0) = captured["files"][0]
        assert name0 == "logEntry" and fn0 is None and ct0 == "application/json"
        log_json = json.loads(body0)
        assert log_json["id"] == 17  # numeric, LogResource:577
        # every overwrite-field is round-tripped verbatim (else updateLog wipes it)
        assert log_json["title"] == "existing title"
        assert log_json["source"] == "raw **body**"
        assert log_json["level"] == "Info"
        assert log_json["logbooks"] == [{"name": "Ops", "owner": None, "state": "Active"}]
        assert log_json["tags"] == [{"name": "shift"}]
        # attachments = existing (checksum dropped) + new — the anti-retainAll list
        assert log_json["attachments"] == [
            {"id": "old1", "filename": "old1_a.png", "fileMetadataDescription": "image"},
            {"id": "new1", "filename": "new1_x.bob", "fileMetadataDescription": "file"},
        ]
        # one files part for the NEW attachment only (existing bytes already stored server-side)
        assert captured["files"][1] == (
            "files",
            ("new1_x.bob", b"<display/>", "application/octet-stream"),
        )
        assert len(captured["files"]) == 2

    def test_embed_appends_to_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_post(
            session: object, url: str, files: _http.MultipartFiles, *a: object, **k: object
        ) -> object:
            captured["files"] = files
            return {"id": 17, "logbooks": ["Ops"]}

        monkeypatch.setattr(olog_client_module, "rest_post_multipart", fake_post)
        client = OlogClient(_LOOPBACK, assume_test_data=True)
        new = AttachmentUpload(
            id="img1", filename="img1.png", content=b"IMG", content_type="image/png"
        )
        client.add_attachment("17", _RAW_ENTRY, [new], inline_markup="\n\n![](attachment/img1)")
        log_json = json.loads(captured["files"][0][1][1])
        assert log_json["source"] == "raw **body**\n\n![](attachment/img1)"

    def test_refuses_unroundtrippable_attachments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF: attaching also round-trips the EXISTING attachment list, and retention is
        # filename-keyed — so an entry whose current attachments collide case-insensitively cannot
        # be attached to without the server silently dropping one of them. Refuse instead.
        captured: dict[str, Any] = {}

        def fake_post(
            session: object, url: str, files: _http.MultipartFiles, *a: object, **k: object
        ) -> object:
            captured["files"] = files
            return {"id": 17, "logbooks": ["Ops"]}

        monkeypatch.setattr(olog_client_module, "rest_post_multipart", fake_post)
        client = OlogClient(_LOOPBACK, assume_test_data=True)
        entry = dict(_RAW_ENTRY)
        entry["attachments"] = [
            {"id": "1", "filename": "plot.png"},
            {"id": "2", "filename": "PLOT.PNG"},
        ]
        new = AttachmentUpload(
            id="n1", filename="n1_x.bob", content=b"<display/>", content_type=None
        )
        with pytest.raises(OlogRoundTripUnsafe, match="filename"):
            client.add_attachment("17", entry, [new])
        assert captured == {}  # nothing was written

    def test_get_raw_entry_refuses_when_not_whole_mode(self) -> None:
        # RED-PROOF (guard a, client backstop): a redacted client (loopback but no assume_test_data)
        # must refuse to read the round-trip source — no redacted entry is ever round-tripped.
        client = OlogClient(_LOOPBACK, assume_test_data=False)
        assert client.whole_mode is False
        with pytest.raises(OlogWholeModeRequired):
            client.get_raw_entry("17")


class _AddCaptureClient:
    """A fake OlogClient for add_log_attachment service tests: flippable whole_mode, a canned raw
    entry, and a recording add_attachment."""

    whole: ClassVar[bool] = True
    raw: ClassVar[dict[str, object] | None] = None
    calls: ClassVar[dict[str, Any]] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    @property
    def whole_mode(self) -> bool:
        return _AddCaptureClient.whole

    def get_raw_entry(self, log_id: str) -> dict[str, object] | None:
        return _AddCaptureClient.raw

    def add_attachment(
        self,
        log_id: str,
        raw_entry: dict[str, object],
        uploads: list[AttachmentUpload],
        inline_markup: str = "",
    ) -> dict[str, object]:
        _AddCaptureClient.calls = {"log_id": log_id, "uploads": uploads, "inline": inline_markup}
        return {"id": int(log_id), "title": "withheld", "logbooks": ["Ops"]}


class TestServiceAddAttachment:
    @pytest.mark.asyncio
    async def test_disabled_without_url(self) -> None:
        _set_config(olog_url="")
        result = await query_olog_add_attachment("17", attachments=["x"])
        assert result["enabled"] is False
        assert result["added"] is False

    @pytest.mark.asyncio
    async def test_needs_at_least_one_attachment(self) -> None:
        config_module._config = _write_config()
        with pytest.raises(EpicsError) as exc:
            await query_olog_add_attachment("17")
        assert exc.value.error_code == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_numeric_id_required(self) -> None:
        # RED-PROOF (guard b): a non-numeric id is refused BEFORE any network/filesystem (the server
        # rejects a null/absent/negative id; we fail fast with a clear message).
        config_module._config = _write_config()
        with pytest.raises(EpicsError) as exc:
            await query_olog_add_attachment("not-a-number", attachments=["/x"])
        assert exc.value.error_code == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_refuses_when_not_whole_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard a, service): a redacted server is refused up front — no read, no write.
        config_module._config = _write_config()
        _AddCaptureClient.whole = False
        _AddCaptureClient.calls = {}
        monkeypatch.setattr(checkers_module, "OlogClient", _AddCaptureClient)
        with pytest.raises(OlogWriteDeniedError, match="sandbox"):
            await query_olog_add_attachment("17", attachments=["/x"])
        assert _AddCaptureClient.calls == {}  # never reached add_attachment

    @pytest.mark.asyncio
    async def test_gate_deny_when_target_logbook_not_allowlisted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # RED-PROOF (guard d): the gate is keyed on the TARGET entry's OWN logbooks; a target in a
        # logbook outside EPICS_MCP_OLOG_WRITE_LOGBOOKS is denied.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _AddCaptureClient.whole = True
        _AddCaptureClient.raw = {"id": 17, "logbooks": [{"name": "SecretBook"}], "title": "x"}
        _AddCaptureClient.calls = {}
        monkeypatch.setattr(checkers_module, "OlogClient", _AddCaptureClient)
        f = tmp_path / "a.bob"
        f.write_bytes(b"<display/>")
        with pytest.raises(OlogWriteDeniedError, match="allowlist"):
            await query_olog_add_attachment("17", attachments=[str(f)], id_factory=lambda: "uid")
        assert _AddCaptureClient.calls == {}  # denied before add_attachment

    @pytest.mark.asyncio
    async def test_happy_path_rounds_trip_and_audits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _AddCaptureClient.whole = True
        _AddCaptureClient.raw = {"id": 17, "logbooks": [{"name": "Ops"}], "title": "existing"}
        _AddCaptureClient.calls = {}
        monkeypatch.setattr(checkers_module, "OlogClient", _AddCaptureClient)
        f = tmp_path / "plot.png"
        f.write_bytes(b"PNGDATA")  # 7 bytes
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            result = await query_olog_add_attachment(
                "17", attachments=[str(f)], id_factory=lambda: "uidA"
            )
        assert result["added"] is True
        assert result["attachments_uploaded"] == [{"id": "uidA", "filename": "uidA_plot.png"}]
        # the raw entry reached add_attachment (round-trip source)
        assert _AddCaptureClient.calls["uploads"][0]["filename"] == "uidA_plot.png"
        # audit is metadata-only (count + bytes, never a filename)
        assert "attachments=1 attach_bytes=7" in caplog.text
        assert "plot.png" not in caplog.text
        assert "caller=add_log_attachment" in caplog.text

    @pytest.mark.asyncio
    async def test_failed_attach_is_audited_and_names_the_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        # This branch had NO test at all: only the ALLOW path was covered, so a broken FAILED-audit
        # call could not go red here (mypy caught one that pytest did not). Same reasoning as the
        # update path — POST /logs/multipart IS the destructive updateLog, so a timeout can leave
        # the entry mutated while the caller sees FAILED; the record must name it.
        _set_config(olog_url=_LOOPBACK, allow_olog_write=True, olog_write_logbooks="Ops")
        _AddCaptureClient.whole = True
        _AddCaptureClient.raw = {"id": 17, "logbooks": [{"name": "Ops"}], "title": "existing"}
        _AddCaptureClient.calls = {}

        def boom(*args: object, **kwargs: object) -> dict[str, object]:
            raise OlogResponseError("Olog timed out (HTTP 504)")

        monkeypatch.setattr(checkers_module, "OlogClient", _AddCaptureClient)
        monkeypatch.setattr(_AddCaptureClient, "add_attachment", boom)
        f = tmp_path / "plot.png"
        f.write_bytes(b"PNGDATA")
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER), pytest.raises(EpicsError):
            await query_olog_add_attachment("17", attachments=[str(f)], id_factory=lambda: "uidA")
        assert "event=FAILED" in caplog.text
        assert "entry_id=17" in caplog.text
        assert "caller=add_log_attachment" in caplog.text
        assert "plot.png" not in caplog.text  # SEC-5: still metadata-only
        assert "owner=" not in caplog.text

    @pytest.mark.asyncio
    async def test_missing_entry_is_found_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(olog_url=_LOOPBACK)
        monkeypatch.setattr(checkers_module, "OlogClient", _list_client(None))
        result = await query_olog_list_attachments("404")
        assert result["found"] is False
