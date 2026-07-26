"""One join-orchestration function per display-aware vertical (M2/C2-iii).

Each cross-plane vertical (crossplane provenance, coverage audit) gets exactly ONE orchestration
function here, the same shape the diagnose vertical already had (``services.diagnose.diagnose``).
The MCP tool wrapper AND the CLI both call it, so the up-to-10-argument lockstep that used to be
copied line-for-line between ``cli_crossplane.main`` and ``tools/crossplane._run_check`` (and the
coverage pair) is gone, drift can no longer make the CLI serve a different report than the tool.

**Why here and not in the pure cores** (:mod:`~.crossplane`, :mod:`~.coverage`): the orchestration
pulls the macro-aware PV inventory (``analyze_display_pvs`` / ``analyze_display_index`` →
``opi_navigation`` via :mod:`~.inventory_adapter`), so it is part of the optional ``[displays]``
surface. The pure cores stay ``opi_navigation``-free (offline-testable) and receive
already-translated rows. This module is imported ONLY by the two display tools and the two CLIs,
never by the core server, a standalone install without ``opi_navigation`` never reaches it.

**Path validation lives here** (``resolve_user_path``, canonicalize + existence + opt-in
``allowed_roots`` boundary), so the CLI gets the SAME boundary the MCP tool already had. That closes
the CLI path-validation divergence (S4-4): the CLI no longer does a bare ``Path.is_dir()`` that
ignored the boundary and skipped canonicalization.
"""

from __future__ import annotations

from dataclasses import dataclass

from epics_pv_mcp.paths import resolve_user_path
from epics_pv_mcp.services.checkers import (
    build_alarm_checker,
    build_archiver_checker,
    build_cf_checker,
    build_naming_client,
)
from epics_pv_mcp.services.coverage import CoverageReport, audit_coverage
from epics_pv_mcp.services.crossplane import CrossPlaneReport, crossplane_check
from epics_pv_mcp.services.e3_db import load_ioc_db, parse_st_cmd
from epics_pv_mcp.services.inventory_adapter import (
    DEFAULT_PV_CONTEXT_CAP,
    analyze_display_index,
    analyze_display_pvs,
)


@dataclass(frozen=True)
class CrossPlaneRequest:
    """Inputs to :func:`run_crossplane`, bundles the knobs the tool and CLI passed in lockstep.

    ``displays_dir`` is the project/dataset ROOT (macros bind via the operator top-levels there).
    ``module_db_root`` empty = offline (no ``broken`` verdict). Frozen for determinism.
    """

    displays_dir: str
    st_cmd_path: str
    query_naming: bool = False
    query_channelfinder: bool = False
    context_cap: int = DEFAULT_PV_CONTEXT_CAP
    windows_paths: bool = False
    module_db_root: str = ""


def run_crossplane(request: CrossPlaneRequest) -> CrossPlaneReport:
    """Cross-plane PV provenance join (Display ↔ e3 IOC ↔ Naming/CF). Blocking, deterministic.

    Validates every user path (canonicalize + existence + ``allowed_roots``), then does the blocking
    work: the macro-aware PV inventory over the project ROOT, the ``st.cmd`` parse, the optional IOC
    ``.db`` load (``module_db_root``), the optional Naming/ChannelFinder checker construction, and
    the pure-core join. Returns the typed report; the tool serializes it, the CLI renders it.

    Blocking file I/O + optional HTTP GETs → the async tool offloads it to a thread while the CLI
    calls it directly. Raises :class:`EpicsError` (``INVALID_INPUT``) via ``resolve_user_path`` on a
    missing/invalid path.
    """
    # S5-7: use the CANONICALIZED paths resolve_user_path returns (not the raw request strings), so
    # the path that is boundary-checked is exactly the one walked/read.
    displays_dir = resolve_user_path(request.displays_dir, kind="dir", label="displays_dir")
    st_cmd_path = resolve_user_path(request.st_cmd_path, kind="file", label="st_cmd_path")
    module_db_root = (
        resolve_user_path(request.module_db_root, kind="dir", label="module_db_root")
        if request.module_db_root
        else None
    )

    join_pvs, context_capped, glob_capped_count = analyze_display_pvs(
        displays_dir,
        context_cap=request.context_cap,
        windows_paths=request.windows_paths,
    )
    st_info = parse_st_cmd(st_cmd_path.read_text(encoding="utf-8"))
    naming = build_naming_client(request.query_naming)
    # Opt-in IOC .db enumeration: only when a module/db root is given (offline default unchanged).
    # ``complete`` gates the broken verdict, a partial/templated set withholds it (no false alarm).
    ioc_db: tuple[set[str], set[str]] | None = None
    ioc_db_complete = False
    if module_db_root is not None:
        db_result = load_ioc_db(st_info, module_db_root)
        ioc_db = (set(db_result.resolved), set(db_result.unresolved))
        ioc_db_complete = db_result.complete
    channelfinder = build_cf_checker(request.query_channelfinder)
    return crossplane_check(
        join_pvs,
        st_info,
        naming=naming,
        ioc_db=ioc_db,
        ioc_db_complete=ioc_db_complete,
        channelfinder=channelfinder,
        cf_requested=request.query_channelfinder,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )


@dataclass(frozen=True)
class CoverageRequest:
    """Inputs to :func:`build_coverage_report`, the knobs the tool and CLI passed in lockstep.

    ``displays_dir`` is the project/dataset ROOT. ``scope`` narrows both the ChannelFinder query and
    the display set; ``""`` audits the whole site (CF cap risk, small-scope only). Frozen.
    """

    displays_dir: str
    scope: str = ""
    query_channelfinder: bool = False
    query_archiver: bool = False
    query_alarm: bool = False
    alarm_config: str | None = None
    context_cap: int = DEFAULT_PV_CONTEXT_CAP
    windows_paths: bool = False


def build_coverage_report(request: CoverageRequest) -> CoverageReport:
    """Cross-plane coverage audit (Display ↔ CF ↔ Archiver ↔ Alarm). Blocking, deterministic.

    Validates ``displays_dir`` (``resolve_user_path``), builds the macro-aware display-PV index over
    the project ROOT, constructs the requested runtime checkers (each config-gated on its
    ``*_URL``), and calls the pure core. Returns the typed report; tool serializes, CLI renders.

    Blocking I/O → the async tool offloads to a thread while the CLI calls it directly. Raises
    :class:`EpicsError` (``INVALID_INPUT``) on a missing ``displays_dir``.
    """
    # S5-7: walk the canonicalized path resolve_user_path returns, not the raw request string.
    displays_dir = resolve_user_path(request.displays_dir, kind="dir", label="displays_dir")
    index_rows, context_capped, glob_capped_count = analyze_display_index(
        displays_dir,
        context_cap=request.context_cap,
        windows_paths=request.windows_paths,
    )
    channelfinder = build_cf_checker(request.query_channelfinder)
    archived = build_archiver_checker(request.query_archiver)
    alarmed = build_alarm_checker(request.query_alarm, request.alarm_config)
    return audit_coverage(
        index_rows,
        scope=request.scope,
        channelfinder=channelfinder,
        cf_requested=request.query_channelfinder,
        archived=archived,
        archive_requested=request.query_archiver,
        alarmed=alarmed,
        alarm_requested=request.query_alarm,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
