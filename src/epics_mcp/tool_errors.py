"""Translate domain errors into MCP ``ToolError`` at the tool boundary (M4).

Every MCP tool used to repeat the same ``try/except EpicsError → ToolError`` block
verbatim (15×). :func:`translate_epics_errors` centralises it into one decorator, so
the mapping is defined, and unit-tested, in exactly one place and each tool body
becomes a single ``return await _impl(...)``.
"""

import functools
import logging
from collections.abc import Awaitable, Callable

from fastmcp.exceptions import ToolError

from epics_mcp.errors import EpicsError

logger = logging.getLogger(__name__)


def translate_epics_errors[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Map errors raised by an async tool to ``ToolError`` with a tagged message.

    * :class:`~epics_mcp.errors.EpicsError` → ``ToolError("[<error_code>] <msg>")``, the
      curated, intentional client-facing text; it reaches the client verbatim (that is its purpose).
    * any other :class:`Exception` → ``ToolError("[INTERNAL] <ClassName>")``, an UNEXPECTED bug.
      Its raw ``str(e)`` can carry an internal detail (a request URL, a live PV name, a filesystem
      path), so only the exception CLASS name is on the wire; the FULL detail (message + traceback)
      is logged SERVER-SIDE at ERROR level for debugging. What every tool answers with is a
      CURATED payload, and an unexpected traceback is the one path that could bypass that; this
      closes it without losing debuggability. (Note:
      ``mask_error_details`` on the FastMCP constructor does NOT cover this, a ToolError message
      always reaches the client; the confinement has to happen HERE, where the message is built.)

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
            # Full detail stays server-side (message via %s + traceback via exc_info); the client
            # gets only the class name so an unexpected bug cannot leak an internal string.
            #
            # ⚠️ The detail this line writes CAN be a service URL with credentials in it, and that
            # is a decided posture rather than an oversight (BG-DERR-B, 2026-08-14). The redaction
            # posture of this server governs what travels toward the CLIENT, a tool answer or a
            # resource payload the client keeps; the log is a different channel with a different
            # reader. Dropping exc_info would not even close it (the %s argument carries the same
            # string, measured) and would cost the debuggability of the one path where the caller
            # is told nothing but a class name. What that means for an operator, and what they can
            # do about it, is stated in docs/safety.md and SECURITY.md; the assertion that pins it
            # is tests/test_tool_errors.py::test_generic_exception_is_confined_to_the_class_name_
            # and_logged, which checks BOTH halves, nothing on the wire and the detail in the log.
            logger.error("[INTERNAL] %s at a tool boundary: %s", type(e).__name__, e, exc_info=True)
            raise ToolError(f"[INTERNAL] {type(e).__name__}") from e

    return wrapper
