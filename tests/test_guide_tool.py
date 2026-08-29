"""Guards over ``get_guide``, the channel a MODEL pulls the operator guide from.

**Why the tool exists although the same text has been ``epics://guide`` since E1.** The
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
import json
import re
import subprocess
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
    copy JSON-escaped and therefore longer: one call costs a little over TWICE the document. The
    tool therefore returns a ``ToolResult`` with one text block and nothing else. The exact byte
    figures that used to stand here are gone on purpose (they were stale within a day); the ratio
    is measured by :func:`test_the_rounded_size_claims_still_hold` below.

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
    section instead, that question would cost the whole palette, most of it Olog and write-gate
    detail the asker did not want. (This docstring said 27 KB until 2026-08-24, when the palette
    measured 34; the figure is claimed once, in ``tools/guide.py``, where a test reads it.)
    Red-proof: make a section body carry its subsections again.
    """
    async with Client(mcp) as client:
        text, _ = await _answer(client, topic="tools")
    assert "BEGIN:tool-inventory" in text, "the 'tools' key no longer serves the tool inventory"
    assert "### " not in text, (
        "the 'tools' key serves its subsections too, so the inventory costs their bytes as well"
    )


async def test_the_errors_key_serves_the_lead_and_not_one_single_signature() -> None:
    """GP-15: the error signatures are reached one GROUP at a time, never all thirty at once.

    ⛔ THE ASSERTION IS "NO SIGNATURE", NOT "NO SUBHEADING", and the difference is the whole guard.
    A ``"### " not in text`` check is what the sibling test above can afford, because it has a
    positive assertion beside it (the inventory marker) that pins WHICH text arrived. Here it would
    be a sham: delete the FIRST ``### `` of the section and its signatures fall into the lead,
    which then still contains no ``### `` at all, because the lead ends at the NEXT subheading.
    The guard would stay green over exactly the regression it exists to catch.

    A signature is a top-level list item, so the lead containing none of them is the property that
    matters, and it holds however many groups there are. Deliberately zero numbers: the count of
    groups and their sizes are what a later editor is entitled to change.

    ⚠️ WHAT THIS GUARD IS FOR, and its reach is NARROW, which the first write-up of it got wrong.
    The obvious red-proof, deleting a ``### `` line, does not reach this assertion at all:
    :func:`resolve_topics` raises ``GUIDE_DRIFT`` first, because the orphaned key then anchors on
    nothing. So this is a SECOND line, over a CONSISTENT rollback where a heading and its key go
    together. But it only sees such a rollback of the FIRST group: remove any later one and its
    signatures fall into the PRECEDING group, not into the lead, and this assertion stays green.
    Measured, not reasoned: dropping ``err-olog`` and its heading leaves the whole module green,
    and rolling back ten of the eleven leaves the ENTIRE suite green with 24 666 B back behind one
    key. The guard for that is
    :func:`test_no_error_group_is_the_largest_part_of_the_guide`, written after this one was
    measured and found to be a sham for ten of its eleven cases.

    Red-proof, run: remove the first ``### `` line of the section AND its ``err-transport`` entry
    from ``TOPICS``. Reddens here, naming the strays, with ``resolve_topics`` staying green.
    """
    async with Client(mcp) as client:
        text, _ = await _answer(client, topic="errors")
    strays = [line for line in text.split("\n") if line.startswith("- ")]
    assert not strays, (
        f"the 'errors' key serves {len(strays)} signature(s) rather than the section's lead alone, "
        "so asking about one error signature pays for its neighbours again. A signature belongs "
        "under one of the 'err-' subsections. First stray: " + strays[0][:80]
    )
    assert text.lstrip().startswith("## Error signatures"), (
        "the 'errors' key no longer serves the signature section's own lead"
    )


