"""Drift guard: the answer-field index must name real fields and route them to the right topic.

**What the index is for.** A caller meets a field name in an ANSWER, not in a question, and this
surface deliberately has no free-text search (``tools/guide.py`` states that and gives the reason).
So a field name was, measured, unreachable: it rode on the wire in an ``outputSchema`` or in a
report and nothing mapped it to the topic that explains it. The ``answer-fields`` topic is that map.
It is an INDEX and not a second glossary, which is exactly what makes it guardable: an index makes
two claims per row, both mechanical, and this module holds both.

**The two claims.**

* the field EXISTS, measured against the response keys this package actually builds, so the index
  cannot name a field that was renamed away or never existed;
* the topic beside it EXPLAINS it, measured as the field name occurring in the text that topic
  serves, so a row cannot point a reader at a section that never mentions the word.

The second is the one that pays. Writing the index is cheap and it silently rots the moment a
paragraph is moved between topics, and a reader following a wrong route has no way to tell: they get
a real section, in full, without the word they came for. Measured while this index was first
written, 19 of its rows failed this check on the first run.

**Why the population is derived rather than listed.** A list would be a second copy of the response
models and would rot with them. It is read out of the AST instead, from three places a response key
can be born in this package: an annotated field in a class body (the pydantic models, the
``TypedDict``s and the ``NamedTuple``s), a string key of a dict literal (the tools that build their
answer by hand), and a string element of a module-level allowlist (the projections, where a field
such as ``config_msg`` exists only as a member of a ``frozenset``).

⚠️ **A derived population is only a guard while it is NOT EMPTY**, and that failure mode is the
whole reason the floors below exist rather than a bare loop: the first draft of this module filtered
on ``@dataclass``, and there is not a single dataclass in the service layer, so it passed on an
empty set and proved nothing. A mutant cannot redden a guard that iterates over nothing, so the
floors are asserted first and separately, and they are what makes the rest of the module honest.

Provably red (each verified by mutating the shipped guide, never a fixture):

* rename a field in the index table -> ``test_every_indexed_field_is_a_real_response_key``;
* point a row at a topic that does not exist -> ``test_every_indexed_topic_exists``;
* point a row at a real topic that does not mention the field ->
  ``test_every_indexed_field_appears_in_the_topic_it_names``;
* break the AST walk so it collects nothing -> ``test_the_derived_population_is_not_empty``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from epics_mcp.resources import get_guide
from epics_mcp.tools.guide import TOPICS, resolve_topics

#: The package whose response keys the index is allowed to name.
_SRC = Path(__file__).resolve().parents[1] / "src" / "epics_mcp"

#: The index lives under this topic. Named once, so a rename is one edit and a loud one.
_INDEX_TOPIC = "answer-fields"

#: A table row of the index: backticked field names on the left, backticked topic keys on the
#: right. Anchored on the leading pipe plus a backtick so the header and the separator row cannot
#: match, and so prose paragraphs above the table contribute nothing.
_ROW = re.compile(r"^\|\s*(`[^|]+)\|([^|]+)\|\s*$", re.MULTILINE)

#: Any backticked span, used on BOTH sides of a row.
#:
#: ⛔ Deliberately shape-BLIND, and that is the whole point rather than laziness. The first draft
#: matched ``[a-z_][a-z0-9_]*`` on the left and ``[a-z][a-z-]*`` on the right, which reads as
#: precision and behaves as a silent skip: a corrupted entry simply stops matching and drops out of
#: the population instead of failing. Measured, that made two of this module's four mutants stay
#: GREEN (``critical_uncoveredX`` and ``postureX`` both vanished rather than being rejected). A
#: guard that only inspects the entries it already likes cannot fail on the entries it does not.
_SPAN = re.compile(r"`([^`]+)`")

#: Floors under the two derived quantities. They are ROUND NUMBERS well below the measured values
#: (444 response keys and 16 index rows on 2026-08-16) rather than pins: the point is to catch a
#: derivation that collapsed to nothing or a table that was emptied, never to freeze a count that
#: every ordinary edit would move.
_MIN_RESPONSE_KEYS = 100
_MIN_INDEX_ROWS = 8


def _response_keys() -> set[str]:
    """Every name this package can put in an answer, read out of the AST of ``src/epics_mcp``.

    Three births, because a response key has three in this package and reading only one of them is
    how a guard ends up green on an empty set (see the module docstring):

    * an annotated name in a class body: the pydantic report models, the ``TypedDict`` results and
      the ``NamedTuple`` rows;
    * a string key of a dict literal: the tools that assemble their answer directly;
    * a string element of a list/tuple/set literal: the projection allowlists, where a field exists
      only as a member (``config_msg`` is the measured case).

    Deliberately a UNION rather than a per-file mapping: the index answers "does this field exist at
    all", and pinning which module owns a field would make an ordinary move a red test.
    """
    keys: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                keys.update(
                    statement.target.id
                    for statement in node.body
                    if isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                )
            elif isinstance(node, ast.Dict):
                keys.update(
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
            elif isinstance(node, ast.List | ast.Tuple | ast.Set):
                keys.update(
                    element.value
                    for element in node.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
    return keys


def _index_rows() -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """The index as (fields, topics) pairs, read out of the SHIPPED guide.

    Read from ``resolve_topics`` rather than from the raw file, so the rows are the ones a caller
    actually receives: a table that drifted out of the ``answer-fields`` part would be invisible
    here, which is the honest scope of this guard rather than a gap in it.
    """
    body = resolve_topics(get_guide())[_INDEX_TOPIC]
    rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for left, right in _ROW.findall(body):
        fields = tuple(_SPAN.findall(left))
        topics = tuple(_SPAN.findall(right))
        if fields:
            rows.append((fields, topics))
    return rows


def test_the_derived_population_is_not_empty() -> None:
    """The floor that makes every other test in this module mean something.

    ⚠️ Asserted separately and first, because a guard iterating over an empty population is green
    for the wrong reason and no mutant can redden it. That is not hypothetical: the first draft of
    this module read ``@dataclass`` decorators, of which the service layer has none.
    """
    keys = _response_keys()
    assert len(keys) >= _MIN_RESPONSE_KEYS, (
        f"only {len(keys)} response keys were derived from {_SRC}, below the floor of "
        f"{_MIN_RESPONSE_KEYS}. The AST walk is broken or the layout moved; fix the walk rather "
        "than lowering the floor, because every other assertion here is vacuous without it."
    )
    rows = _index_rows()
    assert len(rows) >= _MIN_INDEX_ROWS, (
        f"the answer-field index has {len(rows)} rows, below the floor of "
        f"{_MIN_INDEX_ROWS}. Either "
        f"the table under the '{_INDEX_TOPIC}' topic was emptied, or its shape changed and the row "
        "pattern no longer matches it."
    )


def test_every_indexed_field_is_a_real_response_key() -> None:
    """A field named in the index must be one this package can actually put in an answer."""
    keys = _response_keys()
    unknown = sorted(
        {field for fields, _ in _index_rows() for field in fields if field not in keys}
    )
    assert not unknown, (
        f"the answer-field index names {len(unknown)} field(s) that no response of this package "
        f"builds: {', '.join(unknown)}. Either the field was renamed and the index was not, or the "
        "index invented it. An index that names a field a caller can never receive sends them "
        "looking for a word that is not there."
    )


def test_every_indexed_topic_exists() -> None:
    """A topic named in the index must be a key ``get_guide`` accepts."""
    unknown = sorted(
        {topic for _, topics in _index_rows() for topic in topics if topic not in TOPICS}
    )
    assert not unknown, (
        f"the answer-field index routes to {len(unknown)} topic(s) that do not exist: "
        f"{', '.join(unknown)}. get_guide refuses those with [UNKNOWN_TOPIC], so the row is a dead "
        "end. Valid keys: " + ", ".join(TOPICS)
    )


def test_every_indexed_field_appears_in_the_topic_it_names() -> None:
    """The routing claim itself: the named topic has to MENTION the field.

    This is the assertion the index exists for. The two above catch a name that is wrong; this one
    catches a name that is right and pointed at the wrong place, which is the failure a reader
    cannot detect for themselves: they receive a real section, in full, without the word they came
    for. Measured on the first run of the finished index, 19 rows failed exactly this way.
    """
    parts = resolve_topics(get_guide())
    misrouted: list[str] = []
    for fields, topics in _index_rows():
        for topic in topics:
            body = parts.get(topic)
            if body is None:  # covered by test_every_indexed_topic_exists
                continue
            misrouted.extend(f"{field} -> {topic}" for field in fields if field not in body)
    assert not misrouted, (
        f"{len(misrouted)} index row(s) route a field to a topic whose text never names it: "
        + "; ".join(sorted(misrouted))
        + ". Either explain the field in that topic, or point the row at the topic that does."
    )


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_topic_still_serves_something(topic: str) -> None:
    """A companion floor: no topic key may serve an empty part.

    Cheap, and it closes the one way the routing test above could be satisfied dishonestly, by
    pointing rows at a topic that resolves to nothing at all.
    """
    body = resolve_topics(get_guide())[topic]
    assert body.strip(), f"the topic '{topic}' resolves to an empty part"
