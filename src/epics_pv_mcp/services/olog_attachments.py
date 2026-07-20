"""Attachment prep for the Olog upload/download surface (OA1) — pure, IO-bounded helpers.

Kept out of the transport client (:mod:`epics_pv_mcp.services.olog_client`) and the service
orchestrator (:mod:`epics_pv_mcp.services.checkers`) so the byte handling is unit-testable in
isolation, in three single-responsibility steps:

* :func:`plan_attachments` — resolve + SIZE the upload (``stat`` only, NO file read): the write gate
  refuses an over-limit request before any bytes are materialised (anti-DoS). Mints the client-side
  UUIDs and the id-prefixed unique filenames, and builds the inline-image markup.
* :func:`read_uploads` — materialise the planned specs into payloads, RE-CHECKING the size budget
  while reading (a file that grew between stat and read is refused; at most one byte over budget is
  ever read).
* :func:`write_download` — write downloaded bytes to a NEW, boundary-checked workspace file.

UUIDs are INJECTED (a factory) so the logic stays deterministic — a ``take_screenshot`` → attachment
workflow must be reproducible in a test (the project's determinism rule: no ``uuid`` in the logic).
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.paths import resolve_new_file_path, resolve_user_path
from epics_pv_mcp.services.olog_client import AttachmentUpload


class _Spec(NamedTuple):
    """One planned attachment: identity + size source, but NOT yet the bytes (read is deferred)."""

    id: str
    filename: str
    content_type: str | None
    path: Path | None  # a workspace file to read at upload time (None for an inline-base64 image)
    inline_bytes: bytes | None  # already-materialised bytes (a small base64 embed; None for a file)


class AttachmentPlan(NamedTuple):
    """The planned upload: specs (unread), the inline-image markup, and the total pre-read size."""

    specs: list[_Spec]
    inline_markup: str
    total_bytes: int


def plan_attachments(
    attachment_paths: list[str] | None,
    embed_image_base64: str | None,
    id_factory: Callable[[], str],
) -> AttachmentPlan:
    """Resolve + SIZE attachments WITHOUT reading file bytes — the anti-DoS half of an upload.

    Each *attachment_paths* entry is canonicalised + existence-checked through
    :func:`~epics_pv_mcp.paths.resolve_user_path` (kind ``file`` — it must exist) and sized by
    ``stat`` (not read), so an over-limit file is refused by the write gate before it is loaded. The
    filename is id-prefixed ``<uuid>_<basename>`` (exactly CS-Studio's convention) so it is unique
    per submission and a by-name download can never hit the server's duplicate-filename 404.

    *embed_image_base64* is a small inline image already in memory, so it IS decoded here; it is
    added
    as an ``image/png`` attachment and its ``![](attachment/<uuid>)`` markup (the CS-Studio inline
    convention, resolving to ``/Olog/attachment/<uuid>``) is returned to append to the description.
    """
    specs: list[_Spec] = []
    total = 0
    for raw in attachment_paths or []:
        path = resolve_user_path(raw, kind="file", label="attachments")
        total += path.stat().st_size
        uid = id_factory()
        specs.append(
            _Spec(
                id=uid,
                filename=f"{uid}_{path.name}",
                content_type=mimetypes.guess_type(path.name)[0],
                path=path,
                inline_bytes=None,
            )
        )
    inline_markup = ""
    if embed_image_base64:
        data = _decode_base64(embed_image_base64)
        total += len(data)
        uid = id_factory()
        specs.append(
            _Spec(
                id=uid,
                filename=f"{uid}.png",
                content_type="image/png",
                path=None,
                inline_bytes=data,
            )
        )
        inline_markup = f"\n\n![](attachment/{uid})"
    return AttachmentPlan(specs=specs, inline_markup=inline_markup, total_bytes=total)


def read_uploads(specs: list[_Spec], *, max_total_bytes: int) -> list[AttachmentUpload]:
    """Materialise planned specs into upload payloads, RE-CHECKING the size while reading.

    :func:`plan_attachments` sizes by ``stat`` and the write gate refuses an over-limit
    TOTAL before any read — but a file can grow (or be swapped) between stat and read
    (QA: TOCTOU), which used to materialise AND upload past the cap. Reading is therefore
    budgeted: at most one byte over the remaining budget is ever read, and exceeding it
    refuses with the gate's own error code (``OLOG_ATTACH_TOO_LARGE``). *max_total_bytes*
    is the same cap the gate enforced (``olog_attach_max_bytes``).
    """
    uploads: list[AttachmentUpload] = []
    remaining = max_total_bytes
    for spec in specs:
        if spec.inline_bytes is not None:
            content = spec.inline_bytes
        elif spec.path is not None:
            with spec.path.open("rb") as handle:
                content = handle.read(max(remaining, 0) + 1)
        else:  # unreachable: plan_attachments always sets exactly one of the two
            raise EpicsError(
                f"attachment {spec.filename!r} has neither a path nor inline bytes",
                error_code="INTERNAL",
            )
        if len(content) > remaining:
            raise EpicsError(
                f"Olog write refused: attachment {spec.filename!r} exceeds the remaining "
                f"size budget at READ time (limit {max_total_bytes} bytes total, "
                "EPICS_MCP_OLOG_ATTACH_MAX_BYTES) — the file changed between stat and read.",
                error_code="OLOG_ATTACH_TOO_LARGE",
            )
        remaining -= len(content)
        uploads.append(
            AttachmentUpload(
                id=spec.id,
                filename=spec.filename,
                content=content,
                content_type=spec.content_type,
            )
        )
    return uploads


def write_download(output_path: str, content: bytes, *, label: str = "output_path") -> str:
    """Write downloaded bytes to a NEW boundary-checked workspace file; return the resolved path.

    Uses :func:`~epics_pv_mcp.paths.resolve_new_file_path` so the ``EPICS_MCP_ALLOWED_ROOTS``
    boundary
    is enforced on a not-yet-existing target (which ``resolve_user_path(kind='file')`` would
    reject).
    Opens EXCLUSIVELY (``"xb"`` = ``O_CREAT | O_EXCL``): the NEW-file contract the name and
    docstring
    promise is enforced, not merely stated — an already-existing target raises ``FILE_EXISTS``
    (never a
    silent overwrite / data loss), and a pre-existing symlink at the target is refused rather than
    followed, so it cannot write OUTSIDE the validated parent (``resolve_new_file_path`` checks the
    parent, and ``O_EXCL`` closes the leaf-symlink escape). Downloaded bytes never destroy other
    data.
    """
    resolved = resolve_new_file_path(output_path, label=label)
    try:
        with open(resolved, "xb") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise EpicsError(
            f"{label} already exists: {output_path} (refusing to overwrite a download target)",
            error_code="FILE_EXISTS",
        ) from exc
    return str(resolved)


def _decode_base64(value: str) -> bytes:
    """Decode a base64 string to bytes, or raise a clear ``INVALID_INPUT`` (not a raw binascii)."""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EpicsError(
            "embed_image_base64 is not valid base64", error_code="INVALID_INPUT"
        ) from exc
