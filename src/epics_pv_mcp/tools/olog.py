"""MCP adapters for the Phoebus Olog logbook (read-only, DS-PRIVACY-redacted).

Thin wrappers: the config-gated, off-loop queries live in the services layer
(:func:`epics_pv_mcp.services.checkers.query_olog_search` / ``query_olog_entry``), so the
DS-PRIVACY redaction runs on the ONE code path every caller shares (the "build a client directly in
the tool" anti-pattern is avoided for this name-capable surface). Default-disabled behaviour (no
``EPICS_MCP_OLOG_URL`` → no network call) is enforced there.
"""

from __future__ import annotations

from epics_pv_mcp.services.checkers import query_olog_entry, query_olog_search
from epics_pv_mcp.services.olog_client import DEFAULT_MAX_LOGS


async def _search_logbook(
    text: str | None = None,
    logbooks: str | None = None,
    tags: str | None = None,
    start: str | None = None,
    end: str | None = None,
    size: int = DEFAULT_MAX_LOGS,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Search the Phoebus Olog logbook (DS-PRIVACY-redacted entries).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_search`.
    """
    return await query_olog_search(
        text=text, logbooks=logbooks, tags=tags, start=start, end=end, size=size, timeout=timeout
    )


async def _get_log_entry(log_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Return one Olog entry by id (DS-PRIVACY-redacted).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_olog_entry`.
    """
    return await query_olog_entry(log_id, timeout=timeout)
