"""Tool functions for validating EPICS PV connectivity."""

from __future__ import annotations

import asyncio

from opi_navigation.pv_analysis import analyze_pv_inventory, channel_name

from epics_pv_mcp.errors import (
    EpicsConnectionError,
    EpicsError,
    PVNotFoundError,
    PVTimeoutError,
)
from epics_pv_mcp.paths import resolve_user_path
from epics_pv_mcp.services.epics_client import pv_get


def _run_validate(file_path: str, displays_dir: str | None) -> tuple[list[str], bool]:
    """Extract the resolved, real (ca/pva) channels physically declared in *file_path*.

    Blocking offline work (run off the event loop, like ``find_device._run_lookup``).
    Reuses the macro-aware ``opi_navigation`` Wedge-0 inventory and **aggregates by
    ``origin_file``**: a PV's resolved value is attributed (lifted) to the
    operator-facing PARENT display, so keying on the file's own ``display_path``
    would miss the PVs of an *embedded fragment*. We instead collect every resolved
    real PV whose physical origin is *file_path*, across all displays.

    *displays_dir* is the dataset ROOT (the inventory binds display macros via the
    operator top-levels found there). Without it the file's own directory is used,
    which under-resolves a fragment that needs ancestor macros — honest, since a
    connectivity check on still-templated macros is meaningless anyway.

    Returns ``(channels, capped)``. *capped* is True when the file's macro expansion
    hit the per-display context cap, so *channels* is a lower bound (a single file
    fanned out across thousands of template instances can exceed the cap).

    Raises:
        EpicsError(INVALID_INPUT): file_path / displays_dir missing, wrong kind, or
            file_path not under displays_dir.
        EpicsError(PATH_OUTSIDE_WORKSPACE): a path is outside the opt-in allowed_roots.
    """
    f = resolve_user_path(file_path, kind="file", label="file_path")
    if displays_dir:
        root = resolve_user_path(displays_dir, kind="dir", label="displays_dir")
    else:
        # No explicit root → walk the file's own directory, but boundary-check that
        # directory too (the path actually walked, not just the file itself).
        root = resolve_user_path(str(f.parent), kind="dir", label="displays_dir")
    inventory = analyze_pv_inventory(root, windows_paths=True)
    try:
        rel = f.relative_to(root).as_posix()
    except ValueError as exc:
        raise EpicsError(
            f"file_path is not under displays_dir: {file_path}",
            error_code="INVALID_INPUT",
        ) from exc

    capped_tops = set(inventory.diagnostics.context_capped)
    seen: set[str] = set()
    channels: list[str] = []
    capped = False
    for display in inventory.displays:
        for ev in display.pvs:
            if ev.origin_file != rel:
                continue
            if ev.resolution != "resolved" or ev.protocol not in ("ca", "pva"):
                continue
            if ev.top_level_display in capped_tops:
                capped = True
            channel = channel_name(ev.pv)  # strip pva://… for the live read
            if channel not in seen:
                seen.add(channel)
                channels.append(channel)
    return channels, capped


async def _validate_pvs(
    pvs: list[str] | None = None,
    file_path: str | None = None,
    displays_dir: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Check PV connectivity. Accepts a PV list or a .bob file path.

    file_path mode reuses the macro-aware ``opi_navigation`` inventory to extract the
    concrete, resolved ca/pva channels the display references (aggregated by
    ``origin_file`` so embedded fragments work too). Pass *displays_dir* = the dataset
    ROOT for full macro resolution; without it the file's own directory is used and
    fragments under-resolve. A ``notes`` entry flags when the PV list is a lower bound
    (the macro expansion hit the context cap). NOTE: a full inventory walk is ~60 s for
    a large dataset, and a file reused across many instances yields many channels read
    sequentially — do not call this per-file in a tight loop.
    """
    notes: list[str] = []
    if file_path and not pvs:
        extracted, capped = await asyncio.to_thread(_run_validate, file_path, displays_dir)
        if capped:
            notes.append(
                "PV list is a lower bound: this file's macro expansion hit the per-display "
                "context cap, so some instances were not enumerated."
            )
        if not extracted:
            # Legitimate: the file declares zero resolved real PVs (a pure container,
            # or a fragment under-resolved without displays_dir). total:0, NOT an error.
            empty: dict[str, object] = {
                "file_path": file_path,
                "total": 0,
                "connected": 0,
                "disconnected": 0,
                "pvs": [],
            }
            if notes:
                empty["notes"] = notes
            return empty
        pvs = extracted

    if not pvs:
        raise EpicsError(
            "Provide either pvs list or file_path",
            error_code="INVALID_INPUT",
        )

    # Try to get each PV, classify as connected or disconnected
    results = []
    connected = 0
    disconnected = 0
    for pv_name in pvs:
        try:
            result = await pv_get(pv_name, timeout)
            results.append(
                {
                    "pv_name": pv_name,
                    "status": "connected",
                    "value": result.get("value"),
                }
            )
            connected += 1
        except (PVTimeoutError, PVNotFoundError, EpicsConnectionError):
            results.append({"pv_name": pv_name, "status": "disconnected"})
            disconnected += 1

    final: dict[str, object] = {
        "total": len(pvs),
        "connected": connected,
        "disconnected": disconnected,
        "pvs": results,
    }
    if notes:
        final["notes"] = notes
    return final
