"""Adapter: ``opi_navigation`` PV-inventory → cross-plane :class:`JoinPv` rows.

The macro-aware display-PV source for the cross-plane join — replaces the macro-blind ``bob_pvs``
extractor. Runs the SHA-pinned Wedge-0 inventory (:func:`analyze_pv_inventory`) over the project
ROOT and translates each **operator-facing** display's ``ExpandedPv`` instances into the narrow
:class:`JoinPv` seam. Embed-only fragment standalone seeds (``operator_facing=False``) are filtered
out HERE, so they never reach the join (otherwise fragment paths would be mis-attributed as
"displays" and the per-instance count would double via lift+seed).

This is the ONLY module that imports ``opi_navigation``; the join (:mod:`~.crossplane`) stays
standalone + offline-testable. The build-once PV engine is consumed, never rebuilt.
"""

from __future__ import annotations

from pathlib import Path

from opi_navigation.pv_analysis import DEFAULT_PV_CONTEXT_CAP, analyze_pv_inventory, channel_name
from opi_navigation.pv_analysis.models import REAL_PROTOCOLS, PvInventory

from epics_pv_mcp.services.crossplane import JoinPv

__all__ = ["DEFAULT_PV_CONTEXT_CAP", "analyze_display_pvs", "inventory_join_pvs"]


def inventory_join_pvs(inventory: PvInventory) -> list[JoinPv]:
    """Translate the **operator-facing** displays' ``ExpandedPv`` instances into ``JoinPv`` rows.

    Fragment standalone seeds (``operator_facing=False``) are skipped: their PVs already roll up to
    the embedding operator display, so counting the fragment as its own "display" would inflate the
    provenance and the indeterminate-occurrence count.

    The PV is normalized to its **channel name** for the real-channel protocols (ca/pva) — the join
    compares ``jp.pv`` against the protocol-free IOC prefix and ``.db`` records (``crossplane.py``
    startswith/broken), so an explicit ``pva://``/``ca://`` prefix would otherwise mis-bucket a
    prefix-sharing PV as ``other_prefix`` (and dodge ``broken``). This is the edge that keeps the
    join protocol-agnostic ("translation happens at the edge"); the protocol survives in
    ``JoinPv.protocol``. ``loc``/``sim``/``sys``/``other`` references are left RAW — they are only
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


def analyze_display_pvs(
    repo_root: Path,
    *,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> tuple[list[JoinPv], tuple[str, ...], int]:
    """Run the Wedge-0 inventory over *repo_root*; return the join input + incompleteness signals.

    *repo_root* must be the project/dataset ROOT (the operator top-levels there bind the display
    macros); a too-narrow per-IOC subdirectory leaves PVs ``dynamic`` and the join under-resolves.
    Returns ``(join_pvs, context_capped, glob_capped_count)`` — the latter two carry the inventory's
    honest lower-bound signals into the report. ``windows_paths`` resolves paths case-insensitively
    (Windows hosts); default Linux (= the ESS-console / CI truth, deterministic).
    """
    inventory = analyze_pv_inventory(
        repo_root, context_cap=context_cap, windows_paths=windows_paths
    )
    return (
        inventory_join_pvs(inventory),
        inventory.diagnostics.context_capped,
        len(inventory.diagnostics.glob_capped),
    )
