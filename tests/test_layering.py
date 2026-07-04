"""Layering guard: the services layer must never import upward from the tool layer (M9/C2-ii).

The intended dependency direction is ``server → tools → services → clients``.
``services/diagnose`` used to import ``_is_archived`` / ``_is_alarm_configured`` /
``_find_channels`` from ``tools/*`` — a ``service → tool`` inversion that made ``diagnose``
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
_FORBIDDEN_ROOT = "epics_pv_mcp.tools"


def _imports_the_tool_layer(module: str) -> bool:
    """True iff *module* is ``epics_pv_mcp.tools`` or a submodule of it."""
    return module == _FORBIDDEN_ROOT or module.startswith(f"{_FORBIDDEN_ROOT}.")


def _tool_layer_imports(source: str) -> list[str]:
    """Return the ``epics_pv_mcp.tools*`` module names imported by *source* (both import forms)."""
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # ``node.module`` is None for a bare relative import (``from . import x``) — ignored.
            if node.module and _imports_the_tool_layer(node.module):
                offenders.append(node.module)
        elif isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names if _imports_the_tool_layer(alias.name)
            )
    return offenders


def test_services_layer_never_imports_the_tool_layer() -> None:
    """No ``services/*.py`` module may import ``epics_pv_mcp.tools`` (layering inversion guard)."""
    modules = sorted(_SERVICES_DIR.glob("*.py"))
    assert modules, f"no services modules found under {_SERVICES_DIR} — check the path"
    violations = {
        path.name: offenders
        for path in modules
        if (offenders := _tool_layer_imports(path.read_text(encoding="utf-8")))
    }
    assert not violations, (
        "services/ must not import the tool layer "
        f"(server → tools → services → clients): {violations}"
    )
