"""Packaging drift guards (C7 / L-Packaging)."""

from __future__ import annotations

import ast
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

import epics_mcp


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
    package_init = Path(epics_mcp.__file__)
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


def _build(target: str, out_dir: Path) -> None:
    """Build one distribution into *out_dir*, skipping only on an environment failure.

    Shared by the two artifact guards below so the offline/defect split is decided in one place.
    """
    repo_root = Path(epics_mcp.__file__).resolve().parent.parent.parent  # .../EPICS-MCP-Server
    try:
        result = subprocess.run(
            ["uv", "build", target, "--out-dir", str(out_dir)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"build toolchain unavailable: {exc}")
    action = _build_failure_action(result.returncode, result.stderr)
    if action == "skip":
        pytest.skip(f"{target} build failed with an offline signature: {result.stderr[-400:]}")
    if action == "fail":
        pytest.fail(
            f"{target} build failed, a real packaging defect, not a toolchain gap:\n"
            f"{result.stderr[-400:]}"
        )


def _repo_root() -> Path:
    return Path(epics_mcp.__file__).resolve().parent.parent.parent


def _tracked_files() -> set[str]:
    """``git ls-files``, the set an sdist is measured against."""
    listing = subprocess.run(
        ["git", "-C", str(_repo_root()), "ls-files"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()
    assert "pyproject.toml" in listing, (
        f"git ls-files returned a tree without pyproject.toml ({len(listing)} entries), the "
        "population anchor broke and the assertions below would pass vacuously"
    )
    return set(listing)


#: The top-level entries deliberately kept OUT of the sdist, each with the reason, because an
#: omission without one becomes a blanket permission the moment nobody remembers what it was for.
#: Checked in BOTH directions by the guard below: a new top-level that silently drops out is not
#: in this map and reddens, and an entry that stops being omitted (because it was deleted, or
#: because somebody added it to only-include) also reddens instead of standing as a stale claim.
_DELIBERATELY_OUT = {
    "tests": (
        "development surface. A consumer cannot run this suite from an sdist in any case: nine "
        "test modules import from scripts/ and three more read .github/ and "
        ".pre-commit-config.yaml, none of which ship either"
    ),
    "scripts": "the prose and audit guards, run by the gate chain, not by a consumer",
    ".github": "CI workflows and issue/PR templates, which belong to the repository",
    ".pre-commit-config.yaml": "the local gate chain, meaningless without scripts/ and the hooks",
    "CLAUDE.md": (
        "internal instructions for an AI assistant working IN this repository. It shipped to a "
        "public index in 0.4.0 and should not have"
    ),
    "uv.lock": (
        "the development lockfile. A consumer resolves against their own environment, and a "
        "lockfile in a source distribution invites the belief that it is honoured"
    ),
}


def test_the_sdist_carries_what_it_declares_and_nothing_stray(tmp_path: Path) -> None:
    """The sdist is compared as a SET against ``git ls-files``, in both directions (S45).

    An undeclared sdist is not "the tracked tree". Hatchling packs the working tree minus what VCS
    ignores, so it also packs every untracked, unignored file that happens to be lying around when
    the build runs. Measured on 0.4.0 before the declaration: 192 files, which was 190 tracked +
    PKG-INFO + a stray ``x.log`` from a parallel window. ``.github/workflows/publish.yml`` builds
    BOTH artifacts and uploads the whole ``dist/`` directory, so that went to a public index.

    ⚠️ This guard opens the ARTIFACT. It does not read ``[tool.hatch.build.targets.sdist]``, and
    that is the whole point rather than an implementation detail. Two measured reasons:

    * ``only-include`` is a PATH-PREFIX filter, not a VCS filter. A stray untracked file INSIDE an
      included directory still ships. Measured in a scratch project: with
      ``only-include = ["src", "tests"]``, an untracked ``src/probepkg/STRAY_TOKEN.txt`` was in the
      artifact. Only the first assertion below can see that class, and a declaration-reading guard
      never could.
    * Hatchling force-includes ``pyproject.toml``, the VCS ignore files, the readme and the
      license whatever the declaration says. Measured: ``.gitignore`` ships while being absent
      from ``only-include``. A guard comparing the two LISTS would have recorded "deliberately
      out" for a file that ships, a documented falsehood, and stayed green.

    The second assertion is by TOP-LEVEL rather than by file, deliberately: a new test module must
    not churn this map, but a new top-level directory silently dropping out of the distribution
    must redden. ``PKG-INFO`` is the one member with no tracked counterpart, because the backend
    generates it.
    """
    _build("--sdist", tmp_path)

    archives = list(tmp_path.glob("*.tar.gz"))
    assert archives, "uv build produced no sdist"
    with tarfile.open(archives[0]) as archive:
        members = {
            member.name.split("/", 1)[1]
            for member in archive.getmembers()
            if member.isfile() and "/" in member.name
        }

    tracked = _tracked_files()

    stray = sorted(members - tracked - {"PKG-INFO"})
    assert not stray, (
        f"the sdist carries files git does not track: {stray}. Hatchling packs the working tree, "
        "so anything untracked and unignored sitting in an included directory is published. "
        "Remove the file or add it to .gitignore; do not widen this assertion."
    )

    dropped = sorted({path.split("/")[0] for path in tracked - members})
    assert dropped == sorted(_DELIBERATELY_OUT), (
        f"the sdist's omissions have drifted from what is declared.\n"
        f"  dropped but not declared: {sorted(set(dropped) - set(_DELIBERATELY_OUT))}\n"
        f"  declared but not dropped: {sorted(set(_DELIBERATELY_OUT) - set(dropped))}\n"
        "A top-level in the first list is falling out of the published distribution silently: "
        "add it to [tool.hatch.build.targets.sdist] only-include, or to _DELIBERATELY_OUT with "
        "the reason. A top-level in the second list no longer describes anything and should be "
        "removed from _DELIBERATELY_OUT."
    )


def test_package_data_ships_in_the_wheel(tmp_path: Path) -> None:
    """Both package-data files have to be INSIDE the wheel, and neither is checkable from the
    source tree.

    ``operator_guide.md`` is read by the ``epics-pv://guide`` resource through
    ``importlib.resources``, which in an editable install passes off the *source tree*, so that
    load test cannot catch a wheel-exclusion regression (a stray ``[tool.hatch.build]`` include
    that forgets ``*.md``, a move/rename). ``py.typed`` is worse off: it has no load test at all,
    because it is read by a CONSUMER's type checker and never by this package, so its absence
    would go unnoticed here indefinitely. ``pyproject.toml`` cites THIS check as the reason the
    ``Typing :: Typed`` classifier is a fact rather than an aspiration, so the citation and the
    assertion have to stay together: the classifier was carrying that reference for a check that
    covered only the guide (QA-37).

    This builds an actual wheel and asserts both files are inside it, the real inclusion guard
    for E1's ``pip install`` distribution DoD. Skipped only if the build TOOLCHAIN/ENVIRONMENT is
    unavailable (missing uv, timeout, offline resolver signature); a build that fails for any
    other reason is a real packaging defect and FAILS."""
    repo_root = Path(epics_mcp.__file__).resolve().parent.parent.parent  # .../EPICS-MCP-Server
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
    shipped = sorted(n for n in names if n.startswith("epics_mcp/"))
    assert "epics_mcp/operator_guide.md" in names, (
        "operator_guide.md missing from the wheel, the guide resource would raise "
        f"FileNotFoundError in a pip-installed server. Package files: {shipped}"
    )
    assert "epics_mcp/py.typed" in names, (
        "py.typed missing from the wheel, so the Typing :: Typed classifier in pyproject.toml is "
        "false and a consumer's type checker treats this package as untyped. Nothing else would "
        f"notice: no code here reads the marker. Package files: {shipped}"
    )
