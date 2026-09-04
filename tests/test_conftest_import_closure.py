"""The invariant that makes the BLAS default in ``tests/conftest.py`` work at all.

``conftest.py`` sets ``OPENBLAS_NUM_THREADS`` with ``os.environ.setdefault`` AFTER its own import
block. That placement is only correct while none of those imports pulls in OpenBLAS, which arrives
through numpy, which arrives through p4p: the variable is read when the library LOADS, so a load
that happens above the ``setdefault`` silently makes the whole measure a no-op. Nothing about that
is visible at the call site, and the failure mode is a suite that crashes exactly as before while
the line sits there looking like a fix.

So the invariant is pinned here rather than written into a comment. One future import in
``conftest.py`` from any of the sixteen ``src`` modules that carry p4p at module level, and this
test goes red with the name of the offending import, which is the moment somebody can still choose
between moving the ``setdefault`` up and importing something else.

The AST walk is NOT reimplemented: ``tests/test_engine_gate.py`` already owns it for the display
engine, and its ``_names_of`` docstring records what happened the one time two walkers answered the
same question from two copies. Same walk, different package and different graph root.

SCOPE, stated because a guard that is read as wider than it is misleads: this proves the ordering,
not that any particular machine crashes without the default. The crash is a failed thread-arena
allocation and therefore not deterministic; three full-suite runs with the variable stripped were
green on 2026-09-04 (see CONTRIBUTING.md, the dev-setup section).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.test_engine_gate import _module_level_imports, _reaches

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"

#: The libraries whose LOAD time is what the ``setdefault`` has to beat. numpy is named as well as
#: p4p because it is the one that actually carries OpenBLAS; p4p is merely how it gets here today,
#: and pinning only the courier would go green the day something else imports numpy directly.
_BLAS_CARRIERS = frozenset({"p4p", "numpy"})


def _module_name(path: Path) -> str:
    """The dotted ``epics_mcp.*`` name of a file under ``src``, packages named by their folder."""
    parts = list(path.relative_to(_SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _blas_tainted_modules() -> set[str]:
    """Every ``src`` module whose import loads a BLAS carrier, transitively, at MODULE level.

    Module level is the whole question, exactly as it is for the engine walk one file over: an
    import inside a function costs nothing until it is called, and several modules here reach p4p
    that way on purpose.
    """
    graph = {
        _module_name(path): _module_level_imports(ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_SRC.rglob("*.py"))
    }
    tainted = {
        module
        for module, imports in graph.items()
        if any(name.split(".")[0] in _BLAS_CARRIERS for name in imports)
    }
    changed = True
    while changed:  # transitive closure over ~60 modules; a fixpoint loop is plenty
        changed = False
        for module, imports in graph.items():
            if module in tainted:
                continue
            if any(_reaches(name, tainted) for name in imports):
                tainted.add(module)
                changed = True
    return tainted


def test_the_walk_finds_the_carriers_it_is_meant_to_find() -> None:
    """Positive control, without which the real assertion below is green for free.

    An empty or near-empty taint set would satisfy ``test_conftest_imports_load_no_blas_carrier``
    no matter what ``conftest.py`` imports, and a walker that stops working fails exactly that way:
    quietly, and in the safe-looking direction. So the population is asserted to be substantial and
    to contain the module every reader would expect in it.
    """
    tainted = _blas_tainted_modules()
    assert "epics_mcp.services.epics_client" in tainted, (
        "the PV client no longer reaches p4p at module level; either the walk broke or the import "
        "moved, and until that is settled the guard below proves nothing"
    )
    assert len(tainted) >= 10, (
        f"only {len(tainted)} src module(s) reach a BLAS carrier at module level, which is far "
        "below every previous measurement (16 on 2026-09-04); suspect the walk, not the tree"
    )


def test_conftest_imports_load_no_blas_carrier() -> None:
    """THE invariant: nothing ``conftest.py`` imports may load OpenBLAS above the ``setdefault``.

    Goes red with the offending import named, because "conftest imports something that loads
    numpy" is not actionable and "``from epics_mcp.services.doctor import check`` does" is.
    """
    tainted = _blas_tainted_modules()
    conftest = ast.parse((_REPO / "tests" / "conftest.py").read_text(encoding="utf-8"))
    offenders = sorted(
        name
        for name in _module_level_imports(conftest)
        if name.split(".")[0] in _BLAS_CARRIERS or _reaches(name, tainted)
    )
    assert not offenders, (
        f"tests/conftest.py imports {offenders} at module level, and each of those loads a BLAS "
        "carrier BEFORE the os.environ.setdefault below the import block, which makes that "
        "default a no-op. Either move the setdefault above the imports (every following import "
        "then needs '# noqa: E402') or reach this code some other way."
    )


@pytest.mark.parametrize("carrier", sorted(_BLAS_CARRIERS))
def test_each_named_carrier_is_really_present_in_the_tree(carrier: str) -> None:
    """The second half of the positive control: a carrier nobody imports pins nothing.

    ``numpy`` is not imported by name anywhere in ``src`` today, it arrives through p4p, so this
    asserts presence in the ENVIRONMENT rather than in the source. If a future install drops one,
    this says so instead of letting the guard shrink to half its reach in silence.
    """
    pytest.importorskip(
        carrier,
        reason=f"{carrier} is not installed, so it cannot be what the conftest default guards",
    )
