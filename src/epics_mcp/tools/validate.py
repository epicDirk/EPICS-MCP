"""Tool functions for validating EPICS PV connectivity."""

from __future__ import annotations

import asyncio

from opi_navigation.pv_analysis import analyze_pv_inventory, channel_name

from epics_mcp.config import get_config
from epics_mcp.errors import EpicsError
from epics_mcp.paths import resolve_user_path
from epics_mcp.services.epics_client import pv_get_batch

#: The suffix a display file carries. The inventory reads NOTHING else: ``opi_navigation``'s
#: ``find_bob_files`` walks the root and keeps a candidate only when ``suffix.lower()`` equals this,
#: and every embed target is resolved against that already-collected set, so a file with any other
#: suffix can never surface as an ``origin_file``. Measured, not read off the source: a ``.txt``
#: holding valid display XML AND embedded by a ``.bob`` still never appears (and ``UPPER.BOB``
#: does, which is why the comparison must fold case).
#:
#: Deliberately a local constant rather than an import of the engine's private ``_BOB_SUFFIX``;
#: ``tests/test_validate.py`` pins the coupling against the real ``find_bob_files`` instead.
_DISPLAY_SUFFIX = ".bob"


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
    which under-resolves a fragment that needs ancestor macros, honest, since a
    connectivity check on still-templated macros is meaningless anyway.

    Returns ``(channels, capped)``. *capped* is True when the file's macro expansion
    hit the per-display context cap, so *channels* is a lower bound (a single file
    fanned out across thousands of template instances can exceed the cap).

    Two inputs are refused BEFORE the walk because each settles the answer alone: a *file_path*
    that is not a ``.bob`` (the inventory reads nothing else), and one that is not under the
    walked root (it can never match an ``origin_file``). Both used to be discovered AFTER a full
    inventory run, i.e. after ~40 s on a large dataset, for an answer that was already fixed.
    A ``.bob`` that simply declares no real channels is NOT one of these: that is a legitimate
    empty result, not a refusal.

    Raises:
        EpicsError(INVALID_INPUT): file_path / displays_dir missing, wrong kind, not a
            ``.bob`` display file, or file_path not under displays_dir.
        EpicsError(PATH_OUTSIDE_WORKSPACE): a path is outside the opt-in allowed_roots.
    """
    f = resolve_user_path(file_path, kind="file", label="file_path")
    # Both refusals below happen BEFORE the walk, because both decide the answer on their own.
    # The walk is the expensive half (measured: 40 s on a 284-display dataset, of which 0.1 s is
    # finding the files) and it does not depend on file_path at all, so running it first meant
    # waiting a minute for a result that was settled before the first file was opened. This is
    # the order services/orchestration.py already takes: validate every user path, then work.
    if f.suffix.lower() != _DISPLAY_SUFFIX:
        found = f.suffix or "no suffix"
        raise EpicsError(
            f"file_path must be a {_DISPLAY_SUFFIX} display file (got {found}): {file_path}. "
            f"The display inventory reads only {_DISPLAY_SUFFIX} files, so this one has no PVs "
            f"to extract. To check a plain list of PVs, pass pv_names instead.",
            error_code="INVALID_INPUT",
        )
    if displays_dir:
        root = resolve_user_path(displays_dir, kind="dir", label="displays_dir")
    else:
        # No explicit root → walk the file's own directory, but boundary-check that
        # directory too (the path actually walked, not just the file itself).
        root = resolve_user_path(str(f.parent), kind="dir", label="displays_dir")
    try:
        rel = f.relative_to(root).as_posix()
    except ValueError as exc:
        raise EpicsError(
            f"file_path is not under displays_dir: {file_path}",
            error_code="INVALID_INPUT",
        ) from exc
    # windows_paths=True: this server runs on a Windows host, so embedded <file>
    # refs resolve case-insensitively. It does NOT affect the origin_file/rel match
    # below (always posix), only cross-file embed resolution.
    inventory = analyze_pv_inventory(root, windows_paths=True)

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
            channel = channel_name(ev.pv)  # strip pva://... for the live read
            if channel not in seen:
                seen.add(channel)
                channels.append(channel)
    return channels, capped


async def _validate_pvs(
    pvs: list[str] | None = None,
    file_path: str | None = None,
    displays_dir: str | None = None,
    timeout: float | None = None,
) -> dict[str, object]:
    """Check PV connectivity. Accepts a PV list or a .bob file path.

    file_path mode reuses the macro-aware ``opi_navigation`` inventory to extract the
    concrete, resolved ca/pva channels the display references (aggregated by
    ``origin_file`` so embedded fragments work too). Pass *displays_dir* = the dataset
    ROOT for full macro resolution; without it the file's own directory is used and
    fragments under-resolve. A ``notes`` entry flags when the PV list is a lower bound
    (the macro expansion hit the context cap). A *file_path* that is not a ``.bob``, or that
    lies outside the walked root, is refused immediately (see :func:`_run_validate`); note this
    only applies when *file_path* is used, an explicit *pvs* list wins and skips the file path
    entirely. NOTE: a full inventory walk is ~60 s for
    a large dataset, do not call this per-file in a tight loop. The connectivity reads go
    through ``pv_get_batch`` (native batch + concurrent fallback) in ``max_batch_size`` chunks,
    so a disconnected channel no longer serialises the whole check (M6).
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

    # M6: reuse the shared batch primitive (native batch + M5's concurrent fallback) in
    # max_batch_size chunks instead of a serial per-PV read, and drop the duplicated
    # connected/disconnected classification, pv_get_batch already sorts good vs. disconnected
    # (results/errors), so a large display no longer takes n×timeout on a disconnected channel.
    cfg = get_config()
    results: list[dict[str, object]] = []
    connected = 0
    disconnected = 0
    for start in range(0, len(pvs), cfg.max_batch_size):
        chunk = pvs[start : start + cfg.max_batch_size]
        batch = await pv_get_batch(chunk, timeout)
        batch_results = batch["results"] if isinstance(batch["results"], list) else []
        batch_errors = batch["errors"] if isinstance(batch["errors"], list) else []
        # Emit in INPUT order (not connected-block-then-disconnected-block): iterate the chunk and
        # look each PV up in the batch's results/errors. pv_get_batch keys both by pv_name and a PV
        # lands in exactly one, so the counts are unchanged, only the ``pvs`` ordering is
        # stabilised to match the caller's list.
        by_result = {r["pv_name"]: r for r in batch_results}
        by_error = {e["pv_name"] for e in batch_errors}
        for name in chunk:
            if name in by_result:
                result = by_result[name]
                results.append(
                    {"pv_name": name, "status": "connected", "value": result.get("value")}
                )
            elif name in by_error:
                results.append({"pv_name": name, "status": "disconnected"})
        connected += len(batch_results)
        disconnected += len(batch_errors)

    final: dict[str, object] = {
        "total": len(pvs),
        "connected": connected,
        "disconnected": disconnected,
        "pvs": results,
    }
    if notes:
        final["notes"] = notes
    return final
