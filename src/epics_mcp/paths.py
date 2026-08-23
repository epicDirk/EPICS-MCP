"""Workspace path boundary for user-supplied file/directory arguments.

Two layers, in order:

1. **Always-on canonicalization + existence/kind check** (the real, immediate
   value). The path is resolved (symlinks and ``..`` collapsed) and then verified
   to be a directory or a file, raising a clear ``EpicsError(INVALID_INPUT)`` that
   *names the offending argument* so an agent learns which path was bad.

2. **Opt-in ``allowed_roots`` boundary** (off by default). When the env var
   ``EPICS_MCP_ALLOWED_ROOTS`` is set, the resolved path must live under one of
   those roots, else ``EpicsError(PATH_OUTSIDE_WORKSPACE)``. **Default empty = NO
   boundary**, this is future-posture optionality, NOT a "secured" deployment.
   It stays dormant because the caller is trusted, and for no other reason: do
   NOT justify it with "the server is read-only and localhost-isolated". Neither
   half is unconditional any more, the server has a gated write surface (the
   Olog logbook), and its READ reach depends on the launcher, which can widen the
   EPICS address lists onto a real facility network (PV write is the exception:
   enabling it forces loopback-only and refuses to start otherwise). A deployment
   that opens either should consider enabling this boundary deliberately. The
   separator is OS-dependent (``os.pathsep``, ``;`` on Windows, ``:`` on Linux),
   so an ``EPICS_MCP_ALLOWED_ROOTS`` value is not 1:1 portable between the two.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Literal

from epics_mcp.config import get_config
from epics_mcp.errors import EpicsError


def path_boundary_configured(raw: str) -> bool:
    """True iff an ``EPICS_MCP_ALLOWED_ROOTS`` string holds at least one non-blank root.

    The one rule that decides whether the opt-in boundary exists at all, written once because two
    spellings that look equivalent are not: ``bool(raw)`` is true for ``";"`` and for ``"   "``,
    both of which :func:`_resolve_roots` drops to nothing. A caller built on the naive spelling
    would report a boundary that no file argument is actually held to, which is the direction that
    matters. :func:`_allowed_roots` asks it below, and so does ``epics://health``.

    It answers WITHOUT touching the filesystem, unlike ``_allowed_roots``: ``Path.resolve()`` is a
    stat, and the health resource is a synchronous handler where a stat on a dead network root
    would block. And it takes the STRING rather than reading the config itself, so a caller pointed
    at one configuration cannot be answered from another (the resource tests rebind ``get_config``
    in their own module, which a ``get_config()`` in here would bypass).

    ⚠️ True does not mean NARROW. A root of ``"."``, or a volume root, satisfies it and constrains
    almost nothing; how WIDE a configured boundary is, is a different question this does not ask.
    """
    return any(part.strip() for part in raw.split(os.pathsep))


@functools.lru_cache(maxsize=8)
def _resolve_roots(raw: str) -> tuple[Path, ...]:
    """Resolve an ``EPICS_MCP_ALLOWED_ROOTS`` string into roots, cached on the raw string (S2-7).

    ``Path.resolve()`` is a filesystem stat (symlink resolution); the config is an immutable
    singleton, so the roots never change within its lifetime and re-resolving them on every
    ``resolve_user_path`` call is wasted work. Keyed on the raw string, so a config reset or a
    different value recomputes correctly. Returns a tuple (immutable → safe to cache).
    """
    return tuple(Path(part).resolve() for part in raw.split(os.pathsep) if part.strip())


def _allowed_roots() -> list[Path]:
    """Resolve ``EPICS_MCP_ALLOWED_ROOTS`` into roots; empty/unset = no boundary.

    Guards the empty-string trap: ``"".split(os.pathsep)`` yields ``[""]`` whose
    ``Path("")`` would resolve to the *current working directory* and silently
    become an allowed root. An unset/blank value must mean "no boundary", and
    that decision is :func:`path_boundary_configured`, shared with the health
    resource so the two can never disagree about what "configured" means.
    """
    raw = get_config().allowed_roots
    if not path_boundary_configured(raw):
        return []
    return list(_resolve_roots(raw))


def resolve_user_path(raw: str, *, kind: Literal["dir", "file"], label: str) -> Path:
    """Canonicalize *raw*, verify it is a *kind*, and enforce the opt-in boundary.

    *label* names the argument in error messages (e.g. ``"displays_dir"``) so the
    caller learns which path was rejected.

    Raises:
        EpicsError(INVALID_INPUT): the path is empty/blank, does not exist, or is
            the wrong kind.
        EpicsError(PATH_OUTSIDE_WORKSPACE): an ``allowed_roots`` boundary is
            configured and *raw* resolves outside every root.
    """
    # Reject empty/blank BEFORE resolve(): Path("")/Path("   ").resolve() collapse to
    # the process CWD (and is_dir() is then True), which would silently walk the
    # server's working directory instead of raising.
    if not raw.strip():
        raise EpicsError(f"{label} must not be empty", error_code="INVALID_INPUT")
    resolved = Path(raw).resolve()
    noun = "directory" if kind == "dir" else "file"
    exists_as_kind = resolved.is_dir() if kind == "dir" else resolved.is_file()
    if not exists_as_kind:
        raise EpicsError(f"{label} is not a {noun}: {raw}", error_code="INVALID_INPUT")

    roots = _allowed_roots()
    # is_relative_to folds case on Windows (WindowsPath flavour), do NOT swap it
    # for startswith/commonpath, which would lose that folding.
    if roots and not any(resolved.is_relative_to(root) for root in roots):
        raise EpicsError(
            f"{label} is outside the allowed roots (EPICS_MCP_ALLOWED_ROOTS): {raw}",
            error_code="PATH_OUTSIDE_WORKSPACE",
        )
    return resolved


def resolve_new_file_path(raw: str, *, label: str) -> Path:
    """Canonicalize a NOT-yet-existing output-file path, enforcing the boundary via its PARENT.

    :func:`resolve_user_path` with ``kind="file"`` stat-checks ``is_file`` and therefore REJECTS a
    path that does not exist yet, which is exactly a download TARGET the caller is about to create.
    This resolves the PARENT directory (which must exist) through :func:`resolve_user_path`, so the
    opt-in ``EPICS_MCP_ALLOWED_ROOTS`` boundary is enforced identically, then rejoins the basename.
    The basename is taken via ``Path(raw).name`` (a single component, never a separator) and
    rejected
    if it is ``""`` / ``"."`` / ``".."``, so the result cannot traverse out of the validated parent.
    An EXISTING SYMLINK at the target is rejected too (``is_symlink`` = ``lstat``, does not
    follow), so
    a symlink cannot redirect the write OUT of the validated parent, this is the cross-platform
    guard
    the caller's ``O_EXCL`` open cannot give alone (on Windows ``O_EXCL`` follows a DANGLING
    symlink and
    creates its target). Mirrors the parent-dir fallback pattern in
    :mod:`epics_mcp.tools.validate`.

    Raises:
        EpicsError(INVALID_INPUT): empty/blank *raw*, a basename that is not a plain filename, a
            non-existent parent directory, or an existing symlink at the target.
        EpicsError(PATH_OUTSIDE_WORKSPACE): the parent resolves outside every allowed root.
    """
    if not raw.strip():
        raise EpicsError(f"{label} must not be empty", error_code="INVALID_INPUT")
    candidate = Path(raw)
    name = candidate.name
    if name in ("", ".", ".."):
        raise EpicsError(
            f"{label} must end in a plain filename (got {raw!r})", error_code="INVALID_INPUT"
        )
    parent = resolve_user_path(str(candidate.parent), kind="dir", label=label)
    target = parent / name
    if target.is_symlink():
        raise EpicsError(
            f"{label} is an existing symlink; refusing to write through it (got {raw!r})",
            error_code="INVALID_INPUT",
        )
    return target
