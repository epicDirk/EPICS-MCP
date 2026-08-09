"""Guard for ``scripts/mypy_on_index.py`` (GB-83).

The script decides WHAT the type gate sees. If it silently materialises the working tree
instead of the index, or leaves a stale tree behind, the gate keeps reporting green while
checking the wrong files, which is the failure mode the script exists to prevent. So the
two claims that carry it are measured here: the tree IS the index, and a reused tree
never keeps a file the commit removed.

The script is loaded by path rather than imported by name. ``scripts/`` is not on the
test path in every repository that carries this file, and a ``sys.path`` splice is ruled
out by QUALITY-STANDARD.md.

Byte-identical copies of this file live next to each copy of the script; see its
PROVENANCE block.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mypy_on_index.py"


def load_script() -> ModuleType:
    """Load the script as a module without touching ``sys.path``."""
    spec = importlib.util.spec_from_file_location("mypy_on_index_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository: one committed file, one staged, one untracked."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "committed.py").write_text("A = 1\n", encoding="utf-8")
    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "guard@example.invalid")
    git(root, "config", "user.name", "guard")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    (root / "src" / "staged.py").write_text("B = 2\n", encoding="utf-8")
    git(root, "add", "src/staged.py")
    (root / "src" / "untracked.py").write_text("C = 3\n", encoding="utf-8")
    return root


def test_the_tree_is_the_index_and_not_the_working_tree(repo: Path, tmp_path: Path) -> None:
    """The whole point: staged content in, untracked content out.

    The third assertion is the one a working-tree implementation would fail: a change
    that is only on disk must not reach the gate, because it is not what the commit
    carries either.
    """
    script = load_script()
    (repo / "src" / "committed.py").write_text("A = 999  # only on disk\n", encoding="utf-8")
    tree = tmp_path / "tree"

    script.materialise_index(repo, tree)

    assert (tree / "src" / "staged.py").exists(), "a staged file belongs to the commit"
    assert not (tree / "src" / "untracked.py").exists(), "an untracked file does not"
    assert (tree / "src" / "committed.py").read_text(encoding="utf-8") == "A = 1\n"


def test_a_reused_tree_never_keeps_a_file_the_commit_removes(repo: Path, tmp_path: Path) -> None:
    """Materialising twice must not accumulate.

    Without the removal, a file deleted from the index would survive from the previous
    run and mypy would go on checking code that is leaving the repository: green or red,
    either way about the wrong tree.
    """
    script = load_script()
    tree = tmp_path / "tree"
    script.materialise_index(repo, tree)
    assert (tree / "src" / "staged.py").exists()

    git(repo, "rm", "-q", "--cached", "src/staged.py")
    script.materialise_index(repo, tree)

    assert not (tree / "src" / "staged.py").exists(), "stale file survived a second run"
    assert (tree / "src" / "committed.py").exists(), "the rest of the index must still be there"


def test_the_scratch_tree_is_named_per_repository_and_per_process() -> None:
    """Two windows committing at the same moment must not share one tree."""
    script = load_script()
    first = script.scratch_tree_name(Path("/a/alpha"), 111)
    second = script.scratch_tree_name(Path("/a/alpha"), 222)
    other_repo = script.scratch_tree_name(Path("/a/beta"), 111)

    assert first != second, "the pid must separate two processes"
    assert first != other_repo, "the repository name must separate two repositories"
    assert first.startswith(script.SCRATCH_PREFIX)


def test_the_command_pins_the_project_the_directory_and_the_cache() -> None:
    """``--project`` is what keeps uv on the existing environment.

    Without it uv would build a second environment inside the scratch tree on every
    commit, which is the difference between one second and one minute.
    """
    script = load_script()
    command = script.mypy_command(Path("/repo"), Path("/tree"), ["--strict"])

    assert command[:2] == ["uv", "run"]
    assert command[command.index("--project") + 1] == str(Path("/repo"))
    assert command[command.index("--directory") + 1] == str(Path("/tree"))
    assert command[command.index("--cache-dir") + 1] == str(Path("/repo") / ".mypy_cache")
    assert command[-1] == "--strict", "extra arguments must reach mypy"


def test_main_removes_the_tree_and_propagates_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing type check must not leave a scratch tree behind, and must stay failing."""
    script = load_script()
    seen: list[Path] = []

    def fake_run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(list(args), 0, f"{tmp_path}\n", "")
        if args[:2] == ["git", "checkout-index"]:
            seen.append(Path(str(args[-1]).removeprefix("--prefix=")))
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return subprocess.CompletedProcess(list(args), 7, "", "")

    monkeypatch.setattr(script.subprocess, "run", fake_run)

    assert script.main([]) == 7, "the exit code of mypy is the exit code of the hook"
    assert seen, "the index was never materialised"
    assert not seen[0].exists(), "the scratch tree survived a failing run"


def test_nothing_runs_at_import_time() -> None:
    """The fail-OPEN guard, measured instead of asserted in prose.

    The module header promises that a half-written copy is a syntax error rather than a
    module that imports cleanly and checks nothing. That only holds while every statement
    lives inside a function or the ``__main__`` guard: a truncated file then either fails
    to parse or defines an incomplete ``main`` that is never reached by the hook's exit
    code. Top-level work would break the promise silently, so it is measured here.
    """
    body = ast.parse(SCRIPT.read_text(encoding="utf-8")).body
    allowed = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.Expr)
    offenders = [
        type(node).__name__
        for node in body
        if not isinstance(node, allowed) and not _is_main_guard(node)
    ]
    assert not offenders, f"statements run at import time: {offenders}"

    docstrings = [node for node in body if isinstance(node, ast.Expr)]
    assert len(docstrings) == 1 and isinstance(docstrings[0].value, ast.Constant), (
        "the only top-level expression may be the module docstring"
    )


def _is_main_guard(node: ast.stmt) -> bool:
    """Is *node* the ``if __name__ == "__main__":`` block?"""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )
