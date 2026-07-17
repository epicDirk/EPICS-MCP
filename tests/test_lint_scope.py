"""Drift guard for the lint scope (S25 / F20).

Before S25 the ruff hooks scoped only ``src tests``, so ``scripts/`` — tracked product code —
was silently unlinted while ``pre-commit run --all-files`` still reported green (a green-gate
claim that did not cover everything, measured: ``ruff check .`` was red on
``scripts/check_no_ess_internal.py``). This guard pins ``scripts`` into the ruff hook scope so the
gap cannot reopen unnoticed.
"""

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / ".pre-commit-config.yaml"
_PYPROJECT = _ROOT / "pyproject.toml"


def test_ruff_hooks_include_scripts_in_scope() -> None:
    """Every ``uv run ruff`` hook entry must lint ``scripts/`` (not just ``src tests``)."""
    ruff_lines = [
        line for line in _CONFIG.read_text(encoding="utf-8").splitlines() if "uv run ruff" in line
    ]
    assert ruff_lines, "no ruff hook entries found in .pre-commit-config.yaml"
    for line in ruff_lines:
        assert "scripts" in line, f"ruff hook does not lint scripts/: {line.strip()!r}"


def test_mypy_scope_includes_scripts() -> None:
    """mypy must type-check ``scripts/`` too (H4). Ruff was extended to ``scripts`` but the mypy
    ``files`` list lagged, leaving tracked scripts/ product code type-unchecked — the same gap that
    motivated linting scripts/ in the first place."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    files = data["tool"]["mypy"]["files"]
    assert "scripts" in files, f"[tool.mypy] files does not include scripts/: {files}"
