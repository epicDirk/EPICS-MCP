"""Drift guard: the tool ARGUMENT contract, the schema, and the prose that teaches it.

Background (2026-07-25, commit ``9256977``): the tools unified the argument naming "the PV this
tool is about", ``pv``/``name`` → ``pv_name``, ``pvs``/``names`` → ``pv_names``. That is BREAKING
for a client calling by argument name, so the new names have to be pinned, and so does every
surface that TEACHES them.

Both halves had already failed once, and this module is the guard for each:

* The suite pinned only 5 of the 8 renamed tools. Measured by mutation: reverting ``get_pvs``,
  ``monitor_pv`` or ``validate_pvs`` to its old argument name left the whole suite GREEN.
* Two SHIPPED surfaces kept teaching a retired name, the ``compare_machine_state`` prompt
  (``get_pvs(names=[...])``) and the operator guide served as ``epics-pv://guide``
  (``get_alarm_history`` ... ``pv``). A client that follows either builds a call the server rejects.

Three guards, in the order the failure happened:

1. the wire contract itself (``inputSchema``), including the absence of the retired names;
2. every ``tool(keyword=...)`` example in the prose, checked against the real schema, this one
   generalizes to a future rename, because the tool set comes from the live registry;
3. the retired names of THIS rename, which must not appear beside their tool in prose that carries
   no call syntax (exactly how the guide's occurrence was phrased).

Honest limits, so nobody reads more into this than it proves:

* Guard 3 is a pin for a KNOWN rename. Its table is hand-maintained, a FUTURE rename is not
  covered until someone extends it.
* Guard 3's proximity window is a heuristic, not a parser. 200 characters was chosen BY
  MEASUREMENT: across the current guide, README and every prompt variant it flags exactly the
  mentions of the one real defect and nothing else, in particular NOT the Archiver ``getAllPVs``
  recipe, which legitimately documents a FOREIGN REST API's own ``pv`` query parameter.
* Guard 2 only sees keyword-call syntax. A positional example (``get_pv_info("PV")``) carries no
  argument name and is out of scope by construction.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from epics_pv_mcp.prompts import compare_machine_state, diagnose_pv
from epics_pv_mcp.resources import get_guide
from epics_pv_mcp.server import _DISPLAY_TOOLS_AVAILABLE, mcp

_README = Path(__file__).resolve().parent.parent / "README.md"

#: The scalar "the PV this tool is about" argument.
_SINGLE_PV_ARG = "pv_name"
_SINGLE_PV_TOOLS: tuple[str, ...] = (
    "get_pv_value",
    "set_pv_value",
    "get_pv_info",
    "diagnose_connection",
    "monitor_pv",
    "is_archived",
    "get_pv_history",
    "get_archive_info",
    "is_alarm_configured",
    "get_alarm_history",
)

#: The PV-LIST argument. ``validate_pvs`` takes EITHER a PV list OR a .bob path, so the list is
#: optional there, the name is pinned for both, "required" only where it genuinely is.
_PV_LIST_ARG = "pv_names"
_PV_LIST_TOOLS: tuple[str, ...] = ("get_pvs", "validate_pvs")
_PV_LIST_REQUIRED: frozenset[str] = frozenset({"get_pvs"})

#: Deliberately NOT renamed by 9256977: a DEVICE name (not a PV) and the glob/search parameters.
#: Pinned so a later "consistency" sweep cannot quietly fold them into the pv_name family.
_UNCHANGED_ARGS: Mapping[str, str] = {
    "lookup_device_name": "name",
    "discover_pvs": "pattern",
    "list_archived_pvs": "pattern",
    "find_channels": "name_pattern",
    "find_device": "query",
}

#: The retired argument name(s) per tool, must survive neither in a schema nor in the prose.
_RETIRED_ARGS: Mapping[str, tuple[str, ...]] = {
    "monitor_pv": ("name",),
    "is_archived": ("pv",),
    "get_pv_history": ("pv",),
    "get_archive_info": ("pv",),
    "is_alarm_configured": ("pv",),
    "get_alarm_history": ("pv",),
    "get_pvs": ("names",),
    "validate_pvs": ("pvs",),
}

#: Characters after a tool mention within which a retired name counts as "taught by" that tool.
_RETIRED_WINDOW = 200

#: Registered only with the optional ``[displays]`` extra, absent in a core-only install, where
#: their rows are skipped instead of failing (mirrors server.py's own registration condition).
_DISPLAY_GATED: frozenset[str] = frozenset({"validate_pvs", "find_device"})


async def _input_schemas() -> dict[str, dict[str, Any]]:
    """``tool name → inputSchema`` as it goes on the WIRE (not as the source reads)."""
    return {tool.name: (tool.to_mcp_tool().inputSchema or {}) for tool in await mcp.list_tools()}


def _properties(schema: Mapping[str, Any]) -> set[str]:
    return set(schema.get("properties") or {})


def _required(schema: Mapping[str, Any]) -> set[str]:
    return set(schema.get("required") or [])


def _expected_here(tool: str) -> bool:
    """False only for a display-gated tool in a core-only install."""
    return _DISPLAY_TOOLS_AVAILABLE or tool not in _DISPLAY_GATED


def _prose_surfaces() -> dict[str, str]:
    """Every SHIPPED surface that teaches a client how to call a tool.

    The prompts are rendered in all their branches: the branch is chosen by the install's display
    capability, so a stale example could otherwise hide in the branch this install does not take.
    """
    return {
        "operator_guide.md (epics-pv://guide)": get_guide(),
        "README.md": _README.read_text(encoding="utf-8"),
        "prompt diagnose_pv": diagnose_pv("SIM:PS-01:Cur-RB"),
        "prompt compare_machine_state (displays, file)": compare_machine_state(
            "SIM:PS-01", reference_file="status.bob", display_tools_available=True
        ),
        "prompt compare_machine_state (core-only, file)": compare_machine_state(
            "SIM:PS-01", reference_file="status.bob", display_tools_available=False
        ),
        "prompt compare_machine_state (no file)": compare_machine_state(
            "SIM:PS-01", display_tools_available=True
        ),
    }


# --- Guard 1: the wire contract ---------------------------------------------------------------


async def test_single_pv_tools_take_pv_name() -> None:
    schemas = await _input_schemas()
    for tool in _SINGLE_PV_TOOLS:
        assert tool in schemas, f"{tool} is not registered"
        props = _properties(schemas[tool])
        assert _SINGLE_PV_ARG in props, (
            f"{tool} lost its {_SINGLE_PV_ARG} argument: {sorted(props)}"
        )
        assert _SINGLE_PV_ARG in _required(schemas[tool]), (
            f"{tool}.{_SINGLE_PV_ARG} must stay REQUIRED, an optional PV would let a caller "
            f"omit the very thing the tool is about"
        )


async def test_pv_list_tools_take_pv_names() -> None:
    schemas = await _input_schemas()
    for tool in _PV_LIST_TOOLS:
        if not _expected_here(tool):
            continue
        assert tool in schemas, f"{tool} is not registered"
        props = _properties(schemas[tool])
        assert _PV_LIST_ARG in props, f"{tool} lost its {_PV_LIST_ARG} argument: {sorted(props)}"
        if tool in _PV_LIST_REQUIRED:
            assert _PV_LIST_ARG in _required(schemas[tool])


async def test_no_schema_carries_a_retired_argument_name() -> None:
    schemas = await _input_schemas()
    for tool, retired in _RETIRED_ARGS.items():
        if not _expected_here(tool):
            continue
        assert tool in schemas, f"{tool} is not registered"
        props = _properties(schemas[tool])
        for old in retired:
            assert old not in props, f"{tool} re-grew its retired argument {old!r}: {sorted(props)}"


async def test_deliberately_unchanged_arguments_kept_their_names() -> None:
    schemas = await _input_schemas()
    for tool, arg in _UNCHANGED_ARGS.items():
        if not _expected_here(tool):
            continue
        assert tool in schemas, f"{tool} is not registered"
        assert arg in _properties(schemas[tool]), (
            f"{tool}.{arg} was renamed, 9256977 deliberately kept it: a DEVICE name and the "
            f"glob/search parameters are not 'the PV this tool is about'"
        )


# --- Guard 2: every documented keyword call --------------------------------------------------


async def test_documented_keyword_calls_match_the_real_schema() -> None:
    schemas = await _input_schemas()
    # Longest-first so `get_pv_value` is never matched as a prefix of a longer tool name.
    alternation = "|".join(sorted(schemas, key=len, reverse=True))
    offenders: list[str] = []
    checked = 0
    for surface, text in _prose_surfaces().items():
        for call in re.finditer(rf"\b({alternation})\(([^)]*)\)", text):
            tool = call.group(1)
            for keyword in re.findall(r"(\w+)\s*=", call.group(2)):
                checked += 1
                if keyword not in _properties(schemas[tool]):
                    offenders.append(
                        f"{surface}: {tool}({keyword}=...), the tool accepts "
                        f"{sorted(_properties(schemas[tool]))}"
                    )
    assert not offenders, "a shipped surface teaches an argument the tool rejects:\n" + "\n".join(
        offenders
    )
    # Non-empty floor (decision LJ): a regex that silently stops matching would make this
    # guard vacuously green forever.
    assert checked >= 4, f"only {checked} keyword arguments found, the scan is probably broken"


# --- Guard 3: retired names in prose without call syntax --------------------------------------


async def test_retired_argument_names_are_gone_from_the_prose() -> None:
    schemas = await _input_schemas()
    for tool in _RETIRED_ARGS:
        if _expected_here(tool):
            assert tool in schemas, f"_RETIRED_ARGS names {tool}, which is not a registered tool"

    offenders: list[str] = []
    for surface, text in _prose_surfaces().items():
        for tool, retired in _RETIRED_ARGS.items():
            for mention in re.finditer(rf"`?\b{tool}\b`?", text):
                window = text[mention.end() : mention.end() + _RETIRED_WINDOW]
                for old in retired:
                    hit = re.search(rf"`{old}`", window)
                    if hit is None:
                        continue
                    line = text[: mention.start()].count("\n") + 1
                    offenders.append(
                        f"{surface}:{line}: `{old}` appears {hit.start()} chars after "
                        f"{tool}, which now takes "
                        f"{_PV_LIST_ARG if tool in _PV_LIST_TOOLS else _SINGLE_PV_ARG}"
                    )
    assert not offenders, "a shipped surface still names a retired argument:\n" + "\n".join(
        offenders
    )
