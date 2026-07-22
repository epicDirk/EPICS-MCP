"""Translate domain errors into MCP ``ToolError`` at the tool boundary (M4).

Every MCP tool used to repeat the same ``try/except EpicsError → ToolError`` block
verbatim (15×). :func:`translate_epics_errors` centralises it into one decorator, so
the mapping is defined — and unit-tested — in exactly one place and each tool body
becomes a single ``return await _impl(...)``.
"""

import functools
from collections.abc import Awaitable, Callable

from mcp.server.fastmcp.exceptions import ToolError

from epics_pv_mcp.errors import EpicsError


def translate_epics_errors[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Map errors raised by an async tool to ``ToolError`` with a tagged message.

    * :class:`~epics_pv_mcp.errors.EpicsError` → ``ToolError("[<error_code>] <msg>")``
    * any other :class:`Exception` → ``ToolError("[INTERNAL] <ClassName>: <msg>")``

    ``functools.wraps`` preserves the wrapped signature, so FastMCP still builds the
    tool schema (parameter types, Field defaults) from the original function.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except EpicsError as e:
            raise ToolError(f"[{e.error_code}] {e}") from e
        except Exception as e:
            raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e

    return wrapper
