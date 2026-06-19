"""Cross-plane PV provenance: Display (.bob) ↔ e3 IOC (st.cmd/.db) ↔ ESS Naming Service.

The first thread of the "connective tissue": join the raw PVs a set of displays reference
(:mod:`bob_pvs`) with what an e3 IOC actually serves (:mod:`e3_db`) and what the ESS Naming
Service registers (:mod:`naming_client`). Pure + deterministic; all network I/O is injected
(a :class:`NamingChecker`) so the join is testable offline.

**Honest buckets (v1 is deliberately coarse — see plan):**
- *linked*  — concrete display PVs that share the IOC device prefix (provenance link).
- *indeterminate* — display PVs that still contain ``$(...)`` macros: their per-instance
  identity needs the parked ``opi_navigation`` PV-inventory; never judged here.
- *broken*  — concrete linked PVs absent from the IOC ``.db`` set — ONLY computed when an
  IOC ``.db`` PV set is supplied (module repos are deferred), else left empty.
Nothing indeterminate is ever called "broken".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from epics_pv_mcp.services.e3_db import StCmdInfo
from epics_pv_mcp.services.naming_client import NameStatus


class NamingChecker(Protocol):
    """Minimal read-only Naming-Service contract (so tests can inject a fake)."""

    def validate_name(self, ess_name: str) -> NameStatus: ...


def _has_macro(pv: str) -> bool:
    return "$(" in pv or "${" in pv


class NamingResult(BaseModel):
    """Naming-Service verdict for the IOC's device name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registered: bool
    status: str
    message: str


class CrossPlaneReport(BaseModel):
    """Deterministic cross-plane provenance report (JSON via ``model_dump_json``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ioc_prefix: str | None
    ioc_device_name: str | None
    naming: NamingResult | None = None
    #: Displays with ≥1 concrete PV sharing the IOC prefix.
    displays_linked: tuple[str, ...] = ()
    #: Distinct concrete display PVs sharing the IOC prefix.
    pvs_linked: tuple[str, ...] = ()
    #: Concrete display PVs that do NOT share this IOC's prefix (likely other IOCs).
    pvs_other_prefix: tuple[str, ...] = ()
    #: Count of display PVs still carrying macros (need parked Stage-0 expansion).
    pvs_indeterminate: int = 0
    #: IOC .db PV counts (only when a .db set was supplied; module repos deferred).
    ioc_db_resolved: int = 0
    ioc_db_needs_msi: int = 0
    #: Concrete linked display PVs absent from the IOC .db (only when .db supplied).
    broken: tuple[str, ...] = ()
    #: Honest caveats about coverage limits.
    notes: tuple[str, ...] = ()


def crossplane_check(
    display_pvs: Mapping[str, list[str]],
    st_cmd: StCmdInfo,
    *,
    naming: NamingChecker | None = None,
    ioc_db: tuple[set[str], set[str]] | None = None,
) -> CrossPlaneReport:
    """Join display PVs with an IOC's st.cmd (+ optional .db) and the Naming Service.

    *display_pvs* maps display path → raw PV list (from :func:`bob_pvs.extract_pvs_from_dir`).
    *naming* (optional) is queried for the IOC device name; ``None`` skips the (network) check.
    *ioc_db* (optional) is ``(resolved, unresolved)`` from :func:`e3_db.ioc_db_pvs`; when
    supplied, concrete linked PVs missing from *resolved* are reported as ``broken``.
    """
    prefix = st_cmd.prefix
    linked_displays: set[str] = set()
    linked_pvs: set[str] = set()
    other_prefix_pvs: set[str] = set()
    indeterminate = 0

    for display, pvs in display_pvs.items():
        for pv in pvs:
            if _has_macro(pv):
                indeterminate += 1
                continue
            if prefix and pv.startswith(prefix):
                linked_pvs.add(pv)
                linked_displays.add(display)
            else:
                other_prefix_pvs.add(pv)

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
    if indeterminate:
        notes.append(
            f"{indeterminate} display PV reference(s) carry macros — per-instance identity "
            "needs the parked opi_navigation PV-inventory; not judged here."
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
        pvs_other_prefix=tuple(sorted(other_prefix_pvs)),
        pvs_indeterminate=indeterminate,
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
    lines.append(f"- **Concrete PVs with other prefixes:** {len(report.pvs_other_prefix)}")
    lines.append(f"- **Macro-templated (indeterminate):** {report.pvs_indeterminate}")
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
