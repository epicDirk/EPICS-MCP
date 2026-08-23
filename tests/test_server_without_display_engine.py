"""GQ-157: a core-only server offers NO display tool, and the README now says so.

WHY THIS FILE EXISTS. ``README.md`` promised for two releases that the display-aware surface
"refuses with an explanation instead of failing obscurely". Measured, that is true of the two
display-aware COMMANDS and false of the four display-aware MCP TOOLS: ``server.py`` registers them
only when ``opi_navigation`` imports, so a caller without the engine meets a shorter tool list
rather than a refusal. Correcting the sentence was the work item; this module is what keeps the
corrected sentence true, because a page cannot check itself.

WHAT IT DOES NOT DO, deliberately. It does not read ``README.md`` and does not check any prose. A
guard that greps a sentence for a behavioural promise is the half-guard ``docs/known-limits.md``
records as its own defect class, and ``tests/test_prose_counters.py`` does not watch the README
either. What is pinned here is the BEHAVIOUR the sentence describes, so the sentence goes stale
only if this goes red first.

WHY A SUBPROCESS RATHER THAN A FIXTURE. The engine is really installed in this checkout, so
monkeypatching ``find_spec`` proves nothing about a module that IMPORTS the engine rather than
asking about it. ``tests.engine_gate.BLOCKER`` makes it genuinely unreachable in a child process
and is therefore valid in BOTH environments: locally, where the engine is present, and in CI, which
syncs without ``--group displays``. That is the whole reason the first test below is worth having:
it is the one that runs in the lane a public user actually installs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.engine_gate import BLOCKER, engine_available

_REPO = Path(__file__).resolve().parents[1]

#: The four tools ``display_tools.register_display_tools`` adds. Named rather than counted: a count
#: would be a second, unguarded derivation of the same set, and it is the NAMES the corrected
#: README sentence is about.
_DISPLAY_TOOLS = frozenset({"coverage_audit", "crossplane_check", "find_device", "validate_pvs"})

#: A tool the core server registers unconditionally. The POSITIVE CONTROL, and it is not decoration:
#: without it the absence assertion below would also pass on a server that never came up, on an
#: empty tool list, and on a subprocess whose output got lost.
_CORE_CONTROL = "get_pv_value"

#: Printed by the child so the parent parses one line rather than a payload.
_LIST_TOOLS = (
    "import asyncio\n"
    "async def _main():\n"
    "    from epics_mcp.server import mcp\n"
    "    print(' '.join(sorted(t.name for t in await mcp.list_tools())))\n"
    "asyncio.run(_main())\n"
)


def _tool_names_without_the_engine() -> frozenset[str]:
    """``tools/list`` from a server started where ``opi_navigation`` cannot be imported at all."""
    result = subprocess.run(
        [sys.executable, "-c", BLOCKER + _LIST_TOOLS],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, (
        "the core server must start where the engine is unreachable, an optional group may never "
        f"crash it:\n{result.stderr[-1500:]}"
    )
    return frozenset(result.stdout.split())


def test_a_core_only_server_registers_no_display_tool() -> None:
    """The half that matters, and the half CI can prove: absent, not refusing.

    This is the assertion the corrected README sentence rests on. It goes red on exactly the change
    that would make the OLD sentence true again, a display tool registered unconditionally with a
    refusing body, which is why it is worth its subprocess.
    """
    names = _tool_names_without_the_engine()

    assert _CORE_CONTROL in names, (
        "the positive control is missing, so this run says nothing about the display tools: "
        f"the core-only server offered {sorted(names)}"
    )
    offered = sorted(_DISPLAY_TOOLS & names)
    assert not offered, (
        f"a core-only server must not offer any display tool, found: {offered}. The README says a "
        "caller without the engine meets a shorter tool list rather than a refusal; a tool that "
        "registers and then refuses makes that sentence false."
    )


@pytest.mark.skipif(
    not engine_available(),
    reason="the four tools exist only with opi_navigation, so this cannot run on a core-only "
    "install; CI reports it as a SKIP rather than not collecting it at all",
)
async def test_the_same_server_registers_them_when_the_engine_is_there() -> None:
    """The other direction, so the absence above is not simply a server that registers nothing.

    ⚠ SEPARATE FROM THE TEST ABOVE ON PURPOSE, AND MERGING THE TWO SILENTLY KILLS THE GUARD. The
    ``skipif`` here is unavoidable: these four names do not exist without the engine. In a single
    function it would take the FIRST assertion down with it, and precisely in the lane where that
    one counts, because CI syncs without ``--group displays``. A guard that is skipped where it
    matters is not a guard. Whoever tidies this file next: leave them apart.

    In-process rather than in a subprocess, which is a cost decision with a measured price: the
    child in the sibling test needs about seven seconds to import the server, and this half needs
    no separate world, because the engine IS installed wherever this runs at all.
    """
    from epics_mcp.server import mcp

    names = frozenset(tool.name for tool in await mcp.list_tools())

    missing = sorted(_DISPLAY_TOOLS - names)
    assert not missing, (
        f"the engine is installed here, so all four display tools must be registered: {missing} "
        f"are absent. Offered: {sorted(names)}"
    )
