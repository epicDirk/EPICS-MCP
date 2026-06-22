"""Cross-plane PV provenance: Display (.bob) ↔ e3 IOC (st.cmd/.db) ↔ ESS Naming Service.

The first thread of the "connective tissue": join the **macro-expanded, per-instance** PVs a set
of displays reference (Wedge 0 / ``opi_navigation`` PV-inventory, fed in as :class:`JoinPv` rows by
the tool/CLI edge) with what an e3 IOC actually serves (:mod:`e3_db`) and what the ESS Naming
Service registers (:mod:`naming_client`). Pure + deterministic; all network I/O is injected (a
:class:`NamingChecker`) so the join is testable offline. This module stays **free of
``opi_navigation`` imports** — the ``ExpandedPv`` → :class:`JoinPv` translation happens at the edge.

**Honest buckets (Wedge 1 — concrete per-instance PVs, NO IOC .db yet):**
- *linked*  — concrete (``resolved``, real ca/pva) display PVs that share the IOC device prefix
  (provenance link); *linked_write* is the writable subset (operator can command the channel).
- *other_prefix* — concrete display PVs that do NOT share this IOC's prefix (likely other IOCs).
- *indeterminate* — display PVs the inventory could NOT resolve to a concrete channel: ``dynamic``
  (best-effort glob-guessed remainder) / ``unresolved`` (cyclic/unresolvable). Honest residue;
  never judged. (Before Wedge 1 this was every PV carrying a ``$(...)`` macro — a regex proxy; now
  it is exactly what the macro-expander could not resolve.)
- *non_channel* — references on non-channel protocols (loc/sim/sys/other), excluded from the IOC
  join (not real EPICS channels), reported separately rather than silently dropped.
- *broken*  — concrete linked PVs absent from the IOC ``.db`` set — ONLY computed when an IOC
  ``.db`` PV set is supplied (module repos are deferred), else left empty.
Nothing indeterminate is ever called "broken".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict

from epics_pv_mcp.services.e3_db import StCmdInfo
from epics_pv_mcp.services.naming_client import NameStatus

#: Protocols that are real plant channels (the only ones joined against an IOC). Mirrors
#: ``opi_navigation.pv_analysis.models.REAL_PROTOCOLS`` (kept local — no foreign import).
_REAL_PROTOCOLS = frozenset({"ca", "pva"})


class JoinPv(NamedTuple):
    """One macro-expanded, operator-facing display-PV instance fed into the join.

    The narrow seam the join needs from the ``opi_navigation`` PV-inventory: the tool/CLI edge
    translates each ``ExpandedPv`` of an **operator-facing** display into one of these (embed-only
    fragment standalone seeds are filtered out at the edge, so they never reach the join). The
    field literals match ``ExpandedPv.{resolution,role,protocol}`` verbatim.
    """

    display: str
    pv: str
    resolution: Literal["resolved", "dynamic", "unresolved"]
    role: Literal["read", "write"]
    protocol: Literal["ca", "pva", "loc", "sim", "sys", "other"]


class NamingChecker(Protocol):
    """Minimal read-only Naming-Service contract (so tests can inject a fake)."""

    def validate_name(self, ess_name: str) -> NameStatus: ...


class NamingResult(BaseModel):
    """Naming-Service verdict for the IOC's device name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registered: bool
    status: str
    message: str


