"""The names of the display-gated tools, read from the source WITHOUT importing it.

``src/epics_mcp/display_tools.py`` imports ``opi_navigation``, which a core-only install does not
have, so any test that needs to know WHICH tools are display-gated has to read the file rather than
import it. Two modules asked that question independently; this is their one home, and it is
deliberately import-cheap: a test that only needs the four names must not pay for the whole server
stack to learn them.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The file that defines the [displays]-extra tools. AST-scanned, NEVER imported.
DISPLAY_TOOLS_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "epics_mcp" / "display_tools.py"
)


def display_tool_names() -> set[str]:
    """Top-level ``async def`` names in display_tools.py (the [displays]-extra tools)."""
    source = DISPLAY_TOOLS_SRC.read_text(encoding="utf-8")
    return {
        node.name
        for node in ast.iter_child_nodes(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
    }
