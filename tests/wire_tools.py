"""The registered tools as they go over the WIRE, in one place instead of five spellings.

``mcp.list_tools()`` answers FastMCP's own ``Tool`` objects; what a client receives is the
``mcp.types.Tool`` each of them serialises to via ``to_mcp_tool()``. Every test that asserts
something about a name, a description or a schema has to make that conversion first, and the
suite made it in five literally different ways at 43 places in ``test_server.py`` alone, plus
eight more across seven other modules. All five did the same thing, so the differences carried no
information: they were the residue of the site being copied from whichever neighbour was nearest.

⚠ ``mcp`` is imported inside the functions, not at module level, and that is not style. It pulls
in the whole server stack and through it p4p, numpy and OpenBLAS; a module-level import here would
make every importer of this file pay for that, including collection-time readers that only want a
tool name. ``tests/conftest.py`` sets the OpenBLAS thread-arena default for the same reason and
pins the closure that keeps it working.

⚠ Deliberately NOT a fixture. Roughly a third of the call sites sit in tests that take no other
fixture, and a fixture would also make the conversion invisible at the point where a reader is
asking what exactly is being asserted about.
"""

from __future__ import annotations

from mcp.types import Tool


async def wire_tools() -> list[Tool]:
    """Every registered tool, converted to the wire type a client actually sees."""
    from epics_mcp.server import mcp

    return [tool.to_mcp_tool() for tool in await mcp.list_tools()]


async def wire_tools_by_name() -> dict[str, Tool]:
    """The same tools keyed by name, for the sites that look one up rather than walk them all.

    A second function rather than a ``by_name`` flag: the return types differ, and a flag would
    make every caller's type depend on an argument value, which ``mypy --strict`` can only follow
    through an overload nobody needs here.
    """
    return {tool.name: tool for tool in await wire_tools()}
