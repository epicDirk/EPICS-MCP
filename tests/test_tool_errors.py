"""Tests for the translate_epics_errors decorator (M4/C4).

The 15 MCP tool wrappers delegate their error translation to this one decorator, so
its two branches (EpicsError → tagged code, anything else → INTERNAL) are covered here
in one place instead of 15× untested.
"""

import pytest
from fastmcp.exceptions import ToolError

from epics_pv_mcp.errors import EpicsError, PVNotFoundError
from epics_pv_mcp.tool_errors import translate_epics_errors


async def test_epics_error_becomes_tool_error_with_code() -> None:
    @translate_epics_errors
    async def boom() -> str:
        raise PVNotFoundError("PV 'X' not found")

    with pytest.raises(ToolError, match=r"\[PV_NOT_FOUND\] PV 'X' not found"):
        await boom()


async def test_bounds_error_renders_its_own_code() -> None:
    """O2: PVWriteBoundsError subclasses PVWriteDeniedError but must render its OWN error_code
    (PV_WRITE_OUT_OF_BOUNDS), not the parent's PV_WRITE_DENIED. The __init__ override is the
    trap this pins."""
    from epics_pv_mcp.errors import PVWriteBoundsError

    @translate_epics_errors
    async def boom() -> str:
        raise PVWriteBoundsError("out of range")

    with pytest.raises(ToolError, match=r"\[PV_WRITE_OUT_OF_BOUNDS\] out of range"):
        await boom()


async def test_generic_exception_becomes_internal_tool_error() -> None:
    @translate_epics_errors
    async def boom() -> str:
        raise ValueError("weird")

    with pytest.raises(ToolError, match=r"\[INTERNAL\] ValueError: weird"):
        await boom()


async def test_success_passes_through_and_preserves_signature() -> None:
    @translate_epics_errors
    async def ok(a: int, b: int = 2) -> int:
        return a + b

    assert await ok(3) == 5
    assert await ok(3, b=4) == 7
    # functools.wraps keeps the wrapped function's identity so FastMCP can still
    # introspect the original signature to build the tool schema.
    assert ok.__name__ == "ok"
    assert ok.__wrapped__.__name__ == "ok"  # type: ignore[attr-defined]


async def test_base_error_code_default() -> None:
    """A plain EpicsError carries its default error_code (UNKNOWN) into the tag —
    NOT the INTERNAL branch, which is reserved for non-EpicsError exceptions."""

    @translate_epics_errors
    async def boom() -> str:
        raise EpicsError("nope")

    with pytest.raises(ToolError, match=r"\[UNKNOWN\] nope"):
        await boom()


async def test_decorator_raises_the_mcp_runtime_tool_error() -> None:
    """Q1: pin that the decorator raises the RUNTIME's ToolError — the class the server runs on.
    Since the standalone-fastmcp migration that is ``fastmcp.exceptions.ToolError`` (server.py
    builds on ``from fastmcp import FastMCP``). The SDK-bundled, identically-named
    ``mcp.server.fastmcp.exceptions.ToolError`` is now the FOREIGN class the runtime no longer
    knows — raising it would defeat the tool-boundary translation. The module is pinned so an
    import can't silently regress back to it."""
    from fastmcp.exceptions import ToolError as RuntimeToolError

    @translate_epics_errors
    async def boom() -> str:
        raise PVNotFoundError("nope")

    with pytest.raises(RuntimeToolError) as exc_info:
        await boom()
    assert type(exc_info.value).__module__ == "fastmcp.exceptions"
