"""Layering guard: the services layer must never import upward from the tool layer (M9/C2-ii).

The intended dependency direction is ``server → tools → services → clients``.
``services/diagnose`` used to import ``_is_archived`` / ``_is_alarm_configured`` /
``_find_channels`` from ``tools/*``, a ``service → tool`` inversion that made ``diagnose``
unusable without the MCP tool layer. C2-ii lifted that per-plane query logic into
:mod:`epics_pv_mcp.services.checkers`, so the inversion is gone.

This guard keeps it gone. It parses every ``services/*.py`` module with :mod:`ast` (deterministic,
no import side effects) and fails on any ``epics_pv_mcp.tools`` import. A plain pytest guard is
enough here; a formal import-linter contract is only worth it if the layering grows more rules.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SERVICES_DIR = Path(__file__).resolve().parent.parent / "src" / "epics_pv_mcp" / "services"
_SERVICES_PACKAGE = ("epics_pv_mcp", "services")
_FORBIDDEN_ROOT = "epics_pv_mcp.tools"


def _imports_the_tool_layer(module: str) -> bool:
    """True iff *module* is ``epics_pv_mcp.tools`` or a submodule of it."""
    return module == _FORBIDDEN_ROOT or module.startswith(f"{_FORBIDDEN_ROOT}.")


def _resolve_relative(level: int, module: str | None) -> str | None:
    """Resolve a relative import (``level > 0``) against the services package to an absolute module.

    All scanned files live in ``epics_pv_mcp.services``, so ``from . import x`` targets that
    package, ``from ..tools import x`` targets ``epics_pv_mcp.tools`` (the inversion), etc. Returns
    None if the relative import climbs above the known tree (can't target the tool layer then).
    """
    if level <= 0:
        return module
    keep = len(_SERVICES_PACKAGE) - (level - 1)
    if keep <= 0:
        return None
    base = ".".join(_SERVICES_PACKAGE[:keep])
    return f"{base}.{module}" if module else base


def _tool_layer_imports(source: str) -> list[str]:
    """Return the tool-layer module names imported by *source*, ABSOLUTE and RELATIVE forms.

    Catches ``import epics_pv_mcp.tools...``, ``from epics_pv_mcp.tools... import x``, ``from
    ..tools import x`` (relative, previously a blind spot), and ``from .. import tools`` (aliased
    package).
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import: resolve it against the services package first.
                resolved = _resolve_relative(node.level, node.module)
                if resolved is None:
                    continue
                if node.module is not None and _imports_the_tool_layer(resolved):
                    offenders.append(resolved)  # from ..tools[.x] import y
                else:
                    # from .. import tools, the package is `resolved`, `tools` is an imported name.
                    offenders.extend(
                        f"{resolved}.{alias.name}"
                        for alias in node.names
                        if _imports_the_tool_layer(f"{resolved}.{alias.name}")
                    )
            elif node.module and _imports_the_tool_layer(node.module):
                offenders.append(node.module)  # absolute from-import
        elif isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names if _imports_the_tool_layer(alias.name)
            )
    return offenders


def test_services_layer_never_imports_the_tool_layer() -> None:
    """No ``services/*.py`` module may import ``epics_pv_mcp.tools`` (layering inversion guard)."""
    modules = sorted(_SERVICES_DIR.glob("*.py"))
    assert modules, f"no services modules found under {_SERVICES_DIR}, check the path"
    violations = {
        path.name: offenders
        for path in modules
        if (offenders := _tool_layer_imports(path.read_text(encoding="utf-8")))
    }
    assert not violations, (
        "services/ must not import the tool layer "
        f"(server → tools → services → clients): {violations}"
    )


def test_tool_layer_import_detector_catches_all_forms() -> None:
    """The detector must flag EVERY way a services module could reach the tool layer, including the
    RELATIVE forms the old walker missed (it only matched the absolute ``epics_pv_mcp.tools`` name,
    so ``from ..tools import x`` slipped through and a regression could ship green)."""
    # Absolute forms (already covered before).
    assert _tool_layer_imports("from epics_pv_mcp.tools.archiver import x\n")
    assert _tool_layer_imports("import epics_pv_mcp.tools.archiver\n")
    # Relative forms (the fixed blind spot).
    assert _tool_layer_imports("from ..tools import archiver\n")
    assert _tool_layer_imports("from ..tools.archiver import is_archived\n")
    assert _tool_layer_imports("from .. import tools\n")
    # Must NOT flag legitimate downward/sibling imports.
    assert not _tool_layer_imports("from ..config import get_config\n")
    assert not _tool_layer_imports("from .checkers import query_channels\n")
    assert not _tool_layer_imports("from epics_pv_mcp.services.checkers import query_channels\n")
