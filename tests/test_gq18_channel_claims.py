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
    assert PRESETS, "no presets at all: this guard would pass vacuously"
    for name, preset in PRESETS.items():
        for gate_var in _GATE_VARS:
            assert gate_var not in preset.env, (
                f"preset {name!r} sets {gate_var}, so docs/tools.md's 'No preset arms a write "
                f"gate' is now false; either drop it from the preset or fix the sentence"
            )
