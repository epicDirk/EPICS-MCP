"""MCP adapters for the Phoebus Olog logbook (reads + one gated write; output posture is
redacted against a real server, whole against a declared local test sandbox).

Thin wrappers: the config-gated, off-loop queries live in the services layer
(:func:`epics_pv_mcp.services.checkers.query_olog_search` / ``query_olog_entry``), so the
DS-PRIVACY redaction runs on the ONE code path every caller shares (the "build a client directly in
the tool" anti-pattern is avoided for this name-capable surface). Default-disabled behaviour (no
``EPICS_MCP_OLOG_URL`` → no network call) is enforced there.
"""

from __future__ import annotations

from epics_pv_mcp.services.checkers import (
    query_olog_add_attachment,
    query_olog_create,
    query_olog_download,
    query_olog_entry,
    query_olog_list_attachments,
    query_olog_logbooks,
    query_olog_search,
    query_olog_tags,
)
from epics_pv_mcp.services.olog_client import DEFAULT_MAX_LOGS


def _split_paths(value: str | None) -> list[str]:
    """Split a comma-separated attachment-paths argument into stripped, non-empty paths.

    Comma is the tool-layer convention (as for logbooks/tags); a workspace file path rarely contains
    one. The service layer canonicalises + existence-checks each path (a comma inside a real path
    would surface there as a clear ``INVALID_INPUT``)."""
    if not value:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def _split_names(value: str | None) -> list[str]:
    """Split a comma-separated tool argument into a list of stripped, non-empty names."""
    if not value:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


async def _search_logbook(
    text: str | None = None,
    logbooks: str | None = None,
    tags: str | None = None,
    start: str | None = None,
    end: str | None = None,
    size: int = DEFAULT_MAX_LOGS,
    offset: int = 0,
    sort: str = "down",
    timeout: float = 5.0,
) -> dict[str, object]:
    """Search the Phoebus Olog logbook (see services.olog_client for the output posture).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_search`.
    """
    return await query_olog_search(
        text=text,
        logbooks=logbooks,
        tags=tags,
        start=start,
        end=end,
        size=size,
        offset=offset,
        sort=sort,
        timeout=timeout,
    )


async def _get_log_entry(log_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Return one Olog entry by id (see services.olog_client for the output posture).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_entry`.
    """
    return await query_olog_entry(log_id, timeout=timeout)


async def _list_logbooks(timeout: float = 5.0) -> dict[str, object]:
    """List the valid Olog logbook names (name-only; owners dropped).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_logbooks`.
    """
    return await query_olog_logbooks(timeout=timeout)


async def _list_tags(timeout: float = 5.0) -> dict[str, object]:
    """List the valid Olog tag names.

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_tags`.
    """
    return await query_olog_tags(timeout=timeout)


async def _create_log_entry(
    title: str,
    logbooks: str,
    description: str | None = None,
    level: str | None = None,
    tags: str | None = None,
    attachments: str | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Create a Phoebus Olog log entry, optionally with attachments. MUTATING, gated; the response
    follows the same output posture as a read.

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_create`. *logbooks* and
    *tags* are comma-separated names; *attachments* are comma-separated workspace file paths
    uploaded
    with the entry (create-with-attachments, arbitrary type/size); *embed_image_base64* is a single
    small inline image, embedded in the body via ``![](attachment/<uuid>)``.
    """
    return await query_olog_create(
        title=title,
        logbooks=_split_names(logbooks),
        description=description,
        level=level,
        tags=_split_names(tags) or None,
        attachments=_split_paths(attachments) or None,
        embed_image_base64=embed_image_base64,
        timeout=timeout,
    )


async def _reply_to_log(
    log_id: str,
    title: str,
    logbooks: str,
    description: str | None = None,
    level: str | None = None,
    tags: str | None = None,
    attachments: str | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Reply to an existing Olog entry (threads via the Log Entry Group). MUTATING, gated, redacted.

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_create` with
    ``in_reply_to=log_id`` — the same client/server code path as a create (a reply is its own entry,
    so it carries its OWN attachments). A *log_id* that does not identify an existing entry → HTTP
    400.
    """
    return await query_olog_create(
        title=title,
        logbooks=_split_names(logbooks),
        description=description,
        level=level,
        tags=_split_names(tags) or None,
        attachments=_split_paths(attachments) or None,
        embed_image_base64=embed_image_base64,
        in_reply_to=log_id,
        timeout=timeout,
    )


async def _add_log_attachment(
    log_id: str,
    attachments: str | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Attach files to an EXISTING Olog entry. MUTATING, gated, whole-mode only.

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_add_attachment`.
    *attachments* are comma-separated workspace file paths.
    """
    return await query_olog_add_attachment(
        log_id=log_id,
        attachments=_split_paths(attachments) or None,
        embed_image_base64=embed_image_base64,
        timeout=timeout,
    )


async def _download_log_attachment(
    log_id: str | None = None,
    filename: str | None = None,
    attachment_id: str | None = None,
    output_path: str | None = None,
    as_base64: bool = False,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Download one Olog attachment's raw bytes (posture-gated).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_download`.
    """
    return await query_olog_download(
        log_id=log_id,
        filename=filename,
        attachment_id=attachment_id,
        output_path=output_path,
        as_base64=as_base64,
        timeout=timeout,
    )


async def _list_log_attachments(log_id: str, timeout: float = 5.0) -> dict[str, object]:
    """List one Olog entry's attachments (filenames whole-mode only).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_list_attachments`.
    """
    return await query_olog_list_attachments(log_id, timeout=timeout)
