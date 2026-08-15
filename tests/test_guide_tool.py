"""Guards over ``get_guide``, the channel a MODEL pulls the operator guide from.

**Why the tool exists although the same text has been ``epics-pv://guide`` since E1.** The
protocol has three server elements with three controllers: tools are MODEL-controlled, resources
APPLICATION-controlled, prompts USER-controlled. Whatever the model is meant to fetch by itself
has to be a tool; a resource is not a channel it pulls from. The resource was therefore correct,
complete, drift-guarded, and never fetched.

**What hangs on it.** Knowledge may not leave a surface's always-on prose before its target
channel EXISTS rather than being planned. These assertions are what makes "exists" checkable.

**These guards check BEHAVIOUR, not wording, with two deliberate exceptions**, and both are
promises made to a caller rather than notes to ourselves: that the header names the tool at all,
and that the argument description states what an empty string does. A promise only the internal
docstring carries is a promise the caller never sees.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from epics_mcp.errors import GuideDriftError, UnknownTopicError
from epics_mcp.resources import get_guide as guide_text
from epics_mcp.server import build_instructions, mcp
from epics_mcp.tools import guide as guide_module
from epics_mcp.tools.guide import (
    TOPICS,
    is_section,
    resolve_topics,
    serve_guide,
    split_sections,
    split_subsections,
)

_MODULE_SOURCE = Path(guide_module.__file__)

#: Root modules through which a tool could reach a RUNNING system or a caller's file.
#:
#: The promise this pins is deliberately NOT "reads nothing": the module does read a file, its own
#: packaged ``operator_guide.md``, through ``importlib.resources``. What is promised, and what a
#: caller leans on when this is the first call of a session, is the narrower thing: no PV, no REST
#: plane, no subprocess, no path of theirs, so nothing here can time out or depend on how this
#: instance was configured.
_RUNNING_SYSTEM_OR_CALLER_FILE = frozenset(
    {
        "p4p",
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "asyncio",
        "pathlib",
        "os",
        "shutil",
        "glob",
        "tempfile",
    }
)


async def _answer(client: Client[FastMCPTransport], **arguments: object) -> tuple[str, object]:
    """The (single text block, structuredContent) of ONE ``get_guide`` call over the wire."""
    result = await client.call_tool("get_guide", arguments)
    blocks = [part for part in result.content if isinstance(part, TextContent)]
    assert len(blocks) == 1, f"expected exactly one text block, got {len(blocks)}"
    return blocks[0].text, result.structured_content


# --------------------------------------------------------------------------------------------
# 1. The pull: without the header sentence nothing reaches this tool at all
# --------------------------------------------------------------------------------------------


def test_the_instructions_name_the_tool_in_both_capability_branches() -> None:
    """The header is the ONLY channel that arrives before a tool is chosen, so it is the only
    thing that can send a model here. Both branches, because a core-only install has the same
    guide and the same need for it.

    ⚠️ This is the guard that makes trimming the header safe. The header is capped at 2048 bytes
    and is edited whenever a tool line is added; drop this one sentence while shortening it and
    every other guard stays green while the whole guide becomes unreachable. Red-proof: remove
    ``get_guide`` from the leading sentence of ``build_instructions``.
    """
    for display_tools in (True, False):
        header = build_instructions(display_tools)
        assert "get_guide" in header, (
            f"the instructions (display_tools={display_tools}) do not name get_guide; nothing "
            "sends a model to the guide, and the resource is not a channel a model pulls from"
        )


# --------------------------------------------------------------------------------------------
# 2. Verbatim: an excerpt is an excerpt, not a rendering
# --------------------------------------------------------------------------------------------


async def test_without_a_topic_the_tool_serves_the_packaged_file_byte_for_byte() -> None:
    """Read over the real client rather than by calling the function: what is under test is the
    path an agent takes, registration, serialisation and delivery together. A direct
    ``serve_guide()`` call would be green even if the tool were not registered at all.
    """
    async with Client(mcp) as client:
        text, _ = await _answer(client)
    assert text == guide_text(), "the tool serves something other than the packaged file"
    assert text.lstrip().startswith("# "), "the guide is no longer a markdown document"


async def test_a_blank_topic_means_everything_and_the_description_says_so() -> None:
    """Blank is an order, a typo is a mistake, and the two get different answers.

    ⛔ The distinction has to stand ON THE WIRE, not only here. On the sibling surface the
    description promised "never a silent fall back to the whole guide" while ``topic=""`` did
    exactly that; the qualification lived in the internal docstring and in a test, the two places
    a caller never sees. So this checks both the behaviour and the sentence that promises it.
    """
    async with Client(mcp) as client:
        for blank in ("", "   ", "\t\n"):
            text, _ = await _answer(client, topic=blank)
            assert text == guide_text(), f"topic={blank!r} did not serve the whole guide"

        tools = {tool.name: tool for tool in await client.list_tools()}
        described = (tools["get_guide"].inputSchema or {})["properties"]["topic"]["description"]
    assert "empty string" in described, (
        "the argument description does not say that an empty string serves the whole guide, so "
        "its 'never a silent fall back' promises more than the tool keeps"
    )


async def test_every_topic_serves_its_own_part_verbatim() -> None:
    """Each key returns exactly its part, and that part occurs in the file as written.

    The sum check below is the real guard; this one pins the shape, which is what tells a caller
    whether they were handed a section's own text or one subsection of it.
    """
    whole = guide_text()
    async with Client(mcp) as client:
        for key in TOPICS:
            text, structured = await _answer(client, topic=key)
            assert structured is None
            assert text.startswith(("## ", "### ")), (
                f"'{key}' did not serve a whole heading block, it starts {text[:20]!r}"
            )
            # is_section is what the sum check partitions on, so it has to agree with the text
            # it is asked about rather than with a second list somebody keeps in step by hand.
            assert is_section(text) == text.startswith("## "), (
                f"is_section disagrees with the heading level of '{key}'"
            )
            assert text in whole, f"'{key}' served text that is not in the guide as written"


def test_the_topics_partition_the_file_with_no_overlap_and_no_gap() -> None:
    """⭐ The sum check, and it is what makes "verbatim excerpt" a fact rather than a claim.

    ONE identity, because the topics are a PARTITION: the preamble plus every topic's body, in
    document order, is the file again. That covers both failure directions at once. A swallowed or
    duplicated byte breaks it, and so does an overlap, which is the shape this had before: a
    section key that carried its own subsections would make the concatenation longer than the file
    while every individual answer still looked right.

    A second, cheaper assertion states the same property in the units a caller feels: the parts sum
    to the document's size exactly, so no part can be paying for another one's bytes.

    The negative control lives in :func:`test_the_sum_check_can_actually_fail`, so this assertion
    is provably able to go red rather than merely present.
    """
    whole = guide_text()
    preamble, _ = split_sections(whole)
    ordered = list(resolve_topics(whole).values())

    assert _sum_holds(preamble, ordered, whole), (
        "the topics do not re-assemble the guide; a part was lost, duplicated, or two parts overlap"
    )
    assert len(preamble) + sum(len(part) for part in ordered) == len(whole)


def _sum_holds(head: str, parts: list[str], whole: str) -> bool:
    """The check itself, so the negative control exercises IT and not a copy of it."""
    return head + "".join(parts) == whole


def test_the_sum_check_can_actually_fail() -> None:
    """The negative control, in BOTH directions the partition can break.

    Without it, ``_sum_holds`` is an assertion nobody has seen fail, and a guard that cannot go
    red is the defect it was meant to remove. One byte lost is the obvious direction; a part that
    carries the text of the next one is the direction this design actually had, and it is the one
    that stays invisible in any single answer.
    """
    whole = guide_text()
    preamble, sections = split_sections(whole)

    lost = [sections[0][:-1], *sections[1:]]
    assert not _sum_holds(preamble, lost, whole), (
        "the sum check accepted a section that lost a byte, so it proves nothing"
    )

    # The first section that HAS subsections, found rather than hardcoded: which one that is
    # depends on the guide, and an index would rot the next time a section moves.
    nested = next(index for index, s in enumerate(sections) if split_subsections(s)[1])
    _, subsections = split_subsections(sections[nested])
    overlapping = [*sections[:nested], sections[nested], *subsections, *sections[nested + 1 :]]
    assert not _sum_holds(preamble, overlapping, whole), (
        "the sum check accepted a section served ALONGSIDE its own subsections, which is exactly "
        "the overlap the partition rule exists to forbid"
    )


# --------------------------------------------------------------------------------------------
# 3. The refusals
# --------------------------------------------------------------------------------------------


async def test_an_unknown_topic_is_refused_by_name_and_never_guessed() -> None:
    """A typo costs a message, never the wrong section. The refusal names every valid key, so the
    correction needs no second lookup, and it is a hard error rather than a nearest-neighbour hit:
    an answer that looks plausible and is the wrong part is worse than none, because nothing in
    the text says so.
    """
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_guide", {"topic": "recipe"})  # 'recipes' exists
    message = str(excinfo.value)
    assert "[UNKNOWN_TOPIC]" in message, f"wrong error code on the wire: {message}"
    missing = [key for key in TOPICS if key not in message]
    assert not missing, f"the refusal does not name every valid topic, missing: {missing}"


def test_a_swapped_anchor_is_drift_and_not_an_unknown_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two exchanged fragments are the expensive failure, and the ONE check that catches them.

    The first two checks in :func:`resolve_topics` pass on a swap: each fragment still matches
    exactly one heading and each heading is still claimed once, so ``recipes`` would quietly serve
    the error signatures. Only the document-order check sees it.

    It is also a different fault from a caller's typo and carries a different code: the caller did
    nothing wrong, the shipped document and the topic table disagree.
    """
    order = list(TOPICS)
    swapped = dict(TOPICS)
    swapped[order[0]], swapped[order[-1]] = TOPICS[order[-1]], TOPICS[order[0]]
    monkeypatch.setattr(guide_module, "TOPICS", swapped)

    with pytest.raises(GuideDriftError) as excinfo:
        resolve_topics(guide_text())
    assert excinfo.value.error_code == "GUIDE_DRIFT"
    assert not isinstance(excinfo.value, UnknownTopicError)


