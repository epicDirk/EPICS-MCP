"""Run mypy against the git INDEX instead of the working tree (GB-83).

WHY. The lint and type hooks of this repository declare ``pass_filenames: false``, so
their tools walk the WORKING TREE. A single untracked file belonging to a parallel
window therefore blocks EVERY commit in the repository, including commits that do not
touch it. That happened twice, and the second time it was mutual: two windows held each
other, each one recording the other as the cause, so waiting was not a way out. CI does
not have the problem, because it runs on a clean checkout where untracked files do not
exist. The local gate was, in other words, stricter than CI at a place that has nothing
to do with the commit.

WHAT. ``git checkout-index`` materialises exactly the index into a scratch tree, and
mypy runs there. That is constructively the set CI checks. The alternative that suggests
itself, subtracting the untracked files with ``--exclude``, was measured and rejected:
mypy's ``--exclude`` is an UNANCHORED regular expression, so excluding an untracked
``utils.py`` also silences a tracked ``src/utils.py``, and the gate reports success on
staged, broken code. Materialising has no such failure mode: there is no pattern to get
wrong, no command-line length limit, and a foreign untracked file simply is not there.

WHY NOT FOR RUFF. Every rule family this project selects is per-file exact; ruff has no
cross-file analysis. Those two hooks therefore pass filenames instead, which is both
simpler and structurally immune. See the header of ``.pre-commit-config.yaml``.

COST, measured on this repository at 209 source files: materialising 0.97 s, the first
mypy run 45.8 s against a cold cache, every later run 3.6 s against 3.9 s in the
repository itself. The cache survives a fresh tree because mypy validates by content
hash rather than by mtime, so the standing overhead is about one second per commit. The
cache directory is deliberately the repository's own, so both paths keep it warm.

PATHS. mypy runs with the scratch tree as its working directory and therefore prints
repository-relative locations (measured: ``src\\foo.py:1: error: ...``). No rewriting is
needed and error locations stay clickable from the repository root.

PROVENANCE, because a copied file that does not say where it came from is how three
versions of one guard start drifting apart. Byte-identical copies live in three
repositories, each as ``scripts/mypy_on_index.py``: cs-studio-mcp, EPICS-MCP-Server and
the opi-foundry skill repository. The file is written in English and pure ASCII so that
all three accept it (EPICS-MCP-Server enforces both by hook). A reporting guard in the
opi-foundry workspace compares the three hashes and says so when they diverge; it never
blocks. The sanctioned alternative, one shared hook via ``.pre-commit-hooks.yaml`` and a
``rev:`` pin, is currently closed by QUALITY-STANDARD.md.

EVERYTHING IS INSIDE ``main()`` on purpose. A half-written copy of this file must be a
syntax error, which is loud, rather than a module that imports cleanly and checks
nothing, which would be a silent pass. That is the fail-OPEN lesson from the workspace
pre-commit hook.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRATCH_PREFIX = "precommit-mypy-index-"


def find_repo_root(start: Path) -> Path:
    """Ask git for the work tree root rather than counting ``..`` from ``__file__``.

    ``main`` passes this script's OWN directory, never the current one. The script then
    always operates on the repository it belongs to, whatever directory the hook happens
    to be started from, and it cannot be pointed at a neighbouring checkout by accident.
    """
    finished = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(finished.stdout.strip())


def scratch_tree_name(root: Path, pid: int) -> str:
    """Name the scratch tree per repository AND per process.

    The pid is what keeps two windows that commit at the same moment out of each other's
    tree. Kept as a pure function so a test can pin the shape without spawning anything.
    """
    return f"{SCRATCH_PREFIX}{root.name}-{pid}"


def materialise_index(root: Path, tree: Path) -> None:
    """Write exactly the index into *tree*.

    The tree is REMOVED first, never reused in place: a file that the commit deletes
    would otherwise stay behind from an earlier run, and mypy would go on checking code
    that is on its way out of the repository.
    """
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)
    subprocess.run(
        ["git", "checkout-index", "-a", "-f", f"--prefix={tree.as_posix()}/"],
        cwd=root,
        check=True,
    )


def mypy_command(root: Path, tree: Path, extra_args: list[str]) -> list[str]:
    """Build the command line as a value, so a test can read it without running mypy.

    ``--project`` keeps uv on this repository's existing environment while
    ``--directory`` puts mypy into the scratch tree; without the first, uv would try to
    build a second environment inside the scratch tree on every commit.
    """
    return [
        "uv",
        "run",
        "--project",
        str(root),
        "--directory",
        str(tree),
        "mypy",
        "--cache-dir",
        str(root / ".mypy_cache"),
        *extra_args,
    ]


def main(argv: list[str]) -> int:
    root = find_repo_root(Path(__file__).resolve().parent)
    tree = Path(tempfile.gettempdir()) / scratch_tree_name(root, os.getpid())
    try:
        materialise_index(root, tree)
        return subprocess.run(mypy_command(root, tree, argv), check=False).returncode
    finally:
        shutil.rmtree(tree, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
