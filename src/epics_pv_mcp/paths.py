"""Workspace path boundary for user-supplied file/directory arguments.

Two layers, in order:

1. **Always-on canonicalization + existence/kind check** (the real, immediate
   value). The path is resolved (symlinks and ``..`` collapsed) and then verified
   to be a directory or a file, raising a clear ``EpicsError(INVALID_INPUT)`` that
   *names the offending argument* so an agent learns which path was bad.

2. **Opt-in ``allowed_roots`` boundary** (off by default). When the env var
   ``EPICS_MCP_ALLOWED_ROOTS`` is set, the resolved path must live under one of
   those roots, else ``EpicsError(PATH_OUTSIDE_WORKSPACE)``. **Default empty = NO
   boundary** — this is future-posture optionality, NOT a "secured" deployment:
   the server is read-only and localhost-isolated with a single trusted caller,
   so the boundary is dormant unless deliberately enabled. The separator is
   OS-dependent (``os.pathsep`` — ``;`` on Windows, ``:`` on Linux), so an
   ``EPICS_MCP_ALLOWED_ROOTS`` value is not 1:1 portable between the two.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsError


def _allowed_roots() -> list[Path]:
    """Resolve ``EPICS_MCP_ALLOWED_ROOTS`` into roots; empty/unset = no boundary.

    Guards the empty-string trap: ``"".split(os.pathsep)`` yields ``[""]`` whose
    ``Path("")`` would resolve to the *current working directory* and silently
    become an allowed root. An unset/blank value must mean "no boundary".
    """
    raw = get_config().allowed_roots
    if not raw.strip():
        return []
    return [Path(part).resolve() for part in raw.split(os.pathsep) if part.strip()]


def resolve_user_path(raw: str, *, kind: Literal["dir", "file"], label: str) -> Path:
    """Canonicalize *raw*, verify it is a *kind*, and enforce the opt-in boundary.

    *label* names the argument in error messages (e.g. ``"displays_dir"``) so the
    caller learns which path was rejected.

    Raises:
        EpicsError(INVALID_INPUT): the path does not exist or is the wrong kind.
        EpicsError(PATH_OUTSIDE_WORKSPACE): an ``allowed_roots`` boundary is
            configured and *raw* resolves outside every root.
    """
    resolved = Path(raw).resolve()
    noun = "directory" if kind == "dir" else "file"
    exists_as_kind = resolved.is_dir() if kind == "dir" else resolved.is_file()
    if not exists_as_kind:
        raise EpicsError(f"{label} is not a {noun}: {raw}", error_code="INVALID_INPUT")

    roots = _allowed_roots()
    # is_relative_to folds case on Windows (WindowsPath flavour) — do NOT swap it
    # for startswith/commonpath, which would lose that folding.
    if roots and not any(resolved.is_relative_to(root) for root in roots):
        raise EpicsError(
            f"{label} is outside the allowed roots (EPICS_MCP_ALLOWED_ROOTS): {raw}",
            error_code="PATH_OUTSIDE_WORKSPACE",
        )
    return resolved
