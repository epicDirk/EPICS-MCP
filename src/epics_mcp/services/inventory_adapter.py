"""Adapter: ``opi_navigation`` PV-inventory → cross-plane :class:`JoinPv` rows.

The macro-aware display-PV source for the cross-plane join, replaces the macro-blind ``bob_pvs``
extractor. Runs the SHA-pinned Wedge-0 inventory (:func:`analyze_pv_inventory`) over the project
ROOT and translates each **operator-facing** display's ``ExpandedPv`` instances into the narrow
:class:`JoinPv` seam. Embed-only fragment standalone seeds (``operator_facing=False``) are filtered
out HERE, so they never reach the join (otherwise fragment paths would be mis-attributed as
"displays" and the per-instance count would double via lift+seed).

This is the ONLY module that RUNS the engine: every ``analyze_pv_inventory`` call in ``src/`` goes
through :func:`analyze_inventory` below, so the walk and its diagnostics tail are read once, in one
place, for all four display tools. The join (:mod:`~.crossplane`) stays standalone +
offline-testable. The build-once PV engine is consumed, never rebuilt.

⚠ "Runs the engine", not "imports ``opi_navigation``". The sentence here used to claim the latter
and it was measurably false: ``services/device_lookup.py``, ``display_tools.py``,
``tools/validate.py`` and ``tools/find_device.py`` all import from the engine as well, for
``channel_name``, ``contains_macros`` and the lookup types. Those are pure helpers with no walk and
no diagnostics behind them; the claim that carries is about the CALL, and
``tests/test_diagnostics_tail.py`` enforces exactly that one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from opi_navigation.pv_analysis import DEFAULT_PV_CONTEXT_CAP, analyze_pv_inventory, channel_name
from opi_navigation.pv_analysis.models import REAL_PROTOCOLS, PvInventory

from epics_mcp.services.coverage import IndexRow
from epics_mcp.services.crossplane import JoinPv

__all__ = [
    "DEFAULT_PV_CONTEXT_CAP",
    "analyze_display_index",
    "analyze_display_pvs",
    "analyze_inventory",
    "inventory_join_pvs",
]


def inventory_join_pvs(inventory: PvInventory) -> list[JoinPv]:
    """Translate the **operator-facing** displays' ``ExpandedPv`` instances into ``JoinPv`` rows.

    Fragment standalone seeds (``operator_facing=False``) are skipped: their PVs already roll up to
    the embedding operator display, so counting the fragment as its own "display" would inflate the
    provenance and the indeterminate-occurrence count.

    The PV is normalized to its **channel name** for the real-channel protocols (ca/pva), the join
    compares ``jp.pv`` against the protocol-free IOC prefix and ``.db`` records (``crossplane.py``
    startswith/broken), so an explicit ``pva://``/``ca://`` prefix would otherwise mis-bucket a
    prefix-sharing PV as ``other_prefix`` (and dodge ``broken``). This is the edge that keeps the
    join protocol-agnostic ("translation happens at the edge"); the protocol survives in
    ``JoinPv.protocol``. ``loc``/``sim``/``sys``/``other`` references are left RAW, they are only
    displayed in ``non_channel`` (never prefix-compared), so stripping would drop their tag and risk
    colliding with a real channel of the same bare name.
    """
    return [
        JoinPv(
            display=display.display_path,
            pv=channel_name(expanded.pv) if expanded.protocol in REAL_PROTOCOLS else expanded.pv,
            resolution=expanded.resolution,
            role=expanded.role,
            protocol=expanded.protocol,
        )
        for display in inventory.displays
        if display.operator_facing
        for expanded in display.pvs
    ]


def analyze_inventory[T](
    repo_root: Path,
    project: Callable[[PvInventory], T],
    *,
    context_cap: int,
    windows_paths: bool,
) -> tuple[T, tuple[str, ...], int]:
    """Run the Wedge-0 inventory ONCE, project it, and pair the result with the DIAGNOSTICS TAIL.

    **The one place that reads the tail.** Every tool that reports the inventory's incompleteness
    signals goes through here, so the four display tools (``crossplane_check``, ``coverage_audit``,
    ``validate_pvs``, ``find_device``) cannot drift apart in what they count. They had three
    separate copies of this read before, and one of them, ``find_device``, had simply forgotten it
    (GB-65). Nothing detected that, because the only assertion on the two values was an
    ``isinstance`` check.

    *project* is unconstrained in its return type on purpose: a caller that needs rows gets rows
    (``inventory_join_pvs``, :func:`_index_rows`), one that needs a lookup result or the raw sweep
    of a single file returns that instead. Narrowing it to ``list[T]``, as it was, is what forced
    the two tools to call the engine directly and grow their own copy of the read.

    Returns ``(projected, context_capped, glob_capped_count)``.

    ⚠ ``glob_capped_count`` counts **PAIRS**, not source displays. The engine records
    ``(source display, raw <file> target)``, and one source can cap several distinct targets, so
    counting sources reports a smaller number for the same walk. Both readings are defensible in
    isolation and only one may be reported, because two denominators for one category inside one
    report are the second truth the project forbids. The pair reading is the one every tool has
    always shipped; ``tests/test_inventory_adapter.py`` pins the four against each other with a
    fixture whose pair count and source count deliberately differ.

    ⚠ ``context_capped`` is handed on as the engine's TUPLE, not as a set or a bool. Callers read
    it differently on purpose: the coverage plane collapses it to one global flag, while
    ``validate_pvs`` tests membership per file. Deciding that here would silently pick one of those
    questions for everybody.
    """
    inventory = analyze_pv_inventory(
        repo_root, context_cap=context_cap, windows_paths=windows_paths
    )
    return (
        project(inventory),
        inventory.diagnostics.context_capped,
        len(inventory.diagnostics.glob_capped),
    )


def _index_rows(inventory: PvInventory) -> list[IndexRow]:
    """Project the inventory's global ``PV → [displays]`` index into :class:`IndexRow` rows.

    The index is real-protocol only, so each ``pv`` is normalized to its protocol-free channel name.
    """
    return [
        IndexRow(
            pv=channel_name(entry.pv),
            displays=tuple(str(display) for display in entry.displays),
            roles=tuple(str(role) for role in entry.roles),
        )
        for entry in inventory.index
    ]


def analyze_display_pvs(
    repo_root: Path,
    *,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> tuple[list[JoinPv], tuple[str, ...], int]:
    """Run the Wedge-0 inventory over *repo_root*; return the join input + incompleteness signals.

    *repo_root* must be the project/dataset ROOT (the operator top-levels there bind the display
    macros); a too-narrow per-IOC subdirectory leaves PVs ``dynamic`` and the join under-resolves.
    Returns ``(join_pvs, context_capped, glob_capped_count)``, the latter two carry the inventory's
    honest lower-bound signals into the report. ``windows_paths`` resolves paths case-insensitively
    (Windows hosts); default Linux (= the ESS-console / CI truth, deterministic).
    """
    return analyze_inventory(
        repo_root, inventory_join_pvs, context_cap=context_cap, windows_paths=windows_paths
    )


def analyze_display_index(
    repo_root: Path,
    *,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> tuple[list[IndexRow], tuple[str, ...], int]:
    """Run the Wedge-0 inventory over *repo_root*; return the ``PV → [displays]`` index as rows.

    Symmetric to :func:`analyze_display_pvs`, but reads the inventory's ``index`` field (the global
    operator-facing, resolved, real-protocol PV→[screens] index) instead of the per-display PV
    lists, the input the coverage audit's display set ``D`` needs. *repo_root* must be the dataset
    ROOT (the operator top-levels there bind the display macros). Returns ``(index_rows,
    context_capped, glob_capped_count)``; the latter two carry the inventory's lower-bound signals.
    """
    return analyze_inventory(
        repo_root, _index_rows, context_cap=context_cap, windows_paths=windows_paths
    )
