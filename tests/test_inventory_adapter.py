"""Tests for the opi_navigation PV-inventory → JoinPv adapter (operator-facing filter).

The QA-High regression: ``inv.displays`` seeds EVERY .bob standalone, so embed-only fragments get
their own ``DisplayPvInventory`` (``operator_facing=False``). The adapter MUST skip them — otherwise
a fragment path is mis-attributed as a "display" and the lifted PV is double-counted (once via the
operator parent, once via the fragment seed).
"""

from opi_navigation.pv_analysis.models import DisplayPvInventory, ExpandedPv, PvInventory

from epics_pv_mcp.services.crossplane import JoinPv
from epics_pv_mcp.services.inventory_adapter import inventory_join_pvs


def _ev(pv: str, top: str, *, origin: str | None = None, role: str = "read") -> ExpandedPv:
    return ExpandedPv(
        pv=pv,
        raw_pv="$(DEV):St",
        resolution="resolved",
        role=role,  # type: ignore[arg-type]
        protocol="ca",
        top_level_display=top,
        origin_file=origin or top,
    )


def test_inventory_join_skips_fragment_seeds() -> None:
    """A PV lifted to the operator parent appears ONCE (via the parent); the embed-only fragment's
    standalone seed (operator_facing=False) is filtered out — no double attribution / double count.
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
    (the join itself classifies them) — the adapter does not pre-filter by resolution/protocol.
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
