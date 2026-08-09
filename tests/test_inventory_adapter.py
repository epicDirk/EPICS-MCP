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
    (they construct the models directly instead of running the analyzer).

    ⚠ The two ``isinstance`` assertions on the diagnostics tail below are a SHAPE check and nothing
    more: they were the only thing asserted about those values until GB-71, and a consumer counting
    them differently satisfies them perfectly. What the four display tools actually report, and that
    they all report the same, is in ``tests/test_diagnostics_tail.py``.

    ⚠ THE TREE HOLDS A TREND FILE SINCE GB-79, and the reason is that this guard could not see the
    engine's last widening. It laid a single ``.bob`` in the tree, so when the engine started
    collecting ``.plt`` files as well, nothing here changed and nothing went red: a drift guard
    whose fixture cannot express the drift is reporting on a question it never asked. The trend
    reaches BOTH adapters through the same seam as a display, so its trace channel is asserted the
    same way, and a future engine that stopped lifting trend PVs into the join would be caught
    here rather than in production."""
    root = tmp_path / "ds"
    root.mkdir()
    (root / "panel.bob").write_text(
        '<display version="2.0.0"><name>Panel</name>'
        '<widget type="textupdate"><name>s</name>'
        "<pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget></display>",
        encoding="utf-8",
    )
    # Opened by a button rather than embedded, deliberately: an open_file target is a top level of
    # its own and therefore operator-facing, and inventory_join_pvs keeps operator-facing top
    # levels only. An embedded trend would roll up into panel.bob and prove nothing about the seam.
    (root / "menu.bob").write_text(
        '<display version="2.0.0"><name>Menu</name>'
        '<widget type="action_button" version="3.0.0"><name>b</name><actions>'
        '<action type="open_file"><file>beam.plt</file><description>Trend</description></action>'
        "</actions></widget></display>",
        encoding="utf-8",
    )
    (root / "beam.plt").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        "<databrowser><title>Beam</title><pvlist><pv>"
        "<name>DEV-TEST01:Ctrl-EVR-01:trend</name><visible>true</visible><axis>0</axis>"
        "</pv></pvlist></databrowser>",
        encoding="utf-8",
    )
    channel = "DEV-TEST01:Ctrl-EVR-01:status"
    trend_channel = "DEV-TEST01:Ctrl-EVR-01:trend"

    join_pvs, context_capped, glob_capped = analyze_display_pvs(root)
    assert isinstance(context_capped, tuple)
    assert isinstance(glob_capped, int)
    jp = next(p for p in join_pvs if p.pv == channel)
    assert isinstance(jp, JoinPv)
    assert jp.display == "panel.bob"
    assert jp.resolution == "resolved"
    assert jp.protocol in ("ca", "pva")
    assert jp.role in ("read", "write")

    trend_jp = next((p for p in join_pvs if p.pv == trend_channel), None)
    assert trend_jp is not None, (
        "the trend's trace never reached the join; since GB-79 the engine collects .plt files, "
        "and a button-opened trend is a top level of its own"
    )
    assert trend_jp.display == "beam.plt"
    assert trend_jp.resolution == "resolved"
    # A Data Browser shows histories and never writes, so the engine reports read. Asserted rather
    # than accepted as "read or write" like the display row above: here the value is decidable.
    assert trend_jp.role == "read"

    index_rows, index_capped, index_glob = analyze_display_index(root)
    assert isinstance(index_capped, tuple)
    assert isinstance(index_glob, int)
    ir = next(r for r in index_rows if r.pv == channel)
    assert isinstance(ir, IndexRow)
    assert "panel.bob" in ir.displays
    assert ir.roles  # non-empty roles tuple

    trend_ir = next((r for r in index_rows if r.pv == trend_channel), None)
    assert trend_ir is not None, "the PV -> [displays] index dropped the trend's trace"
    assert "beam.plt" in trend_ir.displays
