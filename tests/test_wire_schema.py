"""The strip that keeps return-shape prose off the listed ``outputSchema`` (GQ-224).

Measured at the client on 2026-08-30 (one host), the host drops the ``outputSchema`` before a
model sees it, so on that host the docstring FastMCP writes there reached nobody.
``tests/test_server.py`` holds that the real listing carries no prose; this file holds that the
listing keeps the whole STRUCTURE, the pure function case by case, and the transform in BOTH
directions on a throwaway server, so a green listing test is interpretable.
"""

from __future__ import annotations

import copy
from typing import Any, TypedDict

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.json_schema import dereference_refs

from epics_mcp.server import mcp
from epics_mcp.wire_schema import OutputSchemaProseStrip, strip_schema_prose
from tests.wire_tools import wire_tools


def _drop_every_description(node: object) -> object:
    """NAIVE: every ``description`` key removed, wherever it stands. Independent of the strip."""
    if isinstance(node, dict):
        return {k: _drop_every_description(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_drop_every_description(item) for item in node]
    return node


def _expected_listing(registered: dict[str, Any] | None) -> object:
    """What the listing should carry for a registered schema: resolved, minus every description."""
    return None if registered is None else _drop_every_description(dereference_refs(registered))


@pytest.mark.asyncio
async def test_the_listing_keeps_the_whole_structure_of_every_output_schema() -> None:
    """The listed schema of every tool equals its registered schema with only ``description``
    removed. Resolved the way the listing resolves it (``dereference_refs``, the middleware's
    step), and compared as a whole, so a strip that dropped an ``items``, an ``anyOf`` or a
    ``required`` would show here. A property NAMED ``description`` would make the naive side
    differ; none exists today, and whoever adds one adjusts this comparison consciously."""
    registered = {tool.name: tool.output_schema for tool in await mcp.local_provider.list_tools()}
    deviating = [
        tool.name
        for tool in await wire_tools()
        if tool.outputSchema != _expected_listing(registered[tool.name])
    ]
    assert not deviating, f"listed outputSchema differs in more than its prose: {deviating}"


class _Item(TypedDict):
    """A nested shape, so the structure the proof keeps is more than one flat object."""

    name: str


class _Answer(TypedDict):
    """Docstring prose of a return shape, which the listing must not carry."""

    description: str
    count: int
    items: list[_Item]


def _probe_server(*, strip: bool) -> FastMCP:
    server = FastMCP("probe", transforms=[OutputSchemaProseStrip()] if strip else [])

    @server.tool
    def probe() -> _Answer:
        """Return a fixed answer."""
        return {"description": "kept", "count": 5, "items": [{"name": "a"}]}

    @server.tool
    def wrong() -> _Answer:
        """Return an answer that does not fit its own shape."""
        return {"description": "kept", "count": "five", "items": [{"name": 1}]}  # type: ignore[typeddict-item]

    return server


@pytest.mark.asyncio
async def test_the_strip_works_in_both_directions_and_the_answer_is_still_validated() -> None:
    """Without the transform the docstring is listed, with it not. The client validates
    ``structuredContent`` against the LISTED schema: a fitting answer passes, and an answer of
    the wrong type is still refused, so the stripped schema still constrains what it did."""
    async with Client(_probe_server(strip=False)) as client:
        unstripped = (await client.list_tools())[0].outputSchema or {}
    assert unstripped.get("description"), "FastMCP no longer lists the shape docstring at all"

    server = _probe_server(strip=True)
    async with Client(server) as client:
        stripped = (await client.list_tools())[0].outputSchema or {}
        result = await client.call_tool("probe", {})
        with pytest.raises(ToolError, match=r"(?i)validat"):
            await client.call_tool("wrong", {})
    assert "description" not in stripped
    assert "description" not in stripped["properties"]["items"]["items"]
    assert stripped["properties"]["description"] == {"type": "string"}
    assert "description" in stripped["required"]
    assert result.structured_content == {
        "description": "kept",
        "count": 5,
        "items": [{"name": "a"}],
    }
    fetched = await server.get_tool("probe")
    assert fetched is not None and "description" not in (fetched.output_schema or {})


def test_the_pure_function_strips_every_schema_node() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "description": "root",
        "properties": {
            "listed": {"type": "array", "items": {"type": "object", "description": "item"}},
            "either": {"anyOf": [{"type": "string", "description": "branch"}, {"type": "null"}]},
            "mapping": {
                "type": "object",
                "additionalProperties": {"type": "integer", "description": "value"},
                "patternProperties": {"^x": {"type": "string", "description": "pattern"}},
            },
            "ref": {"$ref": "#/$defs/Reach"},
        },
        "$defs": {"Reach": {"type": "object", "description": "which planes answered"}},
    }
    assert "description" not in repr(strip_schema_prose(schema))


def test_the_pure_function_keeps_names_literals_and_vendor_keys() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "x-fastmcp-wrap-result": True,
        "x-vendor": {"description": "vendor data"},
        "properties": {
            "description": {"type": "string", "default": {"description": "a value, not prose"}},
            "mode": {"type": "string", "enum": ["description"], "const": "description"},
        },
        "required": ["description"],
        "dependentRequired": {"description": ["mode"]},
        "dependencies": {"description": {"required": ["mode"], "description": "prose"}},
        "discriminator": {"propertyName": "mode", "mapping": {"description": "#/$defs/A"}},
    }
    stripped = strip_schema_prose(schema)
    assert stripped["x-fastmcp-wrap-result"] is True
    assert stripped["x-vendor"] == {"description": "vendor data"}
    assert stripped["required"] == ["description"]
    assert stripped["properties"]["description"]["default"] == {"description": "a value, not prose"}
    assert stripped["properties"]["mode"]["enum"] == ["description"]
    assert stripped["properties"]["mode"]["const"] == "description"
    assert stripped["dependentRequired"] == {"description": ["mode"]}
    assert stripped["dependencies"] == {"description": {"required": ["mode"]}}
    assert stripped["discriminator"]["mapping"] == {"description": "#/$defs/A"}


def test_the_pure_function_leaves_its_input_untouched() -> None:
    schema: dict[str, Any] = {"type": "object", "description": "root", "properties": {}}
    before = copy.deepcopy(schema)
    strip_schema_prose(schema)
    assert schema == before


@pytest.mark.asyncio
async def test_a_tool_without_an_output_schema_passes_unchanged() -> None:
    server = FastMCP("probe")

    @server.tool(output_schema=None)
    def untyped() -> dict[str, object]:
        """Return nothing typed."""
        return {}

    tools = list(await server.list_tools())
    assert tools[0].output_schema is None
    assert list(await OutputSchemaProseStrip().list_tools(tools)) == tools
