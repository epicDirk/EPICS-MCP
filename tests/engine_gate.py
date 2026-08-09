"""Is the optional ``opi_navigation`` display engine usable in this environment?

One helper, because the test suite asked that question in four places with a bare
``importlib.util.find_spec("opi_navigation")`` and that call is not total: it PROPAGATES whatever
a meta-path finder raises. Two of the four sites run at COLLECTION time (``tests/conftest.py``
at module level, ``tests/test_server.py`` inside a ``skipif`` decorator), so under a raising
finder the whole run died instead of skipping the display-coupled modules. Measured on the tree
before this helper existed, with a finder that raises ``ModuleNotFoundError`` for the engine::

    ImportError while loading conftest '.../tests/conftest.py'
    tests/conftest.py:22: in <module>
        if importlib.util.find_spec("opi_navigation") is None:
    E   ModuleNotFoundError: import of 'opi_navigation' is blocked by policy
    (exit 4, nothing collected)

The production path had already been made total in ``cli_common.require_display_engine``; this is
the same repair for the test side, and QA-48 is the ticket that tracked the asymmetry.

WHY A BOOL, WHEN THE PRODUCTION HELPER ANSWERS THREE WAYS. This is a decision with a measured
cost, not a necessity, and the first version of this docstring got that wrong. ``cli_common``
separates "absent" (exit 2, a usage error) from "present but not importable" (exit 1, a failure)
because it has a USER to report to and two different exit codes to report with. A collection hook
has two exits, collect or do not collect, so the two finder failures below collapse. What does
NOT collapse, and is the reason the collapse is safe: a **broken** engine, one whose ``find_spec``
answers and whose import then fails, still makes this return ``True``, so its modules are
collected and fail loudly at import. Measured 2026-07-31, when the list held six, against a finder
that answers with a spec whose loader raises: 1705 tests collected, **6 collection errors** on
exactly the engine-coupled modules, exit 2. Those figures are that probe's and are not re-derived
here, which is why the count sits in the date rather than in the sentence:
``ENGINE_COUPLED_MODULES`` in ``tests/conftest.py`` is longer today, and a number repeated beside
a list it does not read is how this paragraph drifted in the first place. That is the direction a
broken environment should fail in. Only a finder that
cannot answer at all is treated as "not there", because there is nothing to collect then and no
way to say so from a ``collect_ignore`` list.

``find_spec`` is reached through the ``importlib.util`` MODULE ATTRIBUTE rather than imported by
name, and that is load-bearing: ``tests/test_cli_without_display_engine.py`` fakes an engine-less
environment by monkeypatching exactly that attribute, and its own control asserts through this
helper. A ``from importlib.util import find_spec`` binding here would capture the real function at
import time, leaving the fixture's fake unreached and the control green for the wrong reason.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping

_ENGINE = "opi_navigation"

#: A subprocess preamble that makes the engine genuinely unreachable, for the questions a fixture
#: cannot answer. ONE home, because the two callers need the identical world and used to carry
#: byte-identical copies of it.
#:
#: WHY A SUBPROCESS AT ALL: the ``engine_absent`` fixture in
#: ``tests/test_cli_without_display_engine.py`` monkeypatches ``importlib.util.find_spec``, which is
#: enough for anything that ASKS whether the engine is there, and blind to anything that IMPORTS it.
#: In this checkout ``opi_navigation`` is really installed, so a module-level (or parser-level)
#: engine import still succeeds under that fixture and the test stays green; it would redden only in
#: CI. Anything claiming "this works without the engine" therefore has to run here.
#:
#: It RAISES rather than answering None, which is stricter than a plain missing package and
#: deliberate: ``find_spec`` propagates whatever a meta-path finder raises, so this also covers a
#: restricted or broken import system.
BLOCKER = (
    "import sys\n"
    "class _Blocker:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    f"        if name.split('.')[0] == '{_ENGINE}':\n"
    '            raise ModuleNotFoundError(f"No module named {name!r}")\n'
    "        return None\n"
    "sys.meta_path.insert(0, _Blocker())\n"
)


def engine_available() -> bool:
    """True iff the ``opi_navigation`` engine is present, never raising for any finder.

    ``ModuleNotFoundError`` from a finder still means the module is not there (a restricted import
    system is the measured case), so it stays the absent answer. Any OTHER finder exception means
    the finder could not answer, which is a different claim; the test side has no way to report it
    and treats it as absent as well, deliberately, so a broken import system skips the
    engine-coupled modules instead of taking the whole run down with it.
    """
    try:
        return importlib.util.find_spec(_ENGINE) is not None
    except ModuleNotFoundError:
        return False
    except Exception:  # noqa: BLE001 (a finder that cannot answer must not abort collection)
        return False


#: The harness switch that turns a MISSING engine from a silent skip into a loud refusal.
#: Named like its sibling ``EPICS_MCP_REQUIRE_LIVE`` (tests/live_gate.py) because it answers the
#: same class of question: "I DEMANDED this run, do not hand me a green report for it".
REQUIRE_DISPLAYS_ENV = "EPICS_MCP_REQUIRE_DISPLAYS"

# Read generously so a "true"/"yes" does not silently count as "not demanded"; a false-negative
# switch would be the very defect this exists to remove. Same vocabulary as live_gate._TRUTHY.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def displays_demanded(env: Mapping[str, str]) -> bool:
    """Was the display suite explicitly demanded?

    The environment is INJECTED rather than read globally, so the decision is deterministic and
    offline testable. That matters more here than usual: the consumer is a collection hook, which
    is the hardest place in a test suite to observe from the inside.
    """
    return env.get(REQUIRE_DISPLAYS_ENV, "").strip().lower() in _TRUTHY


def engine_collection_decision(*, available: bool, demanded: bool) -> str:
    """What ``tests/conftest.py`` should do about its engine-coupled modules.

    ``"collect"``  the engine is there, run them.
    ``"ignore"``   it is absent and nobody asked, skip them (the standalone-core case a public
                   user gets, and what CI deliberately exercises).
    ``"fail"``     it is absent but the run DEMANDED it: refuse loudly instead of reporting a
                   green run over a hundred tests that never executed (GB-27).

    A pure function, separate from the hook that consumes it, because a hook can only be observed
    by starting a whole pytest process. This is the half a normal test can pin.
    """
    if available:
        return "collect"
    return "fail" if demanded else "ignore"