def test_a_section_no_topic_claims_is_drift_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: a heading added to the guide with no key is unreachable, and silence
    about it is how the topic space rots. Red-proof: drop any key from ``TOPICS``.
    """
    monkeypatch.setattr(guide_module, "TOPICS", {key: TOPICS[key] for key in list(TOPICS)[:-1]})
    with pytest.raises(GuideDriftError) as excinfo:
        resolve_topics(guide_text())
    assert "no topic" in str(excinfo.value)


# --------------------------------------------------------------------------------------------
# 4. The payload, which is the whole reason 'topic' exists
# --------------------------------------------------------------------------------------------


async def test_the_answer_carries_no_structured_copy_of_itself() -> None:
    """⛔ The guard over a doubled payload, and it is measured rather than stylistic.

    A ``-> str`` return makes FastMCP wrap the value in ``{"result": ...}`` and send it BOTH as a
    text block and as ``structuredContent``, so the document crosses the wire twice, the second
    copy JSON-escaped and therefore longer. Measured on 2026-08-15 against this guide (87 268 B):
    87 268 B of text plus 88 868 B of structuredContent, 176 136 B for one call. The tool
    therefore returns a ``ToolResult`` with one text block and nothing else.

    Red-proof: change the tool's return annotation to ``str`` and return the text directly.
    """
    async with Client(mcp) as client:
        for arguments in ({}, {"topic": "errors"}):
            result = await client.call_tool("get_guide", arguments)
            assert result.structured_content is None, (
                "the answer carries a structuredContent copy of the same document; that doubles "
                f"the payload of every call (arguments={arguments})"
            )
            assert len(result.content) == 1


async def test_a_topic_is_a_fraction_of_the_whole_document() -> None:
    """The property the argument exists for, asserted rather than assumed: no single part may be
    anywhere near the whole document, or the split bought nothing.

    A ratio rather than a byte figure, so ordinary editing of the guide does not trip it. The
    threshold is deliberately loose: what it catches is a split that collapsed (one topic serving
    everything), not growth. Measured on the current guide the worst part is well inside it.
    """
    whole = len(guide_text().encode("utf-8"))
    async with Client(mcp) as client:
        for key in TOPICS:
            text, _ = await _answer(client, topic=key)
            assert len(text.encode("utf-8")) < whole * 0.5, (
                f"topic '{key}' serves more than half the guide; the split is not doing its job"
            )


async def test_the_tool_key_serves_the_inventory_rather_than_the_whole_palette() -> None:
    """The promise the two-level design was built for, asserted rather than described.

    "Which tool answers this" is the question this surface is asked first, and the answer is the
    drift-guarded inventory block at the top of the tool palette. If ``tools`` served the whole
    section instead, that question would cost 27 KB, most of it Olog and write-gate detail the
    asker did not want. Red-proof: make a section body carry its subsections again.
    """
    async with Client(mcp) as client:
        text, _ = await _answer(client, topic="tools")
    assert "BEGIN:tool-inventory" in text, "the 'tools' key no longer serves the tool inventory"
    assert "### " not in text, (
        "the 'tools' key serves its subsections too, so the inventory costs their bytes as well"
    )


# --------------------------------------------------------------------------------------------
# 5. Safe as the first call of a session
# --------------------------------------------------------------------------------------------


def test_the_module_reaches_no_running_system_and_no_caller_file() -> None:
    """The tool description promises this is safe before anything is known about the deployment.
    An import scan is the cheapest check that the promise is structural rather than current
    behaviour.

    Honest limit, named rather than papered over: the scanner sees ABSOLUTE imports in this one
    module. A relative import, a dynamic ``importlib.import_module`` call, and anything reached
    THROUGH ``epics_mcp.resources`` are outside it. That is the price of a check that executes
    nothing; the transitive half is covered by the fact that ``resources.get_guide`` reads
    packaged data and by the tests above, which call the tool with no service configured at all.
    """
    tree = ast.parse(_MODULE_SOURCE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])

    forbidden = sorted(roots & _RUNNING_SYSTEM_OR_CALLER_FILE)
    assert not forbidden, (
        f"{_MODULE_SOURCE.name} imports {forbidden}, so the tool can reach a running system or a "
        "caller's file and is no longer safe as the first call of a session"
    )


def test_the_function_answers_without_a_server_at_all() -> None:
    """The same promise from the other side: called directly, with no client, no configuration and
    no network, every topic still answers. This is what "cannot time out" means in practice.
    """
    assert serve_guide() == guide_text()
    for key in TOPICS:
        assert serve_guide(key), f"topic '{key}' answered with nothing"
    with pytest.raises(UnknownTopicError):
        serve_guide("no-such-topic")
