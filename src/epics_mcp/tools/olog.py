"""MCP adapters for the Phoebus Olog logbook (reads + one gated write; entries leave whole).

Thin wrappers: the config-gated, off-loop queries live in the services layer
(:func:`epics_mcp.services.checkers.query_olog_search` / ``query_olog_entry``), so the output
shaping runs on the ONE code path every caller shares (the "build a client directly in
the tool" anti-pattern is avoided for this surface). Default-disabled behaviour (no
``EPICS_MCP_OLOG_URL`` → no network call) is enforced there.
"""

from __future__ import annotations

from epics_mcp.services.checkers import (
    query_olog_add_attachment,
    query_olog_create,
    query_olog_download,
    query_olog_entry,
    query_olog_levels,
    query_olog_list_attachments,
    query_olog_logbooks,
    query_olog_search,
    query_olog_tags,
    query_olog_update,
)
from epics_mcp.services.checkers_olog import (
    OlogAddAttachmentResult,
    OlogCreateResult,
    OlogDownloadResult,
    OlogEntryResult,
    OlogLevelsResult,
    OlogListAttachmentsResult,
    OlogLogbooksResult,
    OlogSearchResult,
    OlogTagsResult,
    OlogUpdateResult,
)
from epics_mcp.services.olog_client import DEFAULT_MAX_LOGS


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
    level: str | None = None,
    title: str | None = None,
    timeout: float = 5.0,
) -> OlogSearchResult:
    """Search the Phoebus Olog logbook (entries come back whole).

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_search`.
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
        level=level,
        title=title,
        timeout=timeout,
    )


async def _get_log_entry(log_id: str, timeout: float = 5.0) -> OlogEntryResult:
    """Return one Olog entry by id, whole.

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_entry`.
    """
    return await query_olog_entry(log_id, timeout=timeout)


async def _list_logbooks(timeout: float = 5.0) -> OlogLogbooksResult:
    """List the valid Olog logbook names (name-only).

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_logbooks`.
    """
    return await query_olog_logbooks(timeout=timeout)


async def _list_tags(timeout: float = 5.0) -> OlogTagsResult:
    """List the valid Olog tag names.

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_tags`.
    """
    return await query_olog_tags(timeout=timeout)


async def _list_log_levels(timeout: float = 5.0) -> OlogLevelsResult:
    """List the valid Olog log-level names (+ the default level).

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_levels`.
    """
    return await query_olog_levels(timeout=timeout)


async def _create_log_entry(
    title: str,
    logbooks: str,
    description: str | None = None,
    level: str | None = None,
    tags: str | None = None,
    attachments: str | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
) -> OlogCreateResult:
    """Create a Phoebus Olog log entry, optionally with attachments. MUTATING, gated; the response
    is the created entry whole.

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_create`. *logbooks* and
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
) -> OlogCreateResult:
    """Reply to an existing Olog entry (threads via the Log Entry Group). MUTATING, gated.

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_create` with
    ``in_reply_to=log_id``: the same client/server code path as a create (a reply is its own entry,
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
) -> OlogAddAttachmentResult:
    """Attach files to an EXISTING Olog entry. MUTATING, gated, whole-mode only.

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_add_attachment`.
    *attachments* are comma-separated workspace file paths.
    """
    return await query_olog_add_attachment(
        log_id=log_id,
        attachments=_split_paths(attachments) or None,
        embed_image_base64=embed_image_base64,
        timeout=timeout,
    )


async def _update_log_entry(
    log_id: str,
    title: str | None = None,
    description: str | None = None,
    level: str | None = None,
    logbooks: str | None = None,
    tags: str | None = None,
    timeout: float = 5.0,
) -> OlogUpdateResult:
    """Edit an EXISTING Olog entry's fields. MUTATING, gated, whole-mode only.

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_update`. An argument left
    at ``None`` leaves that field UNCHANGED. *logbooks* and *tags* are comma-separated names and
    REPLACE the entry's current list (they are not merged), passing an empty *tags* clears the
    tags, while an empty *logbooks* is refused (an entry must stay in at least one logbook).
    """
    return await query_olog_update(
        log_id=log_id,
        title=title,
        description=description,
        level=level,
        logbooks=_split_names(logbooks) if logbooks is not None else None,
        tags=_split_names(tags) if tags is not None else None,
        timeout=timeout,
    )


async def _download_log_attachment(
    log_id: str | None = None,
    filename: str | None = None,
    attachment_id: str | None = None,
    output_path: str | None = None,
    as_base64: bool = False,
    timeout: float = 5.0,
) -> OlogDownloadResult:
    """Download one Olog attachment's raw bytes (posture-gated).

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_download`.
    """
    return await query_olog_download(
        log_id=log_id,
        filename=filename,
        attachment_id=attachment_id,
        output_path=output_path,
        as_base64=as_base64,
        timeout=timeout,
    )


async def _list_log_attachments(log_id: str, timeout: float = 5.0) -> OlogListAttachmentsResult:
    """List one Olog entry's attachments (filenames whole-mode only).

    Thin MCP adapter over :func:`epics_mcp.services.checkers.query_olog_list_attachments`.
    """
    return await query_olog_list_attachments(log_id, timeout=timeout)
