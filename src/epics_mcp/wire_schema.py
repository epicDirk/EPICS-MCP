"""Keep the prose of return shapes off the ``outputSchema`` this server lists.

FastMCP derives each typed tool's ``outputSchema`` from its return annotation, and pydantic writes
the docstring of every TypedDict it meets there into that schema as ``description``: here that is
the one-line docstring of ``provenance.Reach``, repeated on every tool whose answer carries a
``reach`` field. Measured at the client on 2026-08-30 (three windows, positive control over the
``inputSchema``), the host hands a model the name, the description and the parameters of a tool
and drops the ``outputSchema`` wholesale, so that prose reached no model. What a caller has to act
on belongs in the tool description or in the server instructions, which arrive.

The docstrings stay in the code for the human reading it; only the listed schema loses them. The
structure stays whole, because the SDK validates ``structuredContent`` against it over the wire,
and in JSON Schema ``description`` is an annotation without any effect on validation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.server.transforms import Transform

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastmcp.server.transforms import GetToolNext, VersionSpec
    from fastmcp.tools import Tool

# Keywords whose value maps a NAME to a sub-schema. The names are data: a field may well be called
# ``description`` (a log entry has one), and stripping it would drop the field with its
# ``required`` entry.
_NAMED_SUBSCHEMAS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)

# Keywords whose value is literal data, never a schema: a ``default`` shaped like
# ``{"description": ...}`` is a value and stays as it is.
_LITERALS = frozenset({"default", "const", "enum", "examples"})


def strip_schema_prose(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *schema* without any ``description`` on a schema node.

    Pure: the input is left untouched. Only ``description`` is removed. ``title`` is already pruned
    by FastMCP itself, and ``test_output_schema_fields_carry_no_title_annotation`` stays the net
    that catches it coming back, which a silent strip here would disarm.
    """
    return _strip_node(schema)


def _strip_node(node: dict[str, Any]) -> dict[str, Any]:
    stripped: dict[str, Any] = {}
    for key, value in node.items():
        if key == "description":
            continue
        if key in _LITERALS:
            stripped[key] = value
        elif key in _NAMED_SUBSCHEMAS and isinstance(value, dict):
            stripped[key] = {name: _strip_value(sub) for name, sub in value.items()}
        else:
            stripped[key] = _strip_value(value)
    return stripped


def _strip_value(value: object) -> object:
    if isinstance(value, dict):
        return _strip_node(value)
    if isinstance(value, list):
        return [_strip_value(item) for item in value]
    return value


def _without_output_prose(tool: Tool) -> Tool:
    if tool.output_schema is None:
        return tool
    return tool.model_copy(update={"output_schema": strip_schema_prose(tool.output_schema)})


class OutputSchemaProseStrip(Transform):
    """Lists every tool with the prose stripped from its ``outputSchema``.

    A transform rather than a pass over the registered tools: it works on copies at listing time,
    so nothing is mutated at import, and it also covers the display-aware tools that
    ``register_display_tools`` adds after construction. ``get_tool`` applies the same strip, so
    the tool a call runs carries the structure the listing advertised.
    """

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [_without_output_prose(tool) for tool in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return None if tool is None else _without_output_prose(tool)