def test_no_error_group_is_the_largest_part_of_the_guide() -> None:
    """GP-15: the error signatures stopped being the document's outlier, and stay stopped.

    ⛔ THIS IS THE GUARD THAT ACTUALLY HOLDS THE SPLIT, and it exists because the sibling above
    does not. That one asserts the LEAD carries no signature, which only catches a rollback of the
    FIRST group; a rollback of any later one pours its signatures into the preceding GROUP, where
    nothing was looking. Measured on a copy: removing ten of the eleven headings together with
    their keys left the full suite green at 2386 passed while one key served 24 666 B, which is
    the exact payload GP-15 was built to remove. A guard that green is the defect it was meant to
    find.

    THE PROPERTY IS RELATIVE, SO IT CANNOT AGE. Before the split the signature section was the
    single largest addressable part of the guide, at 24 741 B against 8 092 B for the runner-up.
    After it, the largest part is somewhere else entirely, and that sentence is what the commit,
    the changelog and the tool description all claim. This asserts exactly that claim: no ``err-``
    part may be the biggest. No byte figure is pinned, so ordinary growth on either side is free,
    and the threshold recalibrates itself whenever any other section grows.

    ⚠️ WHAT IT DOES NOT CATCH, so nobody reads it as more: merging two ADJACENT small groups. That
    stays under the maximum and is invisible here, which is deliberate rather than an oversight,
    because a smaller number of larger groups is a judgement about the document and not the
    regression this guards. What it does catch is any rollback big enough to matter, which is the
    one that happened.

    Red-proof, run on a copy: remove any ``### `` heading in the section together with its key,
    repeatedly, until one group carries more than the largest part outside. One suffices in the
    limit; ten certainly do.
    """
    parts = resolve_topics(guide_text())
    inside = {
        key: len(body.encode("utf-8")) for key, body in parts.items() if key.startswith("err-")
    }
    outside = {
        key: len(body.encode("utf-8"))
        for key, body in parts.items()
        if not key.startswith("err-") and key != "errors"
    }
    assert inside, "no 'err-' topics at all: the signature section is no longer split"
    biggest_key, biggest = max(inside.items(), key=lambda item: item[1])
    ceiling_key, ceiling = max(outside.items(), key=lambda item: item[1])
    assert biggest < ceiling, (
        f"the error group '{biggest_key}' is {biggest} B, which makes it the largest single part "
        f"of the guide again (the next biggest elsewhere is '{ceiling_key}' at {ceiling} B). "
        "Before GP-15 that section WAS the outlier, at three times the runner-up, and one "
        "question about one signature paid for all of them. Either the section lost subheadings "
        "or one group grew past the rest of the document: split it further."
    )


