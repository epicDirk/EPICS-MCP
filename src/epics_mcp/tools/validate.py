"""Tool functions for validating EPICS PV connectivity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, NamedTuple

from opi_navigation.pv_analysis import analyze_pv_inventory, channel_name

from epics_mcp.config import get_config
from epics_mcp.display_files import DISPLAY_SUFFIX
from epics_mcp.errors import EpicsError
from epics_mcp.paths import resolve_user_path
from epics_mcp.services.epics_client import pv_get_batch

#: ⚠ What this check and the engine agree on is NOT the file name. ``find_bob_files`` deliberately
#: does not resolve symlinks, ``resolve_user_path`` here does, so a ``link.bob`` pointing at a
#: ``.txt`` IS collected by the engine (under the link's own name) and is refused below. Refusing
#: it stays correct for the OTHER reason: the lookup key ``rel`` is built from the resolved path
#: too, so it could never match that entry, and the previous code answered empty after a full
#: walk. Do not simplify the comment on the refusal to "the engine would ignore it".
_DISPLAY_SUFFIX = DISPLAY_SUFFIX

#: Which of the two legitimate questions about a ``.bob`` the caller is asking. They are different
#: questions with different answers, and answering one while the caller meant the other is what this
#: parameter exists to prevent (measured on a 257-display dataset: 54 files where the display view
#: is larger, 42 of them answering ``total: 0`` under the file view while the display resolves up to
#: 5846 channels).
PvView = Literal["file", "display"]


class _Extraction(NamedTuple):
    """What one inventory walk yields about *both* views of a display file.

    Both views come out of the SAME inventory object. The walk is the expensive half (measured 46 s
    on a 284-file dataset); deriving the second view from it costs 0.3 s, so the tool can always
    report the size of the view the caller did not ask for.
    """

    #: The channels that get connectivity-checked, selected by the requested view.
    channels: list[str]
    #: True when *channels* is a lower bound (the macro expansion was capped).
    capped: bool
    #: Size of the display view, reported under BOTH views so the file view can say what it omits.
    shown_by_display: int
    #: Channels the display view holds and the file view does not. Drives the note, and it is a set
    #: difference rather than a size comparison: the two views are not nested, because the file view
    #: aggregates over every top-level while the display view is this one display's own expansion.
    shown_only: int
    #: True when the DISPLAY view is a lower bound. See _display_view_is_capped for why this is not
    #: the same question as *capped*.
    shown_capped: bool


def _display_view_is_capped(rel: str, origins: set[str], capped_targets: frozenset[str]) -> bool:
    """Is the display view of *rel* a lower bound, because some context feeding it was capped?

    ``diagnostics.context_capped`` records the TARGET of a capped enqueue while the cap key is the
    pair ``(target, top)``, so the top axis, which is the one the display view lives on, is thrown
    away. Testing membership of *rel* alone therefore asks the wrong question: it asks whether
    contexts INTO rel were dropped, which is a statement about the file view.

    Measured with a cap lift from 256 to 1024, i.e. against which numbers actually grow when the
    budget does: over the 54 affected files of a 257-display dataset, 8 are provably lower bounds.
    Testing *rel* alone missed 4 of those 8. Adding the origins closes the gap to 0 missed, at the
    price of 34 over-cautious hits out of 42. The over-cautious direction is the correct one, and it
    matches what the sibling coverage_audit does with the same diagnostic (it consumes
    ``context_capped`` as a global bool rather than as a per-display membership test).

    The origins term is sound because the cap only fires after ``context_cap`` contexts have already
    flowed into X under this top, so a capped X has necessarily contributed PVs here and is visible
    among the origin files of this display's own events.
    """
    return rel in capped_targets or bool(origins & capped_targets)


def _run_validate(file_path: str, displays_dir: str | None, view: PvView = "file") -> _Extraction:
    """Extract the resolved, real (ca/pva) channels of *file_path* under the requested *view*.

    Blocking offline work (run off the event loop, like ``find_device._run_lookup``).
    Reuses the macro-aware ``opi_navigation`` Wedge-0 inventory, and reads TWO views out of it.

    ``view="file"`` (default, and the historical behaviour) **aggregates by ``origin_file``**: a
    PV's resolved value is attributed (lifted) to the operator-facing PARENT display, so keying on
    the file's own ``display_path`` would miss the PVs of an *embedded fragment*. We instead
    collect every resolved real PV whose physical origin is *file_path*, across all displays. That
    also picks up channels which only resolve because SOME OTHER parent bound the macros, so this
    view is not a subset of the other one.

    ``view="display"`` keys on ``display_path``: what this file resolves as a display in its own
    right, embedded fragments included. For a parent that only composes fragments the file view is
    empty and this one is not; for a fragment it is the other way round, because its macros are
    unbound when it is seeded standalone.

    Neither view is the correct one. They answer different questions, and the caller picks.

    *displays_dir* is the dataset ROOT (the inventory binds display macros via the
    operator top-levels found there). Without it the file's own directory is used,
    which under-resolves a fragment that needs ancestor macros, honest, since a
    connectivity check on still-templated macros is meaningless anyway. This bounds the display
    view too: its size is a function of the walked root, not of the display alone.

    Returns an :class:`_Extraction`; its field docs say what each number means and which of the two
    distinct lower-bound questions each flag answers.

    Two inputs are refused BEFORE the walk because each settles the answer alone: a *file_path*
    that is not a ``.bob`` (the inventory reads nothing else), and one that is not under the
    walked root (it can never match an ``origin_file``). Both used to be discovered AFTER a full
    inventory run, i.e. after the better part of a minute on a large dataset, for an answer that
    was already fixed. A ``.bob`` that simply declares no real channels is NOT one of these: that
    is a legitimate empty result, not a refusal.

    Raises:
        EpicsError(INVALID_INPUT): file_path / displays_dir missing, wrong kind, not a
            ``.bob`` display file, or file_path not under displays_dir.
        EpicsError(PATH_OUTSIDE_WORKSPACE): a path is outside the opt-in allowed_roots.
    """
    f = resolve_user_path(file_path, kind="file", label="file_path")
    if displays_dir:
        root = resolve_user_path(displays_dir, kind="dir", label="displays_dir")
    else:
        # No explicit root → walk the file's own directory, but boundary-check that
        # directory too (the path actually walked, not just the file itself).
        root = resolve_user_path(str(f.parent), kind="dir", label="displays_dir")
    # Both refusals below happen BEFORE the walk, because both decide the answer on their own.
    # The walk is the expensive half (measured 2026-08-01: 45 to 52 s over ten runs on a
    # 284-display dataset, of which 0.1 s is finding the files) and it does not depend on
    # file_path at all, so running it first meant waiting the better part of a minute for a
    # result that was settled before the first file was opened.
    #
    # They sit AFTER both resolve_user_path calls, not between them: a refusal placed in the
    # middle answers a caller whose displays_dir is ALSO bad with the suffix instead of the
    # boundary error the previous release gave (measured: PATH_OUTSIDE_WORKSPACE became
    # INVALID_INPUT). Validate every user path first, then decide, which is also the order
    # services/orchestration.py takes.
    if f.suffix.lower() != _DISPLAY_SUFFIX:
        found = f.suffix or "no suffix"
        # Name the RESOLVED path when it differs from what was passed: with a symlink the two
        # disagree, and quoting the raw name beside the target's suffix reads as a contradiction
        # ("got .txt: ...\link.bob").
        shown = str(f) if f != Path(file_path) else file_path
        raise EpicsError(
            f"file_path must be a {_DISPLAY_SUFFIX} display file (got {found}): {shown}. "
            f"The display inventory reads only {_DISPLAY_SUFFIX} files, so this call can only "
            f"come back empty. To check a plain list of PVs, pass pv_names instead.",
            error_code="INVALID_INPUT",
        )
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

    # Named for what the engine actually puts in here: the TARGET of a capped enqueue, not the top
    # it was capped under (expansion.py adds ``target`` while the cap key is the pair). The old name
    # ``capped_tops`` read as if it held top-levels and invited exactly the axis mix-up that
    # _display_view_is_capped exists to avoid. The membership test below is unchanged.
    capped_targets = frozenset(inventory.diagnostics.context_capped)

    # The FILE view, unchanged: every resolved real PV whose physical origin is this file, across
    # all top-levels, in document order.
    seen: set[str] = set()
    file_channels: list[str] = []
    file_capped = False
    for display in inventory.displays:
        for ev in display.pvs:
            if ev.origin_file != rel:
                continue
            if ev.resolution != "resolved" or ev.protocol not in ("ca", "pva"):
                continue
            if ev.top_level_display in capped_targets:
                file_capped = True
            channel = channel_name(ev.pv)  # strip pva://... for the live read
            if channel not in seen:
                seen.add(channel)
                file_channels.append(channel)

    # The DISPLAY view: what this file expands to as a display of its own. A single lookup rather
    # than a second sweep, and a None default rather than an index, because the inventory only
    # holds files that yielded at least one PV occurrence (measured: 284 .bob on disk, 257 entries),
    # and an existing test mocks an inventory that does not contain the queried file at all.
    target = next((d for d in inventory.displays if d.display_path == rel), None)
    display_seen: set[str] = set()
    display_channels: list[str] = []
    origins: set[str] = set()
    for ev in target.pvs if target else ():
        if ev.resolution != "resolved" or ev.protocol not in ("ca", "pva"):
            continue
        origins.add(ev.origin_file)
        # Normalise here as well: the engine keeps the protocol prefix as written, so a display
        # referencing both `SIM:X` and `ca://SIM:X` yields two events for ONE channel.
        channel = channel_name(ev.pv)
        if channel not in display_seen:
            display_seen.add(channel)
            display_channels.append(channel)
    display_capped = _display_view_is_capped(rel, origins, capped_targets)

    return _Extraction(
        channels=display_channels if view == "display" else file_channels,
        capped=display_capped if view == "display" else file_capped,
        shown_by_display=len(display_channels),
        shown_only=len(display_seen - seen),
        shown_capped=display_capped,
    )


async def _validate_pvs(
    pvs: list[str] | None = None,
    file_path: str | None = None,
    displays_dir: str | None = None,
    timeout: float | None = None,
    view: PvView = "file",
) -> dict[str, object]:
    """Check PV connectivity. Accepts a PV list or a .bob file path.

    file_path mode reuses the macro-aware ``opi_navigation`` inventory to extract the concrete,
    resolved ca/pva channels, under one of two views (see :func:`_run_validate`): ``view="file"``
    (default) takes what the file itself declares, aggregated by ``origin_file`` so embedded
    fragments work too; ``view="display"`` takes what the file resolves to when opened as a
    display, fragments included. The two differ a lot in practice, so the result always reports
    ``shown_by_display`` and, when the file view omits something, says so in ``notes``.

    Pass *displays_dir* = the dataset ROOT for full macro resolution; without it the file's own
    directory is used and fragments under-resolve. A ``notes`` entry flags when the PV list is a
    lower bound (the macro expansion hit the context cap). A *file_path* that is not a ``.bob``, or
    that lies outside the walked root, is refused immediately (see :func:`_run_validate`); note
    this only applies when *file_path* is used, an explicit *pvs* list wins and skips the file path
    entirely, along with *view*. NOTE: a full inventory walk is ~60 s for
    a large dataset, do not call this per-file in a tight loop. The connectivity reads go
    through ``pv_get_batch`` (native batch + concurrent fallback) in ``max_batch_size`` chunks,
    so a disconnected channel no longer serialises the whole check (M6).
    """
    notes: list[str] = []
    # Set only in file_path mode: with an explicit list there is no display to report about.
    view_fields: dict[str, object] = {}
    if file_path and not pvs:
        found = await asyncio.to_thread(_run_validate, file_path, displays_dir, view)
        extracted = found.channels
        # Always reported, not only when the note fires: a caller comparing the two views needs
        # the number in both cases, and a field that appears conditionally cannot be relied on.
        view_fields = {
            "shown_by_display": found.shown_by_display,
            "shown_by_display_capped": found.shown_capped,
        }
        if found.capped:
            notes.append(
                "PV list is a lower bound: this file's macro expansion hit the per-display "
                "context cap, so some instances were not enumerated."
            )
        if view == "file" and found.shown_only:
            # The whole point of the note is that the OTHER view exists and is bigger. It reports
            # the size of the difference, matching what it is triggered on; the two totals are not
            # nested sets, so comparing them would be a different (and weaker) statement.
            bound = " at least" if found.shown_capped else ""
            tail = (
                " That figure is a lower bound, the macro expansion was capped."
                if found.shown_capped
                else ""
            )
            notes.append(
                f"This is the file view: the check covers the {len(extracted)} channel(s) this "
                f".bob declares itself. Opened as a display it resolves{bound} "
                f"{found.shown_only} further channel(s) through the fragments it embeds. "
                f'Pass view="display" to check those instead.{tail}'
            )
        if not extracted:
            # Legitimate: the file declares zero resolved real PVs (a pure container,
            # or a fragment under-resolved without displays_dir). total:0, NOT an error.
            # This is also where a pure container lands under the file view, so it is where the
            # note above matters most (measured: 42 of 54 affected files in one dataset).
            empty: dict[str, object] = {
                "file_path": file_path,
                "total": 0,
                "connected": 0,
                "disconnected": 0,
                "pvs": [],
                **view_fields,
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
        **view_fields,
    }
    if notes:
        final["notes"] = notes
    return final
