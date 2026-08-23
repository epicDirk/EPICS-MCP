"""Cross-plane PV provenance: Display (.bob/.plt) ↔ e3 IOC (st.cmd/.db) ↔ ESS Naming Service.

The first thread of the "connective tissue": join the **macro-expanded, per-instance** PVs a set
of displays reference (Wedge 0 / ``opi_navigation`` PV-inventory, fed in as :class:`JoinPv` rows by
the tool/CLI edge) with what an e3 IOC actually serves (:mod:`e3_db`) and what the ESS Naming
Service registers (:mod:`naming_client`). Pure + deterministic; all network I/O is injected (a
:class:`NamingChecker`) so the join is testable offline. This module stays **free of
``opi_navigation`` imports**, the ``ExpandedPv`` → :class:`JoinPv` translation happens at the edge.

**Honest buckets (Wedge 1, concrete per-instance PVs, NO IOC .db yet):**
- *linked*, concrete (``resolved``, real ca/pva) display PVs that share the IOC device prefix
  (provenance link); *linked_write* is the writable subset (operator can command the channel).
- *other_prefix*, concrete display PVs that do NOT share this IOC's prefix (likely other IOCs).
- *indeterminate*, display PVs the inventory could NOT resolve to a concrete channel: ``dynamic``
  (best-effort glob-guessed remainder) / ``unresolved`` (cyclic/unresolvable). Honest residue;
  never judged. (Before Wedge 1 this was every PV carrying a ``$(...)`` macro, a regex proxy; now
  it is exactly what the macro-expander could not resolve.)
- *non_channel*, references on non-channel protocols (loc/sim/sys/other), excluded from the IOC
  join (not real EPICS channels), reported separately rather than silently dropped, and split by
  protocol as a partition of the same set (``pvs_non_channel_by_protocol``).
- *broken*, concrete linked PVs absent from the IOC ``.db`` set, ONLY computed when a
  **provably complete + fully resolved** IOC ``.db`` set is supplied (``ioc_db_complete`` and no
  ``needs-msi`` residue). An incomplete or still-templated set cannot prove a linked PV's ABSENCE
  (the record may exist under a name not yet expanded), so the verdict is **withheld**, never
  false-flagged. *broken_write* is the writable subset (a dead command/setpoint target).
- *cf_unregistered*, concrete linked PVs (record names) NOT registered in **ChannelFinder** (the
  runtime PV directory), when a ``ChannelFinderChecker`` is injected. A SEPARATE plane from *broken*
  (runtime registry vs. static ``.db``): a PV may be in both, one, or neither. Withheld (never
  false-flagged) when the registry query is truncated/unavailable. Honest against a partial test CF:
  a high cf_unregistered/linked ratio is surfaced as "likely incomplete CF", not a defect list.
Nothing indeterminate is ever called "broken".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict

from epics_mcp.display_files import KIND_MARKERS
from epics_mcp.services.e3_db import StCmdInfo
from epics_mcp.services.naming_client import NameStatus
from epics_mcp.services.naming_exceptions import NamingServiceResponseError

logger = logging.getLogger(__name__)

#: What a Data Browser trend is called in this report's file list. Read from the shared table
#: rather than spelled here, so this renderer and ``find_device``'s cannot say it differently.
#: ``tests/test_crossplane.py`` holds THIS renderer against the table; the three-way identity with
#: ``device_lookup`` lives in ``tests/test_device_lookup.py``, because that module reaches
#: ``opi_navigation`` and this one runs on an engine-less install too.
_TREND_MARKER = KIND_MARKERS["trend"]

#: Protocols that are real plant channels (the only ones joined against an IOC). Mirrors
#: ``opi_navigation.pv_analysis.models.REAL_PROTOCOLS`` (kept local, no foreign import).
_REAL_PROTOCOLS = frozenset({"ca", "pva"})

#: The order the non-channel breakdown is reported in: the one every text in this module already
#: uses ("loc/sim/sys/other"), so the numbers read in the order the prose taught. Sorting
#: alphabetically instead would put "other" second, and a reader comparing the note to the bucket
#: list above would have to re-map it. A protocol NOT listed here (the engine gains one) is not
#: dropped, it is appended alphabetically, so the breakdown stays total without this tuple having
#: to know the future.
_NON_CHANNEL_PROTOCOL_ORDER = ("loc", "sim", "sys", "other")

#: A trailing EPICS record-field suffix: one or more ``.FIELD`` segments at the end of a PV name
#: (``record.EGU``, ``record.OUT``, ``record.FIELD.SUB``). ESS record names carry no dot (their
#: segments use ``:`` ``-`` ``_``), so a trailing dot-prefixed field-shaped token is always a field.
_FIELD_SUFFIX_RE = re.compile(r"(?:\.[A-Za-z][A-Za-z0-9_]*)+$")

#: When cf_unregistered is at least this fraction of the linked set, surface the ratio caveat: such
#: a count almost certainly means an INCOMPLETE ChannelFinder/IOC (e.g. a partial test registry),
#: not real defects. A fixed constant keeps the verdict deterministic (no runtime judgement).
_CF_RATIO_CAVEAT_THRESHOLD = 0.5


def _record_name(pv: str) -> str:
    """Strip a trailing record-field suffix (``.EGU``/``.OUT``/``.FIELD.SUB``) to the record name.

    ChannelFinder/recsync and the IOC ``.db`` register the **bare record name**, never
    ``record.FIELD``; display PVs routinely reference a field (e.g. ``...Delay-SP.EGU``).
    Normalizing both sides to the record name before a set-diff avoids guaranteed false positives.
    Idempotent on a bare record name (no dot → unchanged); non-channel forms never reach here
    (bucketed out by protocol first).
    """
    return _FIELD_SUFFIX_RE.sub("", pv)


def _non_channel_breakdown(by_protocol: dict[str, set[str]]) -> tuple[tuple[str, int], ...]:
    """Split the non-channel set by protocol as a PARTITION: the counts sum to the distinct total.

    Each PV is counted under exactly ONE protocol, the first of :data:`_NON_CHANNEL_PROTOCOL_ORDER`
    it appears under (unlisted protocols follow alphabetically). That rule exists because the same
    expanded string CAN arrive under two protocols: a value still carrying a macro prefix keeps the
    occurrence's protocol instead of the one its own text says (``opi_navigation`` expansion), so
    counting each protocol's set independently could sum to MORE than the headline number. Two
    numbers for one category inside one report is the second truth this project forbids, and the
    breakdown is the half that must yield, because the headline is what every other note counts.

    The assignment is decided by this fixed order rather than by input order, so it is deterministic
    for any iteration order of *by_protocol* or of the rows that filled it. Protocols left with no
    PV of their own are omitted, an empty bucket is not information.
    """
    ranked = sorted(
        by_protocol,
        key=lambda protocol: (
            _NON_CHANNEL_PROTOCOL_ORDER.index(protocol)
            if protocol in _NON_CHANNEL_PROTOCOL_ORDER
            else len(_NON_CHANNEL_PROTOCOL_ORDER),
            protocol,
        ),
    )
    claimed: set[str] = set()
    counted: list[tuple[str, int]] = []
    for protocol in ranked:
        own = by_protocol[protocol] - claimed
        claimed |= own
        if own:
            counted.append((protocol, len(own)))
    return tuple(counted)


def _format_breakdown(counted: tuple[tuple[str, int], ...]) -> str:
    """Render a breakdown as ``loc: 25154, sim: 36`` for the prose surfaces."""
    return ", ".join(f"{protocol}: {count}" for protocol, count in counted)


class JoinPv(NamedTuple):
    """One macro-expanded, operator-facing display-PV instance fed into the join.

    The narrow seam the join needs from the ``opi_navigation`` PV-inventory: the tool/CLI edge
    translates each ``ExpandedPv`` of an **operator-facing** display into one of these (embed-only
    fragment standalone seeds are filtered out at the edge, so they never reach the join). The
    field literals match ``ExpandedPv.{resolution,role,protocol}`` and
    ``DisplayPvInventory.node_kind`` verbatim, which is how this module stays free of
    ``opi_navigation`` imports while still speaking its vocabulary.

    ⚠ ``display`` is not necessarily a screen, and GQ-153 is the record of that reading a caller
    astray one level up. The edge cuts on ``operator_facing`` and never on the kind of file, so a
    ``.plt`` Data Browser trend opened by a button is a top level in its own right and arrives
    here. ``node_kind`` is what says which it is.

    ``node_kind`` carries NO default on purpose, unlike the wire models' additive kind fields. A
    default would make "kind not stated" indistinguishable from "kind is screen", and every row
    built without thinking about it would silently enlarge the screen count, which is the exact
    defect this field was added to remove. There are four construction sites, one in ``src`` and
    three in the tests; naming the kind at each of them costs one line and cannot go quietly wrong.
    """

    display: str
    pv: str
    resolution: Literal["resolved", "dynamic", "unresolved"]
    role: Literal["read", "write"]
    protocol: Literal["ca", "pva", "loc", "sim", "sys", "other"]
    node_kind: Literal["display", "trend"]


class NamingChecker(Protocol):
    """Minimal read-only Naming-Service contract (so tests can inject a fake)."""

    def validate_name(self, ess_name: str) -> NameStatus: ...


class CFRegistryCapped(RuntimeError):
    """A :class:`ChannelFinderChecker` query was truncated (hit the result cap).

    cf_unregistered is then WITHHELD, diffing against a partial registry would false-flag real
    channels. A distinct ``RuntimeError`` subclass so the core can set ``cf_capped`` apart from a
    generic query failure (also surfaced as a withheld verdict).
    """


class ChannelFinderChecker(Protocol):
    """Minimal read-only ChannelFinder contract (so tests can inject a fake).

    ``registered_under(prefix)`` returns the set of channel NAMES registered under the IOC device
    prefix, OR raises :class:`RuntimeError` (``CFRegistryCapped`` on a truncated result), never
    another type. That narrow contract lets the core's ``except RuntimeError`` degrade to a withheld
    verdict without swallowing unrelated bugs (the edge translates ChannelFinder/network errors into
    ``RuntimeError`` before they reach here).
    """

    def registered_under(self, prefix: str) -> set[str]: ...


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
    #: Operator-facing FILES with ≥1 concrete PV sharing the IOC prefix. ⚠ Not screens only: a
    #: ``.plt`` Data Browser trend opened by a button is operator-facing and belongs here, which
    #: is why ``screens_linked`` and ``trends_linked`` split it below. The field keeps its name and
    #: stays the whole union, so no caller breaks to be told what the two new tuples now say
    #: (the same choice GQ-21 made for ``find_device.screens``).
    displays_linked: tuple[str, ...] = ()
    #: The operator SCREENS among ``displays_linked``. Counted positively, never as
    #: ``displays_linked`` minus the trends: a third ``NodeKind`` would then land in the screen
    #: list unannounced, while positive projection makes it fall out of both and show up in the
    #: union check ``tests/test_crossplane.py`` holds over these three.
    screens_linked: tuple[str, ...] = ()
    #: The Data Browser TRENDS among ``displays_linked``, positively counted for the same reason.
    trends_linked: tuple[str, ...] = ()
    #: Distinct concrete display PVs sharing the IOC prefix.
    pvs_linked: tuple[str, ...] = ()
    #: Writable subset of ``pvs_linked`` (≥1 operator display writes the channel), owner triage.
    pvs_linked_write: tuple[str, ...] = ()
    #: Concrete display PVs that do NOT share this IOC's prefix (likely other IOCs).
    pvs_other_prefix: tuple[str, ...] = ()
    #: Distinct display PVs the inventory could NOT resolve to a concrete channel:
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
    #: The same set split by protocol, ``((protocol, count), ...)``, reported in the
    #: loc/sim/sys/other order every text here uses. A PARTITION: the counts sum to exactly
    #: ``len(pvs_non_channel)``, so the headline number and its breakdown can never disagree.
    #: A pair-tuple rather than a dict because everything else on this model is a tuple and the
    #: order is part of the promise; a dict would carry it only by convention.
    pvs_non_channel_by_protocol: tuple[tuple[str, int], ...] = ()
    #: Operator-facing displays whose per-instance PVs are incomplete (per-display context cap):
    #: their linked/other counts are a LOWER BOUND.
    displays_incomplete: tuple[str, ...] = ()
    #: IOC .db PV counts (only when a .db set was supplied; module repos deferred).
    ioc_db_resolved: int = 0
    ioc_db_needs_msi: int = 0
    #: Concrete linked display PVs absent from the IOC .db (only when a PROVABLY COMPLETE + fully
    #: resolved .db set is supplied; withheld otherwise, never false-flagged).
    broken: tuple[str, ...] = ()
    #: Writable subset of ``broken`` (a dead command/setpoint target, owner triage).
    broken_write: tuple[str, ...] = ()
    #: Concrete linked PVs (record names, field suffixes normalized away) that resolve to the IOC
    #: prefix but are NOT registered in ChannelFinder, unregistered/runtime-missing candidates.
    #: A SEPARATE plane from ``broken`` (CF runtime registry vs. static .db). Only populated when a
    #: ``ChannelFinderChecker`` is supplied AND the query succeeded (else empty + a withhold note).
    cf_unregistered: tuple[str, ...] = ()
    #: Writable subset of ``cf_unregistered`` (an operator can command an unregistered channel).
    cf_unregistered_write: tuple[str, ...] = ()
    #: Count of channels ChannelFinder registered under the IOC prefix. Only meaningful WITHOUT a
    #: cf withhold/cap note (0 both when nothing is registered and when the check was withheld).
    cf_registered: int = 0
    #: True when the ChannelFinder query hit the result cap → cf_unregistered withheld (truncated
    #: registry would false-flag). Meaning is carried by the note, not by an empty cf_unregistered.
    cf_capped: bool = False
    #: Honest caveats about coverage limits.
    notes: tuple[str, ...] = ()


def crossplane_check(
    join_pvs: Iterable[JoinPv],
    st_cmd: StCmdInfo,
    *,
    naming: NamingChecker | None = None,
    ioc_db: tuple[set[str], set[str]] | None = None,
    ioc_db_complete: bool = False,
    channelfinder: ChannelFinderChecker | None = None,
    cf_requested: bool = False,
    context_capped: tuple[str, ...] = (),
    glob_capped_count: int = 0,
) -> CrossPlaneReport:
    """Join macro-expanded display PVs with an IOC's st.cmd (+ optional .db) and the Naming Service.

    *join_pvs* are the per-instance, operator-facing display-PV rows (from the ``opi_navigation``
    PV-inventory, translated at the tool/CLI edge). *naming* (optional) is queried for the IOC
    device name; ``None`` skips it. *ioc_db* (optional) is ``(resolved, unresolved)`` from
    :func:`e3_db.ioc_db_pvs`. A ``broken`` verdict (concrete linked PVs missing from *resolved*) is
    emitted ONLY when *ioc_db_complete* is True AND *unresolved* is empty, i.e. the supplied IOC
    .db set is **provably complete and fully resolved**; otherwise the verdict is withheld (absence
    cannot be proven against a partial/templated set). *channelfinder* (optional) is queried for the
    channels registered under the IOC prefix; concrete linked PVs (record names) absent from it are
    reported as *cf_unregistered* (a SEPARATE plane from broken, runtime registry vs. static .db).
    A truncated/failed query withholds cf_unregistered rather than false-flagging. *cf_requested* is
    True whenever the caller asked for the ChannelFinder check, so an honest "skipped, URL unset"
    note can be emitted when no checker is wired. *context_capped* / *glob_capped_count* carry the
    inventory's honest incompleteness signals (linked/other become lower bounds).
    """
    prefix = st_cmd.prefix
    linked_displays: set[str] = set()
    # The same files kept per kind, filled in the SAME pass (``jp.node_kind`` is a field of the row
    # being bucketed), so the split can never describe a different population than the union above
    # it. A file of an unknown kind lands in NEITHER and surfaces in the note below rather than
    # being folded into the screens.
    linked_by_kind: dict[str, set[str]] = {}
    linked_pvs: set[str] = set()
    linked_write: set[str] = set()
    other_prefix_pvs: set[str] = set()
    dynamic_pvs: set[str] = set()
    unresolved_pvs: set[str] = set()
    non_channel_pvs: set[str] = set()
    # The same references kept per protocol, filled in the SAME pass: ``jp.protocol`` is a field of
    # the row being bucketed, so the breakdown costs no second walk and cannot describe a different
    # population than ``non_channel_pvs``. Turned into a partition by _non_channel_breakdown.
    non_channel_by_protocol: dict[str, set[str]] = {}
    # Distinct (display, pv) pairs for the honest residue, robust against the same PV appearing
    # under multiple roles/origins within one display (the inventory dedups per display, but a PV
    # can recur with a different role/origin_file); counts references, not raw rows.
    indeterminate_pairs: set[tuple[str, str]] = set()

    for jp in join_pvs:
        if jp.protocol not in _REAL_PROTOCOLS:
            non_channel_pvs.add(jp.pv)
            non_channel_by_protocol.setdefault(jp.protocol, set()).add(jp.pv)
            continue
        if jp.resolution == "resolved":
            if prefix and jp.pv.startswith(prefix):
                linked_pvs.add(jp.pv)
                linked_displays.add(jp.display)
                linked_by_kind.setdefault(jp.node_kind, set()).add(jp.display)
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

    # Normalize linked PVs to the bare record name (strip a trailing .FIELD): ChannelFinder and the
    # IOC .db register record names, never ``record.FIELD``, so BOTH the cf_unregistered and the
    # broken set-diffs must compare in record-name space (else ``...Delay-SP.EGU`` is a guaranteed
    # false miss). Working in record-name space also dedups ``record`` vs. ``record.EGU``.
    linked_records = {_record_name(pv) for pv in linked_pvs}
    linked_write_records = {_record_name(pv) for pv in linked_write}

    naming_result: NamingResult | None = None
    if naming is not None and st_cmd.device_name:
        try:
            status = naming.validate_name(st_cmd.device_name)
        except NamingServiceResponseError as exc:
            # A non-404 naming service/URL failure: WITHHOLD the naming verdict (leave it None)
            # instead of aborting the whole cross-plane report, best-effort provenance, never a
            # false verdict. A genuine 404 is already mapped to registered=False inside
            # validate_name and does NOT reach here (DS-2 / audit S5).
            logger.debug("Naming lookup withheld for %s: %s", st_cmd.device_name, exc)
        else:
            naming_result = NamingResult(
                registered=status["registered"],
                status=status["status"],
                message=status["message"],
            )

    broken: set[str] = set()
    broken_withheld = False
    db_resolved = db_needs_msi = 0
    if ioc_db is not None:
        resolved, unresolved = ioc_db
        db_resolved, db_needs_msi = len(resolved), len(unresolved)
        # A linked PV's ABSENCE from the IOC .db can only be proven when that .db set is provably
        # complete (every load mechanism captured) AND fully resolved (no needs-msi residue);
        # otherwise the record may exist under a still-templated name. Withhold rather than
        # false-flag, the verdict's whole value is that it never lies.
        if ioc_db_complete and resolved and not unresolved:
            broken = {r for r in linked_records if r not in resolved}
        else:
            # Withhold over an empty/partial/templated resolved set, proving a linked PV's absence
            # against ZERO known PVs would flag every linked PV (defense-in-depth with the loader's
            # ``bool(resolved)`` completeness term).
            broken_withheld = True
    broken_write = broken & linked_write_records

    # ChannelFinder plane (runtime PV directory), SEPARATE from the static .db ``broken`` above.
    # A concrete linked record absent from the channels registered under the IOC prefix is an
    # unregistered/runtime-missing candidate. Withhold (never false-flag) on a truncated/failed
    # registry: the verdict's value is that it never lies. Scoped to the record-name-normalized
    # linked set so a field-suffixed display PV (``...SP.EGU``) is not a guaranteed false positive.
    cf_unregistered: set[str] = set()
    cf_unregistered_write: set[str] = set()
    cf_registered = 0
    cf_capped = False
    cf_withheld = False
    if channelfinder is not None and prefix:
        try:
            registered = channelfinder.registered_under(prefix)
            cf_unregistered = {r for r in linked_records if r not in registered}
            cf_unregistered_write = cf_unregistered & linked_write_records
            cf_registered = len(registered)
        except CFRegistryCapped:
            cf_withheld = cf_capped = True
        except RuntimeError:
            cf_withheld = True

    # GQ-153: the kind split, positively projected. A kind this server does not know about (a
    # future third ``NodeKind``) is deliberately left out of BOTH tuples and named in a note, so a
    # reader sees a file missing from the split instead of finding it quietly counted as a screen.
    screens_linked = linked_by_kind.get("display", set())
    trends_linked = linked_by_kind.get("trend", set())
    unknown_kinds = sorted(kind for kind in linked_by_kind if kind not in ("display", "trend"))

    notes: list[str] = []
    if unknown_kinds:
        notes.append(
            f"{len(unknown_kinds)} linked file kind(s) this server does not know "
            f"({', '.join(unknown_kinds)}) are counted in displays_linked but in NEITHER "
            "screens_linked nor trends_linked; the display engine has grown a node kind this "
            "report has not been taught to name."
        )
    if not prefix:  # None or "", both mean "no usable IOC prefix" (join sends all to other-prefix)
        notes.append(
            "No IOC device prefix parsed from st.cmd, every concrete PV is reported as "
            "other-prefix (no provenance link possible)."
        )
    if indeterminate_pvs:
        notes.append(
            f"{len(indeterminate_pvs)} distinct display PV(s) ({len(indeterminate_pairs)} "
            "reference(s)) could not be resolved to a concrete channel (dynamic/unresolved), "
            "honest residue, never judged here."
        )
    non_channel_counted = _non_channel_breakdown(non_channel_by_protocol)
    if non_channel_pvs:
        # The breakdown replaces the bare protocol list this note used to carry: naming the four
        # possible protocols told a reader nothing about the set in front of them, and the numbers
        # were already in hand (same pass, same rows). Only protocols that actually occur appear.
        notes.append(
            f"{len(non_channel_pvs)} distinct non-channel reference(s) "
            f"({_format_breakdown(non_channel_counted)}) excluded from the IOC join, "
            "not real EPICS channels."
        )
    if context_capped:
        # "per-display", not "per-instance", and the distinction is this module's own to get
        # wrong: "per-instance" is what THIS file calls the inventory (see the module docstring
        # and ``displays_incomplete``), while the cap counts reachability contexts PER FILE
        # (``opi_navigation`` expansion, budget per (target, top)). Saying "per-instance context
        # cap" names the thing the cap shortens instead of the cap, and it made this the one
        # tool of the four whose note disagreed with its own ``context_cap`` argument
        # description. One wording across all four, pinned in tests/test_diagnostics_tail.py.
        notes.append(
            f"{len(context_capped)} display(s) hit the per-display context cap, "
            "their resolved PVs are a LOWER BOUND; 'linked'/'other-prefix' may undercount "
            "(re-run with a higher context cap)."
        )
    if glob_capped_count:
        notes.append(
            f"{glob_capped_count} globbed <file> reference(s) hit the glob cap, some embedded "
            "targets were left out of the expansion; coverage is a lower bound."
        )
    if not linked_pvs and not other_prefix_pvs and indeterminate_pvs:
        notes.append(
            "Almost no concrete PVs resolved, the displays directory may be too narrow "
            "(macros are bound by operator top-levels; pass the project/dataset ROOT)."
        )
    if ioc_db is None:
        notes.append(
            "No IOC .db PV set supplied (e3 module repos deferred, EM-C-modules): "
            "provenance is at device-prefix + Naming level only; no 'broken' verdict."
        )
    if broken_withheld:
        notes.append(
            "Broken verdict withheld, the IOC .db PV set is not provably complete and fully "
            "resolved (incomplete load capture or records still macro-templated): a linked PV's "
            "absence cannot be proven (it may exist under a name not yet expanded). A single "
            "needs-msi record withholds ALL broken verdicts for this IOC (all-or-nothing)."
        )
    if db_needs_msi:
        notes.append(
            f"{db_needs_msi} IOC record(s) still macro-templated after substitution "
            "(needs msi / .substitutions expansion, Linux/Docker)."
        )
    if cf_requested and channelfinder is None:
        # Honest no-op: the caller asked for the ChannelFinder check but no URL is configured, so no
        # checker was built. Surface it in the report (no silent skip), see tools/channelfinder.
        notes.append(
            "ChannelFinder check requested (query_channelfinder=True) but "
            "EPICS_MCP_CHANNELFINDER_URL is unset, cf_unregistered skipped (no network call)."
        )
    if cf_capped:
        notes.append(
            "cf_unregistered withheld: ChannelFinder returned a capped (truncated) result for the "
            "IOC prefix; diffing against a partial registry would false-flag real channels."
        )
    elif cf_withheld:
        notes.append(
            "cf_unregistered withheld, the ChannelFinder query failed; a linked PV cannot be "
            "proven unregistered against an unavailable registry."
        )
    if cf_unregistered and context_capped:
        # The SECOND place this one service names the cap, and it used to name it differently
        # from the note above ("inventory context cap" against "per-instance context cap"), so
        # the wording diverged inside a single tool and not just between the four. Invisible to
        # a line-wise grep, because the phrase straddles the line break: found by the AST guard.
        notes.append(
            "cf_unregistered is a LOWER BOUND, some operator displays hit the per-display "
            "context cap, so the linked set (and thus cf_unregistered) may undercount."
        )
    if (
        cf_unregistered
        and linked_records
        and len(cf_unregistered) / len(linked_records) >= _CF_RATIO_CAVEAT_THRESHOLD
    ):
        notes.append(
            f"{len(cf_unregistered)}/{len(linked_records)} linked PV(s) are unregistered in "
            "ChannelFinder (>= 50%), this almost certainly means an INCOMPLETE ChannelFinder/IOC "
            "(e.g. a partial test registry), not real defects; treat the count as a coverage "
            "signal, not a defect list."
        )

    return CrossPlaneReport(
        ioc_prefix=prefix,
        ioc_device_name=st_cmd.device_name,
        naming=naming_result,
        displays_linked=tuple(sorted(linked_displays)),
        screens_linked=tuple(sorted(screens_linked)),
        trends_linked=tuple(sorted(trends_linked)),
        pvs_linked=tuple(sorted(linked_pvs)),
        pvs_linked_write=tuple(sorted(linked_write)),
        pvs_other_prefix=tuple(sorted(other_prefix_pvs)),
        pvs_indeterminate=tuple(sorted(indeterminate_pvs)),
        pvs_indeterminate_occurrences=len(indeterminate_pairs),
        pvs_dynamic=tuple(sorted(dynamic_pvs)),
        pvs_unresolved=tuple(sorted(unresolved_pvs)),
        pvs_non_channel=tuple(sorted(non_channel_pvs)),
        pvs_non_channel_by_protocol=non_channel_counted,
        displays_incomplete=tuple(sorted(context_capped)),
        ioc_db_resolved=db_resolved,
        ioc_db_needs_msi=db_needs_msi,
        broken=tuple(sorted(broken)),
        broken_write=tuple(sorted(broken_write)),
        cf_unregistered=tuple(sorted(cf_unregistered)),
        cf_unregistered_write=tuple(sorted(cf_unregistered_write)),
        cf_registered=cf_registered,
        cf_capped=cf_capped,
        notes=tuple(notes),
    )


def render_markdown(report: CrossPlaneReport) -> str:
    """Render a :class:`CrossPlaneReport` as deterministic Markdown."""
    lines = ["# Cross-Plane PV Provenance", ""]
    lines.append(f"- **IOC prefix:** `{report.ioc_prefix or ', '}`")
    lines.append(f"- **IOC device name:** `{report.ioc_device_name or ', '}`")
    if report.naming is None:
        lines.append("- **Naming Service:** not checked (offline)")
    else:
        status = report.naming.status or "not found"
        flag = "✅ ACTIVE" if report.naming.registered else f"⚠️ {status}"
        lines.append(f"- **Naming Service:** {flag}, {report.naming.message}")
    lines.append("")
    # GQ-153: the split belongs in the HEADER, not only in the JSON fields, for the reason
    # find_device's renderer states one file over: this is the half a person reads and the line
    # they draw a conclusion from. It said "Displays linked to this IOC" and counted a Data
    # Browser trend among them. The per-file marker below uses the wording find_device already
    # ships, so the two tools name the same thing the same way.
    lines.append(
        f"- **Operator-facing files linked to this IOC:** {len(report.displays_linked)} "
        f"({len(report.screens_linked)} screen(s), "
        f"{len(report.trends_linked)} Data Browser trend(s))"
    )
    trends = set(report.trends_linked)
    lines.extend(
        f"  - {display}{_TREND_MARKER if display in trends else ''}"
        for display in report.displays_linked
    )
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
            f"- **Non-channel refs (excluded):** {len(report.pvs_non_channel)} "
            f"({_format_breakdown(report.pvs_non_channel_by_protocol)})"
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
        if report.broken_write:
            lines.append(f"  - of which writable (dead command target): {len(report.broken_write)}")
        lines.extend(f"  - {pv}" for pv in report.broken)
    if report.cf_unregistered:
        lines.append(
            f"- **Unregistered in ChannelFinder (linked PV, not in CF):** "
            f"{len(report.cf_unregistered)} of {report.cf_registered} registered under the prefix"
        )
        if report.cf_unregistered_write:
            lines.append(f"  - of which writable: {len(report.cf_unregistered_write)}")
        lines.extend(f"  - {pv}" for pv in report.cf_unregistered)
    if report.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)
