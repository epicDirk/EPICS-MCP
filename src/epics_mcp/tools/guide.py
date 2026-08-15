"""Serve the operator guide by section: the reading half of the ``get_guide`` tool.

**Why a tool, when the same text has been the resource ``epics-pv://guide`` since E1.** The
protocol has three server elements and three different parties control them: tools are
MODEL-controlled, resources are APPLICATION-controlled, prompts are USER-controlled. Whatever the
model is meant to FETCH BY ITSELF therefore has to be a tool. A resource is not a channel a model
pulls from, and this guide has been exactly that: complete, guarded against drift, and never
fetched. The resource stays; this module adds the half a model can reach.

**Why a topic at all, measured rather than assumed.** The guide is around 85 KB. Served from a
tool returning ``str``, FastMCP 3.4.4 wraps the value in ``{"result": ...}`` and sends it BOTH as
a text block AND as ``structuredContent``, so the same document crosses the wire twice, the second
copy JSON-escaped and therefore slightly longer: **one call costs a little over twice the
document**. The server side answers that with a ``ToolResult`` carrying one text block and no
structured payload (see the tool in ``server.py``); this module answers the other half, which is
that a document this size is still too much to hand over for a question about one error signature.

⚠️ **The sizes here are ROUNDED and DECIMAL, and that is the fix rather than sloppiness.** An
exact byte count stood in three files, eight times over. It was stale in its own commit, which
raised the document by 50 B in the same change, and across one day the document took SIX different
sizes spanning nearly 8 KB in both directions while the pinned figure never moved. A size that
changes whenever anyone edits a sentence cannot be carried in prose. What IS stable is the RATIO
(two copies per call), so that is what this paragraph states, and
``tests/test_guide_tool.py::test_the_rounded_size_claims_still_hold`` reads these very sentences,
measures the real figures, and goes red when a rounded word here stops being true. Re-measure
rather than trust: ``len(get_guide().encode("utf-8"))``.

**Two levels of key, because one is not enough here.** Splitting only on ``## `` leaves sections
of 27 KB and more, each larger than a sibling surface's ENTIRE guide. So the keys PARTITION the
document instead: a ``## `` key serves that section's own text and a ``### `` key serves one
subsection, in one flat namespace, with no overlap and no gap. The practical effect is the point:
a caller asking which tool answers a question gets the tool inventory, roughly 1.5 KB, not the
28 KB section that opens with it.

**Deterministic by construction.** The text comes from one packaged file, the split is a plain
line-anchored heading boundary that loses nothing (the parts re-assemble into the file byte for
byte, which ``tests/test_guide_tool.py`` measures in both directions), and a topic is an exact key
up to surrounding whitespace. There is no matcher, so there is nothing that can rank wrongly: the
failure mode of a free-text search this surface deliberately does not have.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from epics_mcp.errors import GuideDriftError, UnknownTopicError
from epics_mcp.resources import get_guide

#: Where a ``## `` section begins. Anchored at the start of a line, so a ``##`` inside a fenced
#: code block or a table cell cannot open one. It cannot match a ``### `` line either: the needle
#: is two hashes AND a space, which a third hash occupies.
_SECTION_START = re.compile(r"(?m)^(?=## )")

#: Where a ``### `` subsection begins, the same anchoring one level down.
_SUBSECTION_START = re.compile(r"(?m)^(?=### )")

#: Topic key -> a distinctive fragment of the heading it names, in DOCUMENT ORDER.
#:
#: The keys are the vocabulary a caller types, so they are short, lowercase and stable even if a
#: heading is reworded. They carry a hyphen rather than an underscore, which keeps them out of
#: every tool-name pattern this repository greps with (``test_guide_matches_code`` reads backticked
#: snake_case tokens as tool names inside the inventory markers).
#:
#: The value is a FRAGMENT, never a whole heading: several headings carry a tool name in backticks
#: or a parenthetical aside, and pinning those would make an ordinary wording fix a silent remap.
#: Each fragment must match exactly one heading, every heading must be claimed exactly once, and
#: the keys must resolve in document order. All three are enforced by :func:`resolve_topics`, so
#: drift in the guide is loud at the first call and in CI, never a topic quietly serving the wrong
#: section.
#:
#: ⚠️ The order is load-bearing and not cosmetic. The first two checks only catch a MISSING
#: section: swap two fragments and each still matches exactly one heading, each heading is still
#: claimed once, and one key quietly serves its neighbour's text. A wrong section is worse than
#: none, because nothing tells the reader it is wrong.
TOPICS: Mapping[str, str] = {
    "posture": "Posture (read this first)",
    "planes": "The planes",
    "tools": "Tool palette",
    "olog-output": "Olog output shape",
    "olog-filters": "Olog search filters",
    "pv-write": "PV write posture",
    "audit": "The audit trail",
    "olog-write": "Olog write posture",
    "olog-attachments": "Olog attachment posture",
    "recipes": "Operational recipes",
    "archived-pvs": "List archived PVs",
    "doctor": "Verify a deployment's config",
    "cluster": "Retrieval-cluster-aware appliances",
    "ca-bundle": "CA-bundle: combine the trust anchors",
    "field-read": "Read record fields directly",
    "naming-url": "Naming URL: no trailing",
    "archiver-window": "Archiver history time window",
    "alarm-window": "Alarm history time window",
    "olog-window": "Olog search time window",
    "alarm-tree": "Discover the alarm config-tree names",
    "errors": "Error signatures",
}


def split_sections(text: str) -> tuple[str, tuple[str, ...]]:
    """Split *text* into (preamble, ``## `` sections) without losing a byte.

    The preamble is everything before the first ``## `` heading, which is the title and the lead
    paragraph. Every section keeps its own heading line, so ``preamble + "".join(sections)`` is
    the input again. That identity is what makes "serve one section" honest: a caller gets a
    verbatim excerpt of the document, never a rendering of it.
    """
    parts = _SECTION_START.split(text)
    if not parts:  # pragma: no cover - re.split always yields at least one element
        return "", ()
    return parts[0], tuple(parts[1:])


def split_subsections(section: str) -> tuple[str, tuple[str, ...]]:
    """Split one ``## `` section into (lead, ``### `` subsections), same identity one level down.

    The lead is the section heading plus whatever prose follows it before the first ``### ``. It
    is a part in its own right rather than a leftover: in the tool palette it is the drift-guarded
    tool inventory, which is what a caller asking "which tool answers this" actually wants.
    """
    parts = _SUBSECTION_START.split(section)
    if not parts:  # pragma: no cover - re.split always yields at least one element
        return "", ()
    return parts[0], tuple(parts[1:])


def is_section(body: str) -> bool:
    """True when *body* opens with a ``## `` heading rather than a ``### `` one.

    Derived from the text itself so no second list has to be kept in step: a subsection starts
    with three hashes, which fails a ``"## "`` prefix test on the third character.
    """
    return body.startswith("## ")


def _addressable_parts(text: str) -> list[tuple[str, str]]:
    """Every (heading line, body) a topic may name, in document order, as a PARTITION.

    The parts do not overlap and they do not leave a gap: a section that has ``### `` children
    contributes its LEAD, then each child; a section without children contributes itself. So the
    preamble plus every part in this order is the document again, which is the single identity
    :mod:`tests.test_guide_tool` measures.

    ⚠️ The consequence is worth stating because it decides what a section key MEANS: ``tools``
    serves the tool palette's own text, the inventory, and NOT the six subsections below it, which
    are keys of their own. That is the whole point of having two levels. A section body that
    carried its children would put 27 KB behind the key a caller reaches for first, which is the
    payload this argument exists to avoid.
    """
    parts: list[tuple[str, str]] = []
    _, sections = split_sections(text)
    for section in sections:
        lead, subsections = split_subsections(section)
        parts.append((section.splitlines()[0], lead))
        parts.extend((subsection.splitlines()[0], subsection) for subsection in subsections)
    return parts


def resolve_topics(text: str) -> dict[str, str]:
    """Map every topic key to its part of *text*, or fail loudly about the guide.

    Three directions are checked, because only all three together are a guarantee:

    * a fragment that matches nothing means a heading was reworded,
    * a fragment that matches twice, or a part claimed by two keys, means a heading was added that
      an existing topic now also claims, and an unclaimed part is a section no topic can reach,
    * and the keys must resolve in DOCUMENT ORDER, which is the check that catches a SWAP. The
      first two pass happily on two exchanged fragments, and the caller is then served the wrong
      section with nothing saying so.
    """
    parts = _addressable_parts(text)
    headings = [heading for heading, _ in parts]

    resolved: dict[str, str] = {}
    claimed: dict[int, str] = {}
    previous = -1
    for key, fragment in TOPICS.items():
        hits = [index for index, heading in enumerate(headings) if fragment in heading]
        if len(hits) != 1:
            raise GuideDriftError(
                f"the operator guide drifted: the topic '{key}' anchors on {fragment!r}, which "
                f"matches {len(hits)} headings instead of exactly one. Headings found: {headings}",
                details={"topic": key, "fragment": fragment, "matches": len(hits)},
            )
        index = hits[0]
        if index in claimed:
            raise GuideDriftError(
                f"the operator guide drifted: the topics '{claimed[index]}' and '{key}' both "
                f"resolve to the heading {headings[index]!r}",
                details={"topics": [claimed[index], key], "heading": headings[index]},
            )
        if index <= previous:
            raise GuideDriftError(
                f"the operator guide drifted: the topic '{key}' resolves to the heading "
                f"{headings[index]!r}, which comes BEFORE the part the previous topic "
                f"'{claimed[previous]}' claims. TOPICS is written in document order; either the "
                f"document was reordered or two anchors were swapped, and a swap would otherwise "
                f"serve the wrong section without a word",
                details={"topic": key, "heading": headings[index]},
            )
        previous = index
        claimed[index] = key
        resolved[key] = parts[index][1]

    unclaimed = [heading for index, heading in enumerate(headings) if index not in claimed]
    if unclaimed:
        raise GuideDriftError(
            f"the operator guide drifted: these parts have no topic and cannot be requested "
            f"individually: {unclaimed}. Add a key to TOPICS.",
            details={"unclaimed": unclaimed},
        )
    return resolved


def serve_guide(topic: str | None = None) -> str:
    """The whole guide, or the one part *topic* names.

    Absent or blank both mean the whole document: "no argument" is not a typo, and a caller who
    omits it has asked for everything. An unknown NON-blank topic is a hard error naming every
    valid key, never a nearest-neighbour hit and never a silent fall back to the whole guide. A
    typo that answers with something plausible costs the reader more than one that refuses,
    because nothing tells them they got the wrong section.
    """
    text = get_guide()
    key = (topic or "").strip()
    if not key:
        return text

    resolved = resolve_topics(text)
    part = resolved.get(key)
    if part is None:
        raise UnknownTopicError(
            f"no topic '{key}'. The topics are: {', '.join(TOPICS)}. "
            f"Omit the argument for the whole guide.",
            details={"topic": key, "known_topics": list(TOPICS)},
        )
    return part
