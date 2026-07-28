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
    ``PackageNotFoundError`` fallback). The ``version("epics-mcp")`` call is a Call, not a
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
    source checkout has no installed metadata, that stale literal becomes a silent version lie:
    this test fails the moment the two drift, so a bump can't forget the fallback."""
    package_init = Path(epics_pv_mcp.__file__)
    repo_root = package_init.resolve().parent.parent.parent  # .../EPICS-MCP-Server
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["version"]

    fallbacks = _fallback_version_literals(package_init.read_text(encoding="utf-8"))
    assert fallbacks == [declared], (
        f"__init__.py __version__ fallback {fallbacks} != pyproject version {declared!r}, "
        "sync the hardcoded fallback on every version bump"
    )


#: stderr signatures of a build that failed for ENVIRONMENT reasons (offline resolver,
#: unreachable index, proxy), the only non-zero outcomes that may skip. Everything else
#: (a broken [tool.hatch.build], a backend error, an include regression) is exactly the
#: defect class this test exists to catch and must FAIL, not skip.
_OFFLINE_BUILD_SIGNATURES = (
    "failed to fetch",
    "could not resolve",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "no route to host",
    "proxy",
    "timed out",
)


def _build_failure_action(returncode: int, stderr: str) -> str:
    """Classify a wheel-build outcome: ``ok`` | ``skip`` (environment) | ``fail`` (defect).

    QA: the former blanket ``returncode != 0 -> skip`` silently swallowed every REAL
    packaging failure too, the test skipped exactly its own target class, while its
    docstring promised "never a silent pass". Split out as a pure function so the
    classification itself is unit-testable offline.
    """
    if returncode == 0:
        return "ok"
    lowered = stderr.lower()
    if any(signature in lowered for signature in _OFFLINE_BUILD_SIGNATURES):
        return "skip"
    return "fail"


def test_build_failure_classification_fails_on_real_defects() -> None:
    """The classifier's contract, pinned: environment signatures skip, a real backend/
    config error FAILS (pre-fix: everything non-zero skipped)."""
    assert _build_failure_action(0, "") == "ok"
    assert _build_failure_action(1, "error: Failed to fetch: https://pypi.org/simple/...") == "skip"
    assert _build_failure_action(1, "getaddrinfo: Temporary failure in name resolution") == "skip"
    assert _build_failure_action(1, "ValueError: invalid [tool.hatch.build] include") == "fail"
    assert _build_failure_action(1, "hatchling.builders.plugin: unknown target") == "fail"


def test_operator_guide_ships_in_the_wheel(tmp_path: Path) -> None:
    """The ``epics-pv://guide`` resource reads ``operator_guide.md`` as package data. The
    ``importlib.resources`` load test passes off the *source tree* in an editable install, so it
    cannot catch a wheel-exclusion regression (a stray ``[tool.hatch.build]`` include that forgets
    ``*.md``, a move/rename). This builds an actual wheel and asserts the file is inside it, the
    real inclusion guard for E1's ``pip install`` distribution DoD. Skipped only if the build
    TOOLCHAIN/ENVIRONMENT is unavailable (missing uv, timeout, offline resolver signature); a
    build that fails for any other reason is a real packaging defect and FAILS."""
    repo_root = Path(epics_pv_mcp.__file__).resolve().parent.parent.parent  # .../EPICS-MCP-Server
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
    action = _build_failure_action(result.returncode, result.stderr)
    if action == "skip":
        pytest.skip(f"wheel build failed with an offline signature: {result.stderr[-400:]}")
    if action == "fail":
        pytest.fail(
            "wheel build failed, a real packaging defect, not a toolchain gap:\n"
            f"{result.stderr[-400:]}"
        )

    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
    assert "epics_pv_mcp/operator_guide.md" in names, (
        "operator_guide.md missing from the wheel, the guide resource would raise "
        f"FileNotFoundError in a pip-installed server. Package files: "
        f"{sorted(n for n in names if n.startswith('epics_pv_mcp/'))}"
    )
