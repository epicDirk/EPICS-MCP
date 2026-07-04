"""Packaging drift guards (C7 / L-Packaging)."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import epics_pv_mcp


def _fallback_version_literals(init_source: str) -> list[str]:
    """Return the string literals assigned to ``__version__`` in ``__init__.py`` (the
    ``PackageNotFoundError`` fallback). The ``version("epics-pv-mcp")`` call is a Call, not a
    Constant, so only the hardcoded fallback string is collected."""
    literals: list[str] = []
    for node in ast.walk(ast.parse(init_source)):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals.append(value.value)
    return literals


def test_version_fallback_matches_pyproject() -> None:
    """C7 drift guard: ``__init__.py``'s ``PackageNotFoundError`` fallback is a hardcoded version
    string that must be hand-synced with ``pyproject [project].version`` on every bump. When a
    source checkout has no installed metadata, that stale literal becomes a silent version lie —
    this test fails the moment the two drift, so a bump can't forget the fallback."""
    package_init = Path(epics_pv_mcp.__file__)
    repo_root = package_init.resolve().parent.parent.parent  # …/EPICS-MCP-Server
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["version"]

    fallbacks = _fallback_version_literals(package_init.read_text(encoding="utf-8"))
    assert fallbacks == [declared], (
        f"__init__.py __version__ fallback {fallbacks} != pyproject version {declared!r} — "
        "sync the hardcoded fallback on every version bump"
    )
