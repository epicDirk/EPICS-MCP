"""Drift guard: the guide's tool/env surface must match the code.

The guide is hand-written prose that names MCP tools and ``EPICS_MCP_*`` env vars. Without a guard,
a renamed tool or a removed env var makes the guide silently wrong — it would tell a fresh session
to call a tool that no longer exists, exactly when the session trusts it. This binds the guide's
tool-inventory section to the ``@mcp.tool`` registrations and its ``EPICS_MCP_*`` mentions to
``EpicsConfig`` — the same anti-drift pattern the repo already uses for README resource URIs.

Scope is deliberately narrow: MCP tool names and env vars only. The free-form Archiver MGMT verbs
(``getAllPVs`` / ``getPVsForThisAppliance``) are manual REST recipes with no implementing tool, so
they are documented in prose and never checked here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.resources import get_guide

_SRC = Path(__file__).resolve().parent.parent / "src" / "epics_pv_mcp"
_SERVER = _SRC / "server.py"
_DISPLAY_TOOLS = _SRC / "display_tools.py"


def _tools_decorated_with_mcp_tool(source: str) -> set[str]:
    """Async functions decorated with ``@mcp.tool(...)`` — the server.py inline tool set."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                names.add(node.name)
    return names


def _toplevel_async_defs(source: str) -> set[str]:
    """Top-level async function names — display_tools.py registers each as a tool."""
    return {
        node.name
        for node in ast.iter_child_nodes(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _registered_tool_names() -> set[str]:
    core = _tools_decorated_with_mcp_tool(_SERVER.read_text(encoding="utf-8"))
    # AST-scan display_tools.py (not import) so the set holds with OR without the [displays] extra.
    displays = _toplevel_async_defs(_DISPLAY_TOOLS.read_text(encoding="utf-8"))
    return core | displays


_INVENTORY_RE = re.compile(
    r"<!-- BEGIN:tool-inventory.*?-->(.*?)<!-- END:tool-inventory -->", re.DOTALL
)
# Backticked snake_case tokens (every tool name has an underscore; excludes camelCase REST verbs,
# ``.RTYP``, ``[displays]``, and single words).
_TOOL_TOKEN_RE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
_ENV_RE = re.compile(r"EPICS_MCP_[A-Z][A-Z0-9_]*")


def _guide_tool_tokens() -> set[str]:
    match = _INVENTORY_RE.search(get_guide())
    assert match, "guide tool-inventory markers not found — the drift anchor broke"
    return set(_TOOL_TOKEN_RE.findall(match.group(1)))


def test_guide_tool_inventory_matches_registrations() -> None:
    """Set equality both ways: no dead tool named in the guide (drift), no registered tool missing
    from the guide (the Knowledge-Persistence-Policy's teeth — a new tool must be documented)."""
    guide = _guide_tool_tokens()
    code = _registered_tool_names()
    assert guide == code, (
        f"guide tool inventory ↔ code drift: only-in-guide={sorted(guide - code)} "
        f"only-in-code={sorted(code - guide)}"
    )


def test_guide_env_vars_exist() -> None:
    allowed = {"EPICS_MCP_" + name.upper() for name in EpicsConfig.model_fields}
    mentioned = set(_ENV_RE.findall(get_guide()))
    unknown = mentioned - allowed
    assert not unknown, f"guide mentions unknown EPICS_MCP_* env vars: {sorted(unknown)}"
