"""GQ-18 (c): the claims this repository's DELIVERED channels were made to carry.

WHY THIS FILE EXISTS. Four facts about this server were parked in a foreign repository's
``references/`` folder, where nothing this server ships ever reaches them. They were measured
against the delivered channel (``tools/list``, the ``initialize`` instructions, the operator guide
and the shipped ``docs/``), and the ones the channel did not carry moved INTO it. A sentence moved
into a description is prose, and prose is the category that rots, so every claim added that way is
pinned here.

TWO KINDS OF PIN, deliberately separated, because they answer different questions:

* **A wire pin** asserts the sentence is still in the channel a client receives. It reads
  ``mcp.list_tools()``, never the docstring, because the docstring is the SOURCE and the wire is
  the DELIVERY, and a decorator that stops forwarding the text would leave a docstring assertion
  green. Whitespace-normalised: the description is a wrapped docstring, so pinning line breaks
  would go red on a harmless re-wrap.
* **A measuring proof** asserts the SUBJECT of the sentence is still true. A wire pin alone only
  proves the sentence is present, which is exactly the failure mode of documentation: the words
  survive the behaviour. Each one below names the mutation that reddens it.

⚠️ **One claim has no measuring proof, and it is named rather than quietly skipped.** The
``set_pv_value`` display boundary is a statement about ANOTHER surface (a Phoebus operator screen
writes through its widget). This repository holds no body that could go red for it, so it carries a
wire pin only. Calling that a complete guard would be the sham this repository's evidence
discipline is written against.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from epics_mcp.presets import PRESETS


async def _description(tool_name: str) -> str:
    """The whitespace-normalised description as a CLIENT receives it, not as it is written."""
    from epics_mcp.server import mcp

    tools = {t.name: t for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    return " ".join((tools[tool_name].description or "").split())


# --- set_pv_value: the display boundary ------------------------------------------------------


@pytest.mark.asyncio
async def test_set_pv_value_says_a_display_writes_through_its_widget() -> None:
    """Wire pin (GQ-18 (c), unit 1): writing from an operator screen is NOT this tool.

    The parked sentence was a routing statement, the kind that protects against reaching for the
    wrong surface, and the delivered channel carried nothing of it: "written by the widget" had
    zero hits across the descriptions and the whole operator guide, whose only "widget" is the
    Data Browser one. Without it, an assistant asked to change a value it can see on a screen
    reaches for the gated tool and is refused, instead of using the screen.

    ⚠️ NO measuring proof exists here and that is a property of the claim, not an omission: the
    behaviour it describes belongs to Phoebus, not to this server. See the module docstring.
    """
    description = await _description("set_pv_value")
    assert "writes a channel directly" in description, (
        "the direct-write half of the boundary is gone"
    )
    assert "operator DISPLAY is put by the widget" in description, (
        "the display half of the boundary is gone: the sentence that keeps a caller off this tool "
        "when a screen is what they are looking at"
    )


# --- the presets: none of them arms a write gate ----------------------------------------------

#: The two independent write gates. Named here rather than imported from a gate module on purpose:
#: this test asks what a PRESET writes into a client configuration, which is a question about
#: strings in `presets.py`, not about the gate implementations.
_GATE_VARS = ("EPICS_MCP_ALLOW_PV_WRITE", "EPICS_MCP_ALLOW_OLOG_WRITE")


def test_no_preset_arms_a_write_gate() -> None:
    """Measuring proof for the docs/tools.md claim (GQ-18 (c), unit 1).

    The parked text warned that the ``sandbox`` PRESET is a read posture and does not switch
    writing on. Measured, the delivered channel said nothing at all about presets, and the
    misreading it guards against is a natural one: ``sandbox`` is the one shape with nothing left
    to fill in, which reads like "the one where you can try things".

    This is the half a wire pin cannot give: it goes red if a preset ever starts shipping a gate
    variable, which is the moment the documented sentence becomes false. Over ALL presets, not
    just ``sandbox``, because the sentence is universal and a claim about four shapes tested on
    one is the all-quantifier error this repository has recorded before.

    Provably red: give any entry of ``PRESETS`` an ``EPICS_MCP_ALLOW_PV_WRITE`` entry.
    """
    # The SENTENCE, not only the fact. Found in this build's own QA round: the guard below holds
    # the behaviour, and nothing held the text, so deleting the paragraph from docs/tools.md would
    # have left every test green while the delivered channel lost the statement again, which is
    # the exact defect this whole piece of work exists to repair. The wire claims are pinned on
    # their descriptions; this is the same pin for the channel that ships as a file.
    tools_md = Path(__file__).resolve().parent.parent / "docs" / "tools.md"
    assert "No preset arms a write gate" in tools_md.read_text(encoding="utf-8"), (
        "docs/tools.md no longer carries the preset/write-gate sentence"
    )

    assert PRESETS, "no presets at all: this guard would pass vacuously"
    for name, preset in PRESETS.items():
        for gate_var in _GATE_VARS:
            assert gate_var not in preset.env, (
                f"preset {name!r} sets {gate_var}, so docs/tools.md's 'No preset arms a write "
                f"gate' is now false; either drop it from the preset or fix the sentence"
            )


# --- get_pvs: the engineering unit rides in the batch ------------------------------------------


class _BatchContext:
    """Stands in for the p4p Context on the BATCH path: ``.get`` takes a LIST and answers a list.

    Faked at the transport seam rather than at a client class, deliberately: the claim under test
    is about a field mapping (``epics_client._DISPLAY_SPEC``) that runs INSIDE the client, so a
    class-level double would remove the very code the assertion is about and leave it green. The
    repository's evidence discipline records that failure mode.
    """

    def __init__(self, values: list[object]) -> None:
        self._values = values

    def get(self, names: list[str], timeout: float | None = None) -> list[object]:
        return self._values


@pytest.mark.asyncio
async def test_get_pvs_says_the_unit_rides_in_display_and_it_does(monkeypatch: Any) -> None:
    """Wire pin AND measuring proof for GQ-18 (c) unit 3, in one test on purpose.

    The parked line said a batch read shows channels that are labelled alike and carry different
    engineering units. The delivered description named the metadata only by referring to
    get_pv_info, so a caller asking "do these share a unit?" had to take a second hop to find out
    that this tool answers it at all.

    The proof drives the REAL batch path with two channels sharing a description and differing in
    ``units``, which is the situation the sentence exists for. Asserting the wire text alone would
    survive the field being dropped from the mapping.

    Provably red, two ways: delete the sentence from the docstring (wire half), or remove
    ``("units", "units", str)`` from ``epics_client._DISPLAY_SPEC`` (behaviour half).
    """
    from epics_mcp.services import epics_client
    from epics_mcp.tools.read import _get_pvs

    description = await _description("get_pvs")
    assert "units and precision ride inside display" in description, (
        "the field location is gone: a caller is back to a second hop through get_pv_info"
    )
    assert "carry the same engineering unit" in description, (
        "the question this tool answers in one call is no longer named"
    )

    same_label = "Cavity forward power"
    monkeypatch.setattr(
        epics_client,
        "get_context",
        lambda: _BatchContext(
            [
                SimpleNamespace(
                    raw=SimpleNamespace(
                        value=1.0,
                        display=SimpleNamespace(units="W", precision=2, description=same_label),
                    )
                ),
                SimpleNamespace(
                    raw=SimpleNamespace(
                        value=2.0,
                        display=SimpleNamespace(units="dBm", precision=1, description=same_label),
                    )
                ),
            ]
        ),
    )

    out = await _get_pvs(["SIM:RF-01:Pwr-R", "SIM:RF-02:Pwr-R"])

    results = out["results"]
    assert isinstance(results, list) and len(results) == 2
    displays: list[dict[str, object]] = []
    for result in results:
        block = result["display"]
        # .get below rather than [], so a dropped field fails on the message that explains it
        # instead of on a bare KeyError. Measured: the first version of this test reddened with
        # "KeyError: 'units'", which is red but tells the next reader nothing.
        assert isinstance(block, dict)
        displays.append(block)
    assert [d.get("units") for d in displays] == ["W", "dBm"], (
        "units no longer reaches the batch result, so the description's promise is false"
    )
    assert [d.get("precision") for d in displays] == [2, 1], (
        "precision no longer reaches the result"
    )
    assert {d.get("description") for d in displays} == {same_label}, (
        "the shared label is what makes the differing units worth reporting"
    )
