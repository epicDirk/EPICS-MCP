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

from epics_pv_mcp.errors import EpicsError

logger = logging.getLogger(__name__)


def translate_epics_errors[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Map errors raised by an async tool to ``ToolError`` with a tagged message.

    * :class:`~epics_pv_mcp.errors.EpicsError` → ``ToolError("[<error_code>] <msg>")``, the
      curated, intentional client-facing text; it reaches the client verbatim (that is its purpose).
    * any other :class:`Exception` → ``ToolError("[INTERNAL] <ClassName>")``, an UNEXPECTED bug.
      Its raw ``str(e)`` can carry an internal detail (a request URL, a live PV name, a filesystem
      path), so only the exception CLASS name is on the wire; the FULL detail (message + traceback)
      is logged SERVER-SIDE at ERROR level for debugging. This keeps the server's redaction posture
      (CF/Alarm/Olog) consistent on the error path without losing debuggability. (Note:
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
            logger.error("[INTERNAL] %s at a tool boundary: %s", type(e).__name__, e, exc_info=True)
            raise ToolError(f"[INTERNAL] {type(e).__name__}") from e

    return wrapper
