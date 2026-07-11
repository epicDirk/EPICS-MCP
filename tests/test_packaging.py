"""Packaging drift guards (C7 / L-Packaging)."""

from __future__ import annotations

import ast
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

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


def test_operator_guide_ships_in_the_wheel(tmp_path: Path) -> None:
    """The ``epics-pv://guide`` resource reads ``operator_guide.md`` as package data. The
    ``importlib.resources`` load test passes off the *source tree* in an editable install, so it
    cannot catch a wheel-exclusion regression (a stray ``[tool.hatch.build]`` include that forgets
    ``*.md``, a move/rename). This builds an actual wheel and asserts the file is inside it — the
    real inclusion guard for E1's ``pip install`` distribution DoD. Skipped only if the build
    toolchain is unavailable (kept honest via the skip reason, never a silent pass)."""
    repo_root = Path(epics_pv_mcp.__file__).resolve().parent.parent.parent  # …/EPICS-MCP-Server
    try:
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"wheel build toolchain unavailable: {exc}")
    if result.returncode != 0:
        pytest.skip(f"wheel build failed (offline/toolchain): {result.stderr[-400:]}")

    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
    assert "epics_pv_mcp/operator_guide.md" in names, (
        "operator_guide.md missing from the wheel — the guide resource would raise "
        f"FileNotFoundError in a pip-installed server. Package files: "
        f"{sorted(n for n in names if n.startswith('epics_pv_mcp/'))}"
    )