class CrossPlaneReport(BaseModel):
    """Deterministic cross-plane provenance report (JSON via ``model_dump_json``).

    All PV tuples are sorted + distinct. ``pvs_indeterminate`` is the union of ``pvs_dynamic`` and
    ``pvs_unresolved`` (the unresolvable residue); ``pvs_indeterminate_occurrences`` counts
    distinct ``(display, pv)`` pairs across operator-facing displays (within-display duplicates
    collapsed), so it is ``>= len(pvs_indeterminate)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ioc_prefix: str | None
    ioc_device_name: str | None
    naming: NamingResult | None = None
    #: Operator-facing displays with ≥1 concrete PV sharing the IOC prefix.
    displays_linked: tuple[str, ...] = ()
    #: Distinct concrete display PVs sharing the IOC prefix.
    pvs_linked: tuple[str, ...] = ()
    #: Writable subset of ``pvs_linked`` (≥1 operator display writes the channel) — owner triage.
    pvs_linked_write: tuple[str, ...] = ()
    #: Concrete display PVs that do NOT share this IOC's prefix (likely other IOCs).
    pvs_other_prefix: tuple[str, ...] = ()
    #: Distinct display PVs the inventory could NOT resolve to a concrete channel —
    #: ``sorted(pvs_dynamic | pvs_unresolved)``. Concrete PVs are now linked/other, not here.
    pvs_indeterminate: tuple[str, ...] = ()
    #: Distinct (display, pv) pairs over dynamic+unresolved PVs; ``>= len(pvs_indeterminate)``.
    pvs_indeterminate_occurrences: int = 0
    #: Distinct PVs with a best-effort glob-guessed remainder (macro not fully resolved).
    pvs_dynamic: tuple[str, ...] = ()
    #: Distinct PVs the expander could not resolve at all (cyclic/unresolvable).
    pvs_unresolved: tuple[str, ...] = ()
    #: Distinct non-channel references (loc/sim/sys/other) excluded from the IOC join.
    pvs_non_channel: tuple[str, ...] = ()
    #: Operator-facing displays whose per-instance PVs are incomplete (inventory context cap) —
    #: their linked/other counts are a LOWER BOUND.
    displays_incomplete: tuple[str, ...] = ()
    #: IOC .db PV counts (only when a .db set was supplied; module repos deferred).
    ioc_db_resolved: int = 0
    ioc_db_needs_msi: int = 0
    #: Concrete linked display PVs absent from the IOC .db (only when .db supplied).
    broken: tuple[str, ...] = ()
    #: Honest caveats about coverage limits.
    notes: tuple[str, ...] = ()


def crossplane_check(
    join_pvs: Iterable[JoinPv],
    st_cmd: StCmdInfo,
    *,
    naming: NamingChecker | None = None,
    ioc_db: tuple[set[str], set[str]] | None = None,
    context_capped: tuple[str, ...] = (),
    glob_capped_count: int = 0,
) -> CrossPlaneReport:
    """Join macro-expanded display PVs with an IOC's st.cmd (+ optional .db) and the Naming Service.

    *join_pvs* are the per-instance, operator-facing display-PV rows (from the ``opi_navigation``
    PV-inventory, translated at the tool/CLI edge). *naming* (optional) is queried for the IOC
    device name; ``None`` skips it. *ioc_db* (optional) is ``(resolved, unresolved)``
    from :func:`e3_db.ioc_db_pvs`; when supplied, concrete linked PVs missing from *resolved* are
    reported as ``broken``. *context_capped* / *glob_capped_count* carry the inventory's honest
    incompleteness signals (linked/other become lower bounds).
    """
    prefix = st_cmd.prefix
    linked_displays: set[str] = set()
    linked_pvs: set[str] = set()
    linked_write: set[str] = set()
    other_prefix_pvs: set[str] = set()
    dynamic_pvs: set[str] = set()
    unresolved_pvs: set[str] = set()
    non_channel_pvs: set[str] = set()
    # Distinct (display, pv) pairs for the honest residue — robust against the same PV appearing
    # under multiple roles/origins within one display (the inventory dedups per display, but a PV
    # can recur with a different role/origin_file); counts references, not raw rows.
    indeterminate_pairs: set[tuple[str, str]] = set()

    for jp in join_pvs:
        if jp.protocol not in _REAL_PROTOCOLS:
            non_channel_pvs.add(jp.pv)
            continue
        if jp.resolution == "resolved":
            if prefix and jp.pv.startswith(prefix):
                linked_pvs.add(jp.pv)
                linked_displays.add(jp.display)
                if jp.role == "write":
                    linked_write.add(jp.pv)
            else:
                other_prefix_pvs.add(jp.pv)
        elif jp.resolution == "dynamic":
            dynamic_pvs.add(jp.pv)
            indeterminate_pairs.add((jp.display, jp.pv))
        else:  # "unresolved"
            unresolved_pvs.add(jp.pv)
            indeterminate_pairs.add((jp.display, jp.pv))

    indeterminate_pvs = dynamic_pvs | unresolved_pvs

    naming_result: NamingResult | None = None
    if naming is not None and st_cmd.device_name:
        status = naming.validate_name(st_cmd.device_name)
        naming_result = NamingResult(
            registered=status["registered"],
            status=status["status"],
            message=status["message"],
        )

    broken: set[str] = set()
    db_resolved = db_needs_msi = 0
    if ioc_db is not None:
        resolved, unresolved = ioc_db
        db_resolved, db_needs_msi = len(resolved), len(unresolved)
        broken = {pv for pv in linked_pvs if pv not in resolved}

    notes: list[str] = []
    if not prefix:  # None or "" — both mean "no usable IOC prefix" (join sends all to other-prefix)
        notes.append(
            "No IOC device prefix parsed from st.cmd — every concrete PV is reported as "
            "other-prefix (no provenance link possible)."
        )
    if indeterminate_pvs:
        notes.append(
            f"{len(indeterminate_pvs)} distinct display PV(s) ({len(indeterminate_pairs)} "
            "reference(s)) could not be resolved to a concrete channel (dynamic/unresolved) — "
            "honest residue, never judged here."
        )
    if non_channel_pvs:
        notes.append(
            f"{len(non_channel_pvs)} distinct non-channel reference(s) (loc/sim/sys/other) "
            "excluded from the IOC join — not real EPICS channels."
        )
    if context_capped:
        notes.append(
            f"{len(context_capped)} display(s) hit the inventory's per-instance context cap — "
            "their resolved PVs are a LOWER BOUND; 'linked'/'other-prefix' may undercount "
            "(re-run with a higher context cap)."
        )
    if glob_capped_count:
        notes.append(
            f"{glob_capped_count} template <file> reference(s) hit the glob cap — some embedded "
            "targets were dropped; coverage is a lower bound."
        )
    if not linked_pvs and not other_prefix_pvs and indeterminate_pvs:
        notes.append(
            "Almost no concrete PVs resolved — the displays directory may be too narrow "
            "(macros are bound by operator top-levels; pass the project/dataset ROOT)."
        )
    if ioc_db is None:
        notes.append(
            "No IOC .db PV set supplied (e3 module repos deferred, EM-C-modules): "
            "provenance is at device-prefix + Naming level only; no 'broken' verdict."
        )
    if db_needs_msi:
        notes.append(
            f"{db_needs_msi} IOC record(s) still macro-templated after substitution "
            "(needs msi / .substitutions expansion — Linux/Docker)."
        )

    return CrossPlaneReport(
        ioc_prefix=prefix,
        ioc_device_name=st_cmd.device_name,
        naming=naming_result,
        displays_linked=tuple(sorted(linked_displays)),
        pvs_linked=tuple(sorted(linked_pvs)),
        pvs_linked_write=tuple(sorted(linked_write)),
        pvs_other_prefix=tuple(sorted(other_prefix_pvs)),
        pvs_indeterminate=tuple(sorted(indeterminate_pvs)),
        pvs_indeterminate_occurrences=len(indeterminate_pairs),
        pvs_dynamic=tuple(sorted(dynamic_pvs)),
        pvs_unresolved=tuple(sorted(unresolved_pvs)),
        pvs_non_channel=tuple(sorted(non_channel_pvs)),
        displays_incomplete=tuple(sorted(context_capped)),
        ioc_db_resolved=db_resolved,
        ioc_db_needs_msi=db_needs_msi,
        broken=tuple(sorted(broken)),
        notes=tuple(notes),
    )


def render_markdown(report: CrossPlaneReport) -> str:
    """Render a :class:`CrossPlaneReport` as deterministic Markdown."""
    lines = ["# Cross-Plane PV Provenance", ""]
    lines.append(f"- **IOC prefix:** `{report.ioc_prefix or '—'}`")
    lines.append(f"- **IOC device name:** `{report.ioc_device_name or '—'}`")
    if report.naming is None:
        lines.append("- **Naming Service:** not checked (offline)")
    else:
        status = report.naming.status or "not found"
        flag = "✅ ACTIVE" if report.naming.registered else f"⚠️ {status}"
        lines.append(f"- **Naming Service:** {flag} — {report.naming.message}")
    lines.append("")
    lines.append(f"- **Displays linked to this IOC:** {len(report.displays_linked)}")
    lines.extend(f"  - {display}" for display in report.displays_linked)
    lines.append(f"- **Concrete PVs sharing the prefix:** {len(report.pvs_linked)}")
    if report.pvs_linked:
        lines.append(f"  - of which writable: {len(report.pvs_linked_write)}")
    lines.append(f"- **Concrete PVs with other prefixes:** {len(report.pvs_other_prefix)}")
    n_indet = len(report.pvs_indeterminate)
    refs = report.pvs_indeterminate_occurrences
    lines.append(f"- **Indeterminate (dynamic+unresolved):** {n_indet} ({refs} references)")
    if n_indet:
        lines.append(
            f"  - dynamic: {len(report.pvs_dynamic)}, unresolved: {len(report.pvs_unresolved)}"
        )
    if report.pvs_non_channel:
        lines.append(
            f"- **Non-channel refs (loc/sim/sys/other, excluded):** {len(report.pvs_non_channel)}"
        )
    if report.displays_incomplete:
        lines.append(
            f"- **Displays with incomplete inventory (lower bound):** "
            f"{len(report.displays_incomplete)}"
        )
        lines.extend(f"  - {display}" for display in report.displays_incomplete)
    if report.ioc_db_resolved or report.ioc_db_needs_msi:
        lines.append(
            f"- **IOC .db PVs:** {report.ioc_db_resolved} resolved, "
            f"{report.ioc_db_needs_msi} needs-msi"
        )
    if report.broken:
        lines.append(f"- **Broken (linked PV absent from IOC .db):** {len(report.broken)}")
        lines.extend(f"  - {pv}" for pv in report.broken)
    if report.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)