def test_the_unguarded_pages_do_not_restate_the_size_promise() -> None:
    """GP-15: the size of the guide is promised where it is measured, and nowhere else.

    ⛔ WHY A GUARD AND NOT JUST A CLEANUP. ``docs/tools.md`` carried its own two figures for this
    document ("~80 KB", "the largest single part is a quarter"). Both were copies of the promise
    made in ``server.py``, both disagreed with it, and the second was measurably false when it was
    found: the largest part was 29% of the document, which is not a quarter. Nothing held them,
    because ``docs/known-limits.md`` already records that this tool table is unchecked. Rewriting
    them would have produced a fourth accurate copy that ages on the next edit, so the page points
    at the carrier instead. This is the guard that keeps a figure from creeping back in, and it is
    the same shape ``tests/test_write_posture_sites.py`` uses for the write posture (GP-4).

    ⛔ THE POPULATION IS DERIVED, NOT LISTED, and that was the first version's flaw. It named two
    pages by hand out of nineteen tracked markdown files; measured, the same removed sentence
    appended to ``OPERATING.md`` or ``docs/safety.md``, both of which name this tool, left the
    suite green. So the population is ``git ls-files "*.md"``, the shape
    ``tests/test_write_posture_sites.py`` uses for the same reason, and a page added tomorrow is
    covered the day it is tracked.

    SCOPE. ``server.py`` and ``tools/guide.py`` are not markdown and are the CARRIERS anyway;
    their wording is checked against the live measurement by
    :func:`test_the_rounded_size_claims_still_hold`. ``CHANGELOG.md`` is excluded because a
    release note records what a version said, so a guard over it would need historical
    exemptions, the construction ``docs/known-limits.md`` section 1 rejects in writing.

    ⚠️ IT MUST FIRE ON A CLAIM AND NOT ON A POINTER, which is why it does not simply grep for
    "largest part": the pointer that replaced the removed sentence has to name the thing it points
    at. The needles are therefore a KB figure, or a fraction WORD next to "part", or a percentage.
    A pointer carries none of the three.

    WHAT IT DOES NOT CATCH, stated so nobody relies on it: a size described in words no pattern
    anticipates ("about a fifth of a megabyte"). That is the standing limit of every phrase guard;
    the first version of this one let TWELVE of fifteen probed rewordings through, including a
    plain "biggest" for "largest", and widening it to these three shapes is what closed that.

    Red-proof: put "the largest single part is a quarter of the document" back into docs/tools.md,
    or any of its rewordings.
    """
    repo = Path(__file__).resolve().parents[1]
    pages = [
        page
        for page in subprocess.run(
            ["git", "ls-files", "*.md"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if page != "CHANGELOG.md"
    ]
    assert len(pages) > 10, (
        f"only {len(pages)} markdown pages found; git ls-files answered nothing useful and this "
        "guard would be reading an empty population"
    )
    # Three shapes, because one was not enough: a KB figure, a fraction word beside "part", and a
    # percentage. Each is a CLAIM about a size; none of them appears in a sentence that merely
    # points at where the claim is made and guarded.
    needles = (
        re.compile(r"\d+\s*KB", re.IGNORECASE),
        re.compile(r"\b(quarter|third|half|fifth|tenth|sixth)\b", re.IGNORECASE),
        re.compile(r"\d+(\.\d+)?\s*%"),
    )
    kb_figure, fraction_word, percentage = needles
    offenders: list[str] = []
    for page in pages:
        text = (repo / page).read_text(encoding="utf-8")
        for number, line in enumerate(text.split("\n"), start=1):
            lowered = line.lower()
            if not any(
                token in lowered
                for token in ("get_guide", "operator guide", "epics://guide", "operator_guide")
            ):
                continue
            hits = [match for needle in needles if (match := needle.search(line))]
            if not hits:
                continue
            # A fraction word only claims a size when it is talking about a PART of something,
            # so "half the recipes" is not a size claim while "half the document" is. A KB figure
            # or a percentage on the same line is a claim on its own and settles it either way.
            if (
                fraction_word.search(line)
                and not kb_figure.search(line)
                and not percentage.search(line)
                and "part" not in lowered
            ):
                continue
            offenders.append(f"{page}:{number} ({hits[0].group(0)!r})")

    # Collected rather than raised on the first hit, so a page carrying three copies is one round
    # of work instead of three.
    assert not offenders, (
        "these lines state the size of the operator guide: "
        + "; ".join(offenders)
        + ". That promise is made in get_guide's own description in src/epics_mcp/server.py and "
        "guarded by test_the_rounded_size_claims_still_hold, which measures the live document. A "
        "second copy is unguarded and will age, which is exactly what happened to the two figures "
        "docs/tools.md used to carry. Point at the carrier instead, naming it without restating "
        "the number. If a figure genuinely belongs on a page, exempt it here in the same commit, "
        "with a reason."
    )


#: KB here means 1000 bytes, and that was MEASURED rather than assumed. The predecessor sentence
#: "around 80 KB" entered the tree in ``ead7f08``, where the guide was 79 535 B: 79.5 decimal, but
#: only 77.7 in KiB. So these sentences have always rounded decimally, and an earlier version of
#: this comment asserted the opposite from a size taken off a different commit. Keeping the unit
#: decimal also keeps the two live claims consistent with each other, which KiB would not
#: (85 359 B is "85" decimal and "83" binary; 28 024 B is "28" decimal and "27" binary).
_KB = 1000

#: How far a rounded word may sit from the measurement and still be honest. One number, applied to
#: every claim, so nobody can widen the tolerance of the one claim that has gone wrong.
#:
#: ⚠️ 3, not 5, and the difference is the whole point: the drift this guard exists for moved the
#: document by 5.2 kB, so a tolerance of 5 would have accepted the stale sentence it was built to
#: catch. Measured: with 5.0, restoring the pre-fix "around 80 KB" against today's 85.4 kB stayed
#: GREEN. A guard whose band is wider than the failure is decoration.
_ROUNDING_TOLERANCE_KB = 3.0

_MODULE_DOC = Path(guide_module.__file__).read_text(encoding="utf-8")
_SERVER_DOC = (Path(__file__).resolve().parents[1] / "src" / "epics_mcp" / "server.py").read_text(
    encoding="utf-8"
)


def _claimed(source: str, pattern: str, where: str) -> float:
    """The number a claim in *source* actually states.

    Serves two kinds of claim now: a ROUNDED size, where the caller compares within a tolerance,
    and an exact COUNT, where it compares for equality. The reading half is the same either way,
    which is why the message below says "claim" rather than "rounded size claim".

    ⛔ THE CLAIM IS READ OUT OF THE PROSE, never hard-coded here, and that is the whole design. A
    test carrying its own copy of the figure checks that the DOCUMENT is a certain size; it says
    nothing about whether the sentence a reader meets is true, so editing the sentence to a wrong
    value leaves it green. Reading the sentence closes that loop in both directions: the guide
    growing past its description is red, and mis-describing the guide is red as well.
    """
    match = re.search(pattern, source)
    assert match, f"the claim {pattern!r} is gone from {where}"
    return float(match.group(1))


def test_the_rounded_size_claims_still_hold() -> None:
    """GP-20: the prose rounds, and this measures whether the rounding is still true.

    ⛔ WHY ROUNDED AND NOT PINNED. An exact byte count for this document stood in three files,
    eight occurrences over seven lines. It was stale in its OWN commit, which raised the guide by
    50 B in the same change, and across a single day the document took six different sizes
    spanning nearly 8 KB in both directions while the pinned figure never moved. Two further
    figures were DERIVED from it (the structuredContent copy and their sum) and inherited the
    error, and a fourth, the tool inventory, moved by the same 50 B in the same commit. Nothing
    re-ran any of them: measured, no test in this repository checked the size of the guide at all.

    ⚠️ WHAT IS ASSERTED IS THE SENTENCE, NOT A CONSTANT. Each claim is parsed out of the prose that
    makes it and compared against the live measurement within one shared tolerance. So the two
    ways this can be wrong are both caught: the guide drifting away from its description, and a
    description edited to something the guide is not.

    ⛔ WHEN THIS GOES RED, EDIT THE PROSE. Widening the tolerance to fit is how the pinned figure
    survived four sizes; it is one number for all four claims precisely so that it cannot be
    stretched for whichever one has gone wrong.
    """
    whole_bytes = len(guide_text().encode("utf-8"))
    whole_kb = whole_bytes / _KB
    claimed_whole = _claimed(_MODULE_DOC, r"guide is around (\d+) KB", "tools/guide.py")
    assert abs(whole_kb - claimed_whole) <= _ROUNDING_TOLERANCE_KB, (
        f"the guide measures {whole_bytes} B ({whole_kb:.1f} KB) and tools/guide.py says around "
        f"{claimed_whole:.0f} KB. Re-word the claim, in tools/guide.py AND in the get_guide "
        "docstring in server.py, which is the copy a caller reads."
    )
    claimed_by_server = _claimed(_SERVER_DOC, r"document is around (\d+) KB", "server.py")
    assert claimed_by_server == claimed_whole, (
        f"the two roundings disagree: server.py says {claimed_by_server:.0f} KB, tools/guide.py "
        f"says {claimed_whole:.0f} KB. They describe the same document and must not drift apart."
    )

    # The second copy a ``-> str`` return would add. json.dumps is the same escaping the protocol
    # applies, so this measures the cost the ToolResult shape avoids rather than restating it.
    doubled = whole_bytes + len(json.dumps({"result": guide_text()}).encode("utf-8"))
    ratio = doubled / whole_bytes
    # ⚠️ The lower bound is a tautology and is kept as documentation, not as a check: JSON escaping
    # only ever adds bytes, so the ratio cannot fall below 2. The UPPER bound is the live half.
    assert 2.0 < ratio <= 2.2, (
        f"one call would cost {ratio:.2f}x the document, so 'a little over twice' no longer "
        "describes it. That phrase is in tools/guide.py, server.py and this module."
    )

    inventory_kb = len(serve_guide("tools").encode("utf-8")) / _KB
    claimed_inventory = _claimed(_MODULE_DOC, r"inventory, roughly ([\d.]+) KB", "tools/guide.py")
    assert abs(inventory_kb - claimed_inventory) <= 0.5, (
        f"the tool inventory measures {inventory_kb:.2f} KB and the claim says roughly "
        f"{claimed_inventory} KB. Re-word it in tools/guide.py."
    )

    _, sections = split_sections(guide_text())
    palette = next((s for s in sections if s.startswith("## Tool palette")), "")
    assert palette, (
        "the tool palette section was renamed. The 'not the NN KB section' claim in "
        "tools/guide.py names it, so rename it there in the same commit and adjust this lookup."
    )
    palette_kb = len(palette.encode("utf-8")) / _KB
    # ``\s+`` on both sides, so a reflow that puts the number and its unit on different lines from
    # the words around them does not redden a claim that is still true. Measured: the earlier
    # pattern anchored on a single optional newline and broke on exactly that.
    claimed_palette = _claimed(_MODULE_DOC, r"not the\s+(\d+)\s+KB\s+section", "tools/guide.py")
    assert abs(palette_kb - claimed_palette) <= _ROUNDING_TOLERANCE_KB, (
        f"the tool palette measures {palette_kb:.1f} KB and the claim says {claimed_palette:.0f} "
        "KB. Re-word it in tools/guide.py."
    )

    largest = max(len(part.encode("utf-8")) for part in resolve_topics(guide_text()).values())
    assert largest < whole_bytes / 3, (
        f"the largest single part is {largest / whole_bytes:.1%} of the document, so the tool "
        "description's 'the largest single part is under a third of the whole' is now false. That "
        "sentence is a promise to a CALLER, in the get_guide docstring in server.py."
    )


#: The one sentence in ``tools/guide.py`` that states how many error-signature groups there are.
#: ``\s+`` on both sides of the number for the reason the palette claim records: the sentence is
#: wrapped at 100 columns, and a pattern anchored on a single space breaks on a reflow that changes
#: nothing about whether the claim is true.
_GROUP_COUNT_CLAIM = r"cut into\s+(\d+)\s+groups"


def test_the_group_count_claim_still_holds() -> None:
    r"""The error-signature group count is prose in SHIPPED source, and nothing read it.

    ⛔ WHY THIS EXISTS. ``tools/guide.py`` said "eleven groups" while the table beside the sentence
    defined TWELVE ``err-`` keys and the shipped guide carried twelve ``### `` subheadings under
    "Error signatures". The figure had been wrong for as long as ``err-crossplane`` existed, in a
    docstring that ships in the wheel, and no test here looked at it: ``guide.py`` is absent from
    ``tests/test_prose_counters._WATCHED``, and every guard over ``CHANGELOG.md`` excludes that file
    by construction, so all three copies of the number were unwatched at once.

    ⚠️ A COUNT IS KEPT WHERE A BYTE SIZE WOULD BE REMOVED, and the difference is not cosmetic. The
    sizes in this module are rounded because they move whenever anyone edits a sentence; a count of
    groups moves only when the split itself changes, which is a decision somebody makes on purpose.
    It also answers the question a caller has here, whether asking for one group instead of the
    whole section is worth it.

    ⚠️ TWO DENOMINATORS, and the honest reason for the second is NARROWER than "it could be
    anything". ``resolve_topics`` already forces a bijection between keys and addressable parts in
    document order, and "Error signatures" is the LAST ``## `` section, so the two counts agree
    structurally today. What ``keys == headings`` adds on top is naming discipline, which
    ``resolve_topics`` does not see: an ``err-``-prefixed key sorted before ``errors``, or a
    subheading down there whose key carries no prefix. That is worth an assertion and it is not a
    second independent measurement; the docstring said "load-bearing half" until a review measured
    the overlap.

    ⚠️ THE MULTIPLICITY IS CHECKED, because ``_claimed`` uses ``re.search`` and takes the first
    match silently. A second sentence of the same shape would be unguarded while this stayed green.
    ⛔ PRESENCE IS ASSERTED SEPARATELY, and that split is a repair rather than a flourish. With one
    ``== 1`` check the empty case failed on the multiplicity message, which says "more than once"
    while the sentence stands ZERO times: red for the right reason with the wrong diagnosis, and
    the docstring below claimed an empty-set proof the test could not deliver. Measured, that is
    exactly what the first run of this guard did.

    ⚠️ THE DIGIT SPELLING IS PART OF THE PROMISE. ``_claimed`` returns ``float(group(1))``, so
    "cut into twelve groups" would redden this even though it is true. The surrounding paragraph
    spells other numbers out; this one stays a digit deliberately.

    ⚠️ ``_MODULE_DOC`` is the whole SOURCE of ``tools/guide.py``, not just its module docstring, so
    the sentence could move into a comment and still be read here. That matches the neighbouring
    size claims, which read the same string.

    Red-proof: change the sentence to "cut into 11 groups", and this fails naming both numbers.
    Empty-set proof: delete the sentence, and the presence assertion below fails by name.
    """
    found = re.findall(_GROUP_COUNT_CLAIM, _MODULE_DOC)
    assert found, (
        "the group-count claim is gone from tools/guide.py: nothing states how many "
        "error-signature groups there are, so this guard would otherwise pass on an empty match"
    )
    assert len(found) == 1, (
        "tools/guide.py states the group count more than once, or the pattern now matches "
        "something else as well; _claimed reads only the first, so the rest would be unguarded"
    )
    claimed = _claimed(_MODULE_DOC, _GROUP_COUNT_CLAIM, "tools/guide.py")

    keys = sum(1 for key in TOPICS if key.startswith("err-"))
    sections = [
        section
        for section in split_sections(guide_text())[1]
        if section.startswith("## Error signatures")
    ]
    assert len(sections) == 1, (
        "the error-signature section was renamed or split. This test names it, so rename it here "
        "in the same commit."
    )
    headings = len(split_subsections(sections[0])[1])

    # The non-empty anchor, and it is the assertion this guard was missing: with the section
    # emptied out, keys, headings and a prose "cut into 0 groups" would agree at zero and every
    # comparison below would pass on nothing at all. Both neighbours in this module carry the same
    # anchor for the same reason.
    assert headings, (
        "'Error signatures' carries no subheadings at all, so the signature section is no longer "
        "split and the claim in tools/guide.py describes nothing"
    )

    assert keys == headings, (
        f"{keys} 'err-' topic keys against {headings} subheadings under 'Error signatures': one of "
        "them anchors somewhere else, so no single number describes the split"
    )
    assert claimed == keys, (
        f"the error signatures are cut into {keys} groups and tools/guide.py claims "
        f"{claimed:.0f}. Re-word the sentence there."
    )


def test_the_four_retired_figures_have_not_been_re_pinned() -> None:
    """A denylist of the four figures GP-20 retired. NOT a rule against pinning any figure.

    ⚠️ The name says denylist because the honest scope is a denylist, and the earlier name
    ("no pinned byte figure came back") promised a rule this cannot keep: writing a FRESH exact
    size into the same docstring passes here, measured. What stops that is the review, plus
    :func:`test_the_rounded_size_claims_still_hold`, which reads the sentences and would redden as
    soon as a fresh figure drifted from the document.

    The ticket's own acceptance command was narrower still: ``grep -o '87 268' -r src tests`` names
    the first of the four and lets the other three through, including the two that sat on the SAME
    line as it. Markdown is swept as well, because a rounded sentence can be moved into the shipped
    guide, where nothing else would read it.

    Two further limits, stated rather than implied. Only the space-separated spelling is caught
    (``87268`` and ``87,268`` are not; measured, this tree writes thousands with a plain ASCII
    space and nothing else). And ``CHANGELOG.md`` is outside the sweep by design: it records what a
    release said, so a figure frozen there is history rather than a live claim.
    """
    stale = ("87 268", "88 868", "176 136", "1 471")
    root = Path(__file__).resolve().parents[1]
    offenders: dict[str, list[str]] = {}
    read = 0
    for folder in ("src", "tests"):
        for path in sorted((root / folder).rglob("*")):
            if path.suffix not in {".py", ".md"} or "__pycache__" in path.parts:
                continue
            read += 1
            text = path.read_text(encoding="utf-8")
            hits = [figure for figure in stale if figure in text]
            # This module names the figures in prose, as the history that explains the rounding.
            if hits and path.name != Path(__file__).name:
                offenders[str(path.relative_to(root))] = hits
    # The anchor: an empty population would make the loop above pass on nothing at all, which is
    # the failure a sweeping test is least likely to be caught in.
    assert read > 100, (
        f"the sweep read only {read} files; src/ or tests/ moved and it reads nothing"
    )
    assert not offenders, (
        f"a byte figure that went stale within a day is pinned again: {offenders}. Round it and "
        "let test_the_rounded_size_claims_still_hold measure the real one."
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


# --------------------------------------------------------------------------------------------
# 6. [GQ-170] (c): the group NAMES, at both places that write them out by hand
# --------------------------------------------------------------------------------------------
#
# ``test_the_group_count_claim_still_holds`` above holds HOW MANY error groups there are. Nothing
# held WHICH. Measured when this guard was written: the twelve names stand written out by hand in
# two places, in the ``[0.7.0]`` entry of ``CHANGELOG.md`` and in the SHIPPED guide, and a rename
# would have left both saying an address ``get_guide`` refuses, with every count still agreeing.
#
# ⚠ THE TRAP IS A TEST THAT LOOKS LIKE THIS ONE AND IS NOT.
# ``test_an_unknown_topic_is_refused_by_name_and_never_guessed`` holds the names against the
# REFUSAL message, which is generated from ``TOPICS`` itself. That
# pins the refusal, never a hand-written list, so a grep for "the names are held somewhere"
# answers yes and means something else. There are three copies of the list; ONE of them was
# covered.

#: A group key as both hand-written lists spell it. Deliberately not anchored on a backtick: the
#: guide writes them in prose as well as in a table, and an anchor on the markup would read one
#: site and silently skip the other.
_ERR_GROUP_NAME = re.compile(r"err-[a-z]+")


def _err_group_keys() -> set[str]:
    """The live set, read off ``TOPICS`` rather than restated here."""
    return {key for key in TOPICS if key.startswith("err-")}


def test_the_shipped_guide_names_exactly_the_error_groups_that_exist() -> None:
    """The guide a model reads names every live group and no dead one.

    EQUALITY in this direction, and it is the strict one on purpose: the guide is a LIVING
    document shipped in the wheel, so a group that exists and is not named there is unreachable
    for the reader who only has the guide, and a name it keeps after a rename sends that reader to
    an address ``get_guide`` refuses. Both failures are the reader's, so both are held.

    Red-proof, both directions: rename a key in ``TOPICS`` and the guide's old name is left over;
    delete a name from the guide sentence and the live key is missing. Empty-set proof: the
    assertion below the anchor cannot pass on an empty ``TOPICS``, because the anchor demands the
    live set is non-empty before the comparison is made.
    """
    keys = _err_group_keys()
    assert keys, (
        "TOPICS carries no err- group at all, so an equality below would compare two empty sets "
        "and pass on nothing"
    )
    named = set(_ERR_GROUP_NAME.findall(guide_text()))
    assert named == keys, (
        f"the shipped guide and TOPICS disagree about the error groups. Only in the guide: "
        f"{sorted(named - keys)}; only in TOPICS: {sorted(keys - named)}"
    )


def test_the_changelog_names_no_error_group_that_stopped_existing() -> None:
    """A released entry writes the names out, and a rename would turn them into dead addresses.

    ⛔ SUBSET, NOT EQUALITY, and the asymmetry against the guide above is the whole design. A
    released section is not rewritten here: ``tests/test_changelog_discipline`` reads
    ``[Unreleased]`` and nothing else, for the stated reason that a rule reaching a published
    section would be asking for a rewrite of something already on the index. A THIRTEENTH group
    must therefore be free to arrive without touching ``[0.7.0]``, which equality would forbid.

    What is NOT free is a name that stops existing: the entry tells a reader which addresses to
    ask for, and a renamed one makes a published sentence point at a refusal. There is no
    exception list here and none is wanted; if that day comes, the answer is a decision about the
    rename, not a row that silences this.

    Red-proof: rename a key in ``TOPICS`` and this fails naming the address the changelog still
    advertises. Empty-set proof: the changelog is asserted to name groups AT ALL before the subset
    is checked, because a file that named none would satisfy any subset relation.
    """
    keys = _err_group_keys()
    assert keys, "TOPICS carries no err- group at all, so any subset below would be vacuous"
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    named = set(_ERR_GROUP_NAME.findall(changelog.read_text(encoding="utf-8")))
    assert named, (
        "CHANGELOG.md names no error group at all: the hand-written list this guards is gone, so "
        "the subset below would hold on an empty set"
    )
    assert named <= keys, (
        f"CHANGELOG.md advertises an address get_guide no longer serves: {sorted(named - keys)}. "
        "A released entry is not rewritten here, so this is a question about the rename."
    )
