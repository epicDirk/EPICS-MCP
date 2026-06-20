"""Tool function for the cross-plane PV provenance check (Display ↔ e3 IOC ↔ Naming).

Read-only join of three planes opi-foundry owns separately: the PVs a set of ``.bob``
displays reference, the device prefix an e3 IOC ``st.cmd`` declares, and (optionally) the
ESS Naming Service registration status. Pure file I/O + one optional read-only HTTP ``GET``;
no running IOC and no PV writes. Mirrors the ``epics-crossplane`` CLI as an MCP tool so the
join is reachable from an agent, not only from the shell.

The join is deliberately coarse in v1: display PVs that still carry ``$(...)`` macros are
bucketed as *indeterminate* (their per-instance identity needs the parked ``opi_navigation``
PV-inventory) and are never judged "broken". See :mod:`epics_pv_mcp.services.crossplane`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.bob_pvs import extract_pvs_from_dir
from epics_pv_mcp.services.crossplane import crossplane_check, render_markdown
from epics_pv_mcp.services.e3_db import parse_st_cmd
from epics_pv_mcp.services.naming_client import NamingServiceClient


def _run_check(displays_dir: str, st_cmd_path: str, query_naming: bool) -> dict[str, object]:
    """Synchronous body of the cross-plane check (run off the event loop in a thread).

    Bundles the blocking work — recursive ``.bob`` reads, ``st.cmd`` read, and the optional
    Naming-Service GET — into one call so the async tool stays non-blocking.
    """
    display_pvs = extract_pvs_from_dir(displays_dir)
    st_info = parse_st_cmd(Path(st_cmd_path).read_text(encoding="utf-8"))
    naming = NamingServiceClient() if query_naming else None
    report = crossplane_check(display_pvs, st_info, naming=naming)
    return {
        "report": report.model_dump(mode="json"),
        "markdown": render_markdown(report),
    }


async def _crossplane_check(
    displays_dir: str,
    st_cmd_path: str,
    query_naming: bool = False,
) -> dict[str, object]:
    """Join display PVs with an e3 IOC ``st.cmd`` (+ optional Naming Service). Read-only.

    Returns ``{"report": <CrossPlaneReport JSON>, "markdown": <rendered report>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) when a path does not exist.
    """
    displays = Path(displays_dir)
    st_cmd = Path(st_cmd_path)
    if not displays.is_dir():
        raise EpicsError(
            f"displays_dir is not a directory: {displays_dir}",
            error_code="INVALID_INPUT",
        )
    if not st_cmd.is_file():
        raise EpicsError(
            f"st_cmd_path is not a file: {st_cmd_path}",
            error_code="INVALID_INPUT",
        )
    return await asyncio.to_thread(_run_check, displays_dir, st_cmd_path, query_naming)
