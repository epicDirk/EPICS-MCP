"""Tests for the opi_navigation PV-inventory → JoinPv adapter (operator-facing filter).

The QA-High regression: ``inv.displays`` seeds EVERY .bob standalone, so embed-only fragments get
their own ``DisplayPvInventory`` (``operator_facing=False``). The adapter MUST skip them, otherwise
a fragment path is mis-attributed as a "display" and the lifted PV is double-counted (once via the
operator parent, once via the fragment seed).
"""

from pathlib import Path

from opi_navigation.pv_analysis.models import DisplayPvInventory, ExpandedPv, PvInventory

from epics_mcp.services.coverage import IndexRow
from epics_mcp.services.crossplane import JoinPv
from epics_mcp.services.inventory_adapter import (
    analyze_display_index,
    analyze_display_pvs,
    inventory_join_pvs,
)


def _ev(
    pv: str,
    top: str,
    *,
    origin: str | None = None,
    role: str = "read",
    protocol: str = "ca",
    resolution: str = "resolved",
) -> ExpandedPv:
    return ExpandedPv(
        pv=pv,
        raw_pv="$(DEV):St",
        resolution=resolution,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        protocol=protocol,  # type: ignore[arg-type]
        top_level_display=top,
        origin_file=origin or top,
    )


def test_inventory_join_skips_fragment_seeds() -> None:
    """A PV lifted to the operator parent appears ONCE (via the parent); the embed-only fragment's
    standalone seed (operator_facing=False) is filtered out, no double attribution / double count.
    """
    inv = PvInventory(
        repo_root="x",
        displays=(
            DisplayPvInventory(
                display_path="screen.bob",
                operator_facing=True,
                pvs=(_ev("VAC01:St", "screen.bob", origin="frag.bob"),),
            ),
            DisplayPvInventory(
                display_path="frag.bob",
                operator_facing=False,
                pvs=(_ev("VAC01:St", "frag.bob", origin="frag.bob"),),
            ),
        ),
    )
    rows = inventory_join_pvs(inv)
    assert rows == [
        JoinPv(
            display="screen.bob",
            pv="VAC01:St",
            resolution="resolved",
            role="read",
            protocol="ca",
        )
    ]


def test_inventory_join_keeps_all_buckets_of_operator_displays() -> None:
    """Within an operator-facing display, ALL resolution/protocol buckets are forwarded
    (the join itself classifies them), the adapter does not pre-filter by resolution/protocol.
    """
    inv = PvInventory(
        repo_root="x",
        displays=(
            DisplayPvInventory(
                display_path="op.bob",
                operator_facing=True,
                pvs=(
                    ExpandedPv(
                        pv="SYS:R",
                        raw_pv="SYS:R",
                        resolution="resolved",
                        role="read",
                        protocol="ca",
                        top_level_display="op.bob",
                        origin_file="op.bob",
                    ),
                    ExpandedPv(
                        pv="$(X):Dyn",
                        raw_pv="$(X):Dyn",
                        resolution="dynamic",
                        role="read",
                        protocol="ca",
                        top_level_display="op.bob",
                        origin_file="op.bob",
                    ),
                    ExpandedPv(
                        pv="loc-sig",
                        raw_pv="loc://sig",
                        resolution="resolved",
                        role="read",
                        protocol="loc",
                        top_level_display="op.bob",
                        origin_file="op.bob",
                    ),
                ),
            ),
        ),
    )
    rows = inventory_join_pvs(inv)
    assert {(r.pv, r.resolution, r.protocol) for r in rows} == {
        ("SYS:R", "resolved", "ca"),
        ("$(X):Dyn", "dynamic", "ca"),
        ("loc-sig", "resolved", "loc"),
    }


