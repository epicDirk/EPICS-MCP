"""The strip that keeps return-shape prose off the listed ``outputSchema`` (GQ-224).

Measured at the client on 2026-08-30, the host drops the ``outputSchema`` before a model sees it,
so the docstring FastMCP writes there reached nobody. ``tests/test_server.py`` holds the real
server's listing; this file holds the pure function and proves the transform in BOTH directions on
a throwaway server, so a green listing test is interpretable: without the transform the docstring
is listed, with it not, and the answer still validates against the stripped structure.
"""

from __future__ import annotations

import copy
from typing import Any, TypedDict

import pytest
from fastmcp import Client, FastMCP

from epics_mcp.wire_schema import OutputSchemaProseStrip, strip_schema_prose


class _Answer(TypedDict):
    """Docstring prose of a return shape, which the listing must not carry."""

    description: str
    count: int


def _probe_server(*, strip: bool) -> FastMCP:
    server = FastMCP("probe", transforms=[OutputSchemaProseStrip()] if strip else [])

    @server.tool
    def probe() -> _Answer:
        """Return a fixed answer."""
        return {"description": "kept", "count": 5}

    return server


@pytest.mark.asyncio
async def test_the_strip_works_in_both_directions_and_keeps_the_answer_valid() -> None:
    """The client validates ``structuredContent`` against the LISTED schema, so a call that
    succeeds through it shows the stripped schema still carries the whole structure, a field
    that is itself called ``description`` included."""
    async with Client(_probe_server(strip=False)) as client:
        unstripped = (await client.list_tools())[0].outputSchema or {}
    assert unstripped.get("description"), "FastMCP no longer lists the shape docstring at all"

    async with Client(_probe_server(strip=True)) as client:
        stripped = (await client.list_tools())[0].outputSchema or {}
        result = await client.call_tool("probe", {})
    assert "description" not in stripped
    assert stripped["properties"]["description"] == {"type": "string"}
    assert "description" in stripped["required"]
    assert result.structured_content == {"description": "kept", "count": 5}


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


def test_the_pure_function_keeps_names_literals_and_the_wrap_marker() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "x-fastmcp-wrap-result": True,
        "properties": {
            "description": {"type": "string", "default": {"description": "a value, not prose"}},
            "mode": {"type": "string", "enum": ["description"], "const": "description"},
        },
        "required": ["description"],
    }
    stripped = strip_schema_prose(schema)
    assert stripped["x-fastmcp-wrap-result"] is True
    assert stripped["required"] == ["description"]
    assert stripped["properties"]["description"]["default"] == {"description": "a value, not prose"}
    assert stripped["properties"]["mode"]["enum"] == ["description"]
    assert stripped["properties"]["mode"]["const"] == "description"


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
