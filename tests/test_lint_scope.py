"""Drift guard for the lint scope (S25 / F20).

Before S25 the ruff hooks scoped only ``src tests``, so ``scripts/``, tracked product code:
was silently unlinted while ``pre-commit run --all-files`` still reported green (a green-gate
claim that did not cover everything, measured: ``ruff check .`` was red on
``scripts/check_no_ess_internal.py``). This guard keeps ``scripts`` inside the lint scope so the
gap cannot reopen unnoticed.

HOW THE SCOPE IS EXPRESSED CHANGED WITH GB-83, the intent did not. The ruff hooks used to name
``src tests scripts`` on the command line and pass no filenames; they now receive the files from
pre-commit, which hands them every tracked Python file (``git ls-files`` under ``--all-files``).
``scripts/`` is therefore in scope by construction, and the two ways to lose it again are the ones
asserted below: putting explicit paths back on the command line, which would scope everything else
away, or excluding the directory in the ruff configuration, which ``--force-exclude`` would then
honour. The old assertion looked for the literal ``scripts`` in a line containing ``uv run ruff``
and cannot be kept: there is no such path argument any more, so it would pin the very wiring the
change removes.
"""

import tomllib
from pathlib import Path

# PyYAML arrives here as a transitive dependency of pre-commit rather than as a declared one, and
# it ships no type stubs; both are handled the way this repository already handles p4p, requests
# and urllib3, through ``ignore_missing_imports`` in pyproject.toml. Reading the hook list with a
# hand-rolled scanner instead would put a second, unguarded YAML parser in the tree for the sake
# of four scalar fields.
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / ".pre-commit-config.yaml"
_PYPROJECT = _ROOT / "pyproject.toml"

#: The arguments a ruff hook entry may carry. Anything else is either a path (which would scope
#: files away again) or a flag nobody argued for.
_ALLOWED_RUFF_ARGS = frozenset(
    {"uv", "run", "ruff", "check", "format", "--check", "--force-exclude"}
)


def _ruff_hooks() -> list[dict[str, object]]:
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    ruff_hooks = [hook for hook in hooks if str(hook["entry"]).startswith("uv run ruff")]
    assert ruff_hooks, "no ruff hook entries found in .pre-commit-config.yaml"
    return ruff_hooks


def test_ruff_hooks_scope_nothing_away() -> None:
    """No ruff hook may narrow its own scope by naming paths, and each must take the file list."""
    for hook in _ruff_hooks():
        arguments = str(hook["entry"]).split()
        unexpected = sorted(set(arguments) - _ALLOWED_RUFF_ARGS)
        assert not unexpected, (
            f"ruff hook {hook['id']!r} carries {unexpected}: a path argument here scopes every "
            "other directory away, which is how scripts/ was lost before S25"
        )
        assert hook["pass_filenames"] is True, (
            f"ruff hook {hook['id']!r} passes no filenames and names no paths, so it would lint "
            "nothing at all"
        )


def test_the_ruff_configuration_does_not_exclude_scripts() -> None:
    """The second way to lose the directory, now that the hooks no longer name it.

    The hooks run with ``--force-exclude``, which makes ruff honour its configured exclusions even
    for files handed to it explicitly. An entry for ``scripts`` there would therefore reopen the
    S25 gap silently, and pre-commit would still report green.
    """
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    ruff = data["tool"].get("ruff", {})
    excluded = [*ruff.get("exclude", []), *ruff.get("extend-exclude", [])]
    offenders = [entry for entry in excluded if "scripts" in entry]
    assert not offenders, f"[tool.ruff] excludes scripts/: {offenders}"


def test_mypy_scope_includes_scripts() -> None:
    """mypy must type-check ``scripts/`` too (H4). Ruff was extended to ``scripts`` but the mypy
    ``files`` list lagged, leaving tracked scripts/ product code type-unchecked, the same gap that
    motivated linting scripts/ in the first place."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    files = data["tool"]["mypy"]["files"]
    assert "scripts" in files, f"[tool.mypy] files does not include scripts/: {files}"