def test_inventory_join_normalizes_real_channel_protocols() -> None:
    """Wedge-1 mini-fix (Option A), full protocol × normalization matrix.

    The adapter strips the ca/pva protocol prefix so the join can compare a channel name against the
    protocol-free IOC prefix/.db (translation at the edge); the protocol survives in
    ``JoinPv.protocol``. The guard is on PROTOCOL, not resolution (sharp-edge §5): a ``pva://`` PV
    is normalized even when ``dynamic`` (real channel everywhere), while ``loc``/``sim``/``sys``
    keep their RAW form regardless (only displayed in ``non_channel``, never prefix-compared:
    stripping drops the tag and could collide with a real bare channel). A bare ca is idempotent.
    """
    pre = "DEV-TEST01:Ctrl-EVR-01:"
    inv = PvInventory(
        repo_root="x",
        displays=(
            DisplayPvInventory(
                display_path="op.bob",
                operator_facing=True,
                pvs=(
                    _ev(f"pva://{pre}X", "op.bob", protocol="pva"),  # pva:// stripped
                    _ev(f"ca://{pre}Y", "op.bob", protocol="ca"),  # ca:// stripped
                    _ev(f"{pre}Bare", "op.bob", protocol="ca"),  # bare ca untouched (idempotent)
                    # dynamic pva:// is STILL normalized, the guard is on protocol, not resolution.
                    _ev(f"pva://{pre}$(N)Dyn", "op.bob", protocol="pva", resolution="dynamic"),
                    _ev("loc://state", "op.bob", protocol="loc"),  # loc:// kept raw
                    _ev("sim://ramp", "op.bob", protocol="sim"),  # sim:// kept raw
                    _ev("sys://TIME", "op.bob", protocol="sys"),  # sys:// kept raw
                ),
            ),
        ),
    )
    rows = {(r.pv, r.protocol) for r in inventory_join_pvs(inv)}
    assert rows == {
        (f"{pre}X", "pva"),  # pva:// stripped; protocol kept in its own field
        (f"{pre}Y", "ca"),  # ca:// stripped
        (f"{pre}Bare", "ca"),  # bare ca untouched (idempotent)
        (f"{pre}$(N)Dyn", "pva"),  # dynamic pva:// also stripped (protocol-guard, not resolution)
        ("loc://state", "loc"),  # loc:// kept raw, only displayed, never prefix-compared
        ("sim://ramp", "sim"),  # sim:// kept raw
        ("sys://TIME", "sys"),  # sys:// kept raw
    }


def test_analyze_adapters_smoke_over_real_bob(tmp_path: Path) -> None:
    """L-Arch-5 build-once drift guard: run the REAL analyze_pv_inventory (opi_navigation) through
    BOTH adapters over a tiny .bob ROOT and assert the consumed JoinPv/IndexRow fields survive.

    Fails LOUD if the SHA-pinned opi_navigation renames/removes a consumed field or changes the
    analyze_pv_inventory API/return shape, the seam the hand-built model tests above cannot catch
    (they construct the models directly instead of running the analyzer)."""
    root = tmp_path / "ds"
    root.mkdir()
    (root / "panel.bob").write_text(
        '<display version="2.0.0"><name>Panel</name>'
        '<widget type="textupdate"><name>s</name>'
        "<pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget></display>",
        encoding="utf-8",
    )
    channel = "DEV-TEST01:Ctrl-EVR-01:status"

    join_pvs, context_capped, glob_capped = analyze_display_pvs(root)
    assert isinstance(context_capped, tuple)
    assert isinstance(glob_capped, int)
    jp = next(p for p in join_pvs if p.pv == channel)
    assert isinstance(jp, JoinPv)
    assert jp.display == "panel.bob"
    assert jp.resolution == "resolved"
    assert jp.protocol in ("ca", "pva")
    assert jp.role in ("read", "write")

    index_rows, index_capped, index_glob = analyze_display_index(root)
    assert isinstance(index_capped, tuple)
    assert isinstance(index_glob, int)
    ir = next(r for r in index_rows if r.pv == channel)
    assert isinstance(ir, IndexRow)
    assert "panel.bob" in ir.displays
    assert ir.roles  # non-empty roles tuple
