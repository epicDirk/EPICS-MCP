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

**The third claim, which the table makes as a WHOLE rather than per row.** It is a SET of routes:
each field name occurs once, and every topic it reaches stays reached. Both halves guard the same
failure, a row that goes away, and that failure was guarded until 2026-08-20 by a floor under the
ROW COUNT, which nothing falls through as long as something takes the row's place. Measured that
day against the shipped guide: overwriting one row with a copy of another deletes a route, holds the
count at 20, and left the WHOLE suite green, 2721 passed and 63 skipped at exit 0. A count was the
wrong quantity in both directions, because it would equally have gone red on a harmless merge of two
rows that share a topic, where no route is lost at all.

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
* break the AST walk so it collects nothing -> ``test_the_derived_population_is_not_empty``;
* replace a row with a copy of another one, leaving the row count untouched ->
  ``test_the_index_is_a_set_of_distinct_routes``, and the field floor in
  ``test_the_derived_population_is_not_empty``;
* delete the last row that points at a topic -> ``test_every_indexed_topic_keeps_its_route``.
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

#: Any line of the index TABLE, matched on the leading pipe alone.
#:
#: ⛔ Deliberately loose, because the strictness belongs in the ASSERTION and not in the match. An
#: earlier version anchored on ``^\|\s*(`[^|]+)\|([^|]+)\|\s*$``, which reads as precision and is a
#: silent skip: measured, a row that lost a backtick, gained a third column or picked up an indent
#: simply stopped matching and left the table with the whole suite green. Every table line is now
#: taken and then has to PROVE it is well formed, so a malformed row is a failure rather than an
#: absence. That is the same lesson as ``_SPAN`` below, one level out.
_TABLE_LINE = re.compile(r"^[ \t]*\|.*$", re.MULTILINE)

#: The separator line of a markdown table (``|---|---|``), the one table line that is not a row.
_SEPARATOR = re.compile(r"^[ \t]*\|[\s:|-]+\|[\s:|-]*$")

#: A well-formed row: exactly two cells, the first opening with a backticked span.
_ROW = re.compile(r"^\|\s*(`[^|]+)\|([^|]+)\|\s*$")

#: Any backticked span, used on BOTH sides of a row.
#:
#: ⛔ Deliberately shape-BLIND, and that is the whole point rather than laziness. The first draft
#: matched ``[a-z_][a-z0-9_]*`` on the left and ``[a-z][a-z-]*`` on the right, which reads as
#: precision and behaves as a silent skip: a corrupted entry simply stops matching and drops out of
#: the population instead of failing. Measured, that made two of this module's four mutants stay
#: GREEN (``critical_uncoveredX`` and ``postureX`` both vanished rather than being rejected). A
#: guard that only inspects the entries it already likes cannot fail on the entries it does not.
_SPAN = re.compile(r"`([^`]+)`")

#: A response key has to look like one: lowercase, identifier-shaped. The population is read out of
#: literals, so without this it also carries env-var names, MIME types, CLI flags and whole English
#: sentences, and an index row naming ``EPICS_MCP_ALLOW_OLOG_WRITE`` as "a field in the answer"
#: passed every assertion here (measured).
#:
#: ⚠️ HONEST LIMIT, stated rather than implied: this still admits INPUT argument names such as
#: ``timeout`` or ``displays_dir``, because they are dict keys in this package too. So the guard
#: answers "is this a name this package uses in a payload", not "is this an OUTPUT field". The
#: routing assertion is what catches such a row in practice, since an argument name is not
#: explained in an answer-field topic.
_KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Floors under the two derived quantities, both RATCHETS rather than round numbers.
#:
#: ⛔ ``_MIN_INDEXED_FIELDS`` replaced a floor under the number of TABLE ROWS on 2026-08-20, and
#: the reason is that a row count is a PRESENTATION quantity while the content of an index is the
#: set of NAMES it routes. The gap was measured rather than argued: overwriting the
#: ``attachment_count`` row of the shipped guide with a second copy of the ``reach`` row deletes a
#: route, holds the count at 20, and left the whole suite green. The floor now sits under the
#: DISTINCT FIELD NAMES, so a row that is replaced rather than removed still falls through it.
#:
#: ⚠️ The predecessor of that predecessor sat at 8 against 16 measured rows, which reads as "a
#: floor against empty" and works as "half the index may vanish silently": measured, deleting 8
#: rows left the suite green. A ratchet at the real count is the honest shape, so raise it in the
#: same commit that adds fields. ``_MIN_RESPONSE_KEYS`` stays a round floor because its only job is
#: to catch a broken AST walk; the real check on that side is the field-existence assertion.
#: Re-measured 2026-08-20 with ``len(_response_keys())`` and
#: ``len({f for fs, _ in _index_rows() for f in fs})``: 480 and 66, across 20 rows
#: (16 rows on 2026-08-16; GQ-33 added four, two for the write-answer fields and two for an
#: archived sample, the second of those because ``severity`` has a live-read meaning as well and a
#: bare row would have routed one name to two subjects). The key population read 477 on
#: 2026-08-19 and moves with ordinary code changes, which is why its floor is round and far below,
#: while the index floor sits exactly on the measured count.
#: ⚠️ An earlier version of this comment claimed 444, a draft figure taken before the third birth
#: was added and never re-measured, and a second draft claimed 597, written before the shape filter
#: above cut the population down. Both are named because the same slip happened twice in one hour:
#: a figure copied forward while the thing it counts kept moving.
_MIN_RESPONSE_KEYS = 100
_MIN_INDEXED_FIELDS = 66

#: The topics the index routes to, frozen as a SET so that losing the LAST route to one is loud.
#:
#: Measured 2026-08-20 with ``sorted({t for _, ts in _index_rows() for t in ts})``: 11 of the 34
#: keys in ``TOPICS``. Growing this set is ordinary and needs no ceremony; shrinking it means a
#: section became unreachable for the one reader this index was written for, someone holding a
#: field name on a surface with no free-text search.
#:
#: ⚠️ HONEST LIMIT, stated rather than implied, because this is a ratchet with NAMES and not a
#: derivation. A derivation was measured first and rejected on the measurement: the best candidate,
#: "a response key that exactly one topic besides ``answer-fields`` names as code", selects 126
#: keys of which the index carries 48, so binding to it would have reddened the HEALTHY index on 78
#: counts. No mechanical criterion separates the fields that need a route from the 480 keys that do
#: not, so the codomain is pinned instead of derived. What that leaves open is said plainly:
#: deleting a row AND its topic here in one edit stays green. The difference to a bare count is
#: that such a deletion then carries a name and stands in the diff.
_INDEXED_TOPICS = frozenset(
    {
        "alarm-tree",
        "doctor",
        "err-archiver",
        "err-crossplane",
        "err-displays",
        "err-pv",
        "err-rest",
        "olog-filters",
        "olog-output",
        "posture",
        "pv-write",
    }
)


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
    return {key for key in keys if _KEY_SHAPE.match(key)} - set(TOPICS)


def _index_rows() -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """The index as (fields, topics) pairs, read out of the SHIPPED guide.

    Read from ``resolve_topics`` rather than from the raw file, so the rows are the ones a caller
    actually receives: a table that drifted out of the ``answer-fields`` part would be invisible
    here, which is the honest scope of this guard rather than a gap in it.
    """
    body = resolve_topics(get_guide())[_INDEX_TOPIC]
    rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    malformed: list[str] = []
    for line in _TABLE_LINE.findall(body):
        if _SEPARATOR.match(line) or line.lstrip().startswith("| A field in the answer"):
            continue
        match = _ROW.match(line)
        fields = tuple(_SPAN.findall(match.group(1))) if match else ()
        topics = tuple(_SPAN.findall(match.group(2))) if match else ()
        if not fields or not topics:
            malformed.append(line.strip())
            continue
        rows.append((fields, topics))
    assert not malformed, (
        f"{len(malformed)} line(s) of the answer-field table are not well-formed rows: "
        + " | ".join(malformed)
        + ". A row needs exactly two cells, at least one backticked field on the left and at least "
        "one backticked topic on the right. A row that merely stops matching would leave the table "
        "unchecked while every assertion below stayed green, so it is refused here instead."
    )
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
    fields = {field for row_fields, _ in _index_rows() for field in row_fields}
    assert len(fields) >= _MIN_INDEXED_FIELDS, (
        f"the answer-field index routes {len(fields)} distinct field name(s), below the floor of "
        f"{_MIN_INDEXED_FIELDS}. Either rows were removed from the table under the "
        f"'{_INDEX_TOPIC}' topic, or its shape changed and the row pattern no longer matches it. "
        "The floor holds NAMES rather than rows on purpose: a row count cannot see a row that was "
        "replaced by a copy of another one, and that is how a route disappears unnoticed."
    )


def test_the_index_is_a_set_of_distinct_routes() -> None:
    """The index is a SET: no field name may be routed twice, so no row may repeat another.

    ⛔ This is the assertion that catches a row being REPLACED rather than removed, and the gap
    it closes was measured rather than reasoned about. On 2026-08-20 the ``attachment_count`` row of
    the shipped guide was overwritten with a second copy of the ``reach`` row, which deletes a route
    while leaving the row count at 20, and the entire suite stayed green: 2721 passed, 63 skipped,
    exit 0. A count cannot see that edit. Set-ness can, because the substitute is a repetition of
    something already in the table.

    The property is real rather than merely convenient: an index names each field once and points it
    at the one topic that explains it, so a name occurring twice is either a contradiction between
    two routes or dead weight. The shipped table has neither, measured the same day at 66 cells and
    66 distinct names, which is what makes this safe to assert rather than aspirational.

    A duplicated ROW needs no assertion of its own: its fields repeat, so it fails here first.
    """
    fields = [field for row_fields, _ in _index_rows() for field in row_fields]
    repeated = sorted({field for field in fields if fields.count(field) > 1})
    assert not repeated, (
        f"the answer-field index routes {len(repeated)} field name(s) more than once: "
        f"{', '.join(repeated)}. Either two rows contradict each other about which topic explains "
        "the field, or a row was overwritten with a copy of another one. The second is how a route "
        "disappears while the row count stays put, which is the failure this assertion exists for."
    )


def test_every_indexed_topic_keeps_its_route() -> None:
    """Every topic the index reaches has to stay reachable through it.

    The companion to the field floor, one level out: the floor holds the index's DOMAIN, this holds
    its CODOMAIN. Losing the last row that points at a topic makes that section unreachable for a
    caller who holds a field name and nothing else, and nothing else in this repository would
    notice: the guide still serves the section, ``resolve_topics`` still claims it, and every other
    assertion in this module is about the rows that ARE there.
    """
    routed = {topic for _, topics in _index_rows() for topic in topics}
    stale = sorted(_INDEXED_TOPICS - set(TOPICS))
    assert not stale, (
        f"{len(stale)} pinned topic(s) are no longer keys of TOPICS: {', '.join(stale)}. The pin "
        "went stale rather than the index. Re-measure it against the renamed key instead of "
        "dropping the entry, or the ratchet quietly stops holding that route."
    )
    lost = sorted(_INDEXED_TOPICS - routed)
    assert not lost, (
        f"the answer-field index no longer routes to {len(lost)} topic(s) it used to explain: "
        f"{', '.join(lost)}. A caller holding a field name explained there now has no route to it, "
        "and this surface has no free-text search to fall back on. Restore the row, or, if that "
        "section genuinely stopped carrying answer fields, remove the name from _INDEXED_TOPICS in "
        "the same commit and say why."
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


def _named_as_code(field: str, body: str) -> bool:
    """True when *body* names *field* inside a CODE SPAN rather than in running prose.

    ⛔ A plain substring test is not enough here, and the gap was measured rather than imagined:
    the index once routed ``found`` to the PV-connection signatures, where the only occurrences are
    the heading "not found vs not registered" and the sentence "the network search found no
    server". Both are ordinary English, the row passed, and a reader following it got a full
    section without their field in it. Requiring a code span rules that class out.

    ⚠️ The span may carry a VALUE, which is why this is a prefix test and not equality: the guide
    writes ``registered:false``, ``found:null``, ``total: 0`` and ``cf_capped: true``, and each of
    those is the field named properly. What it must not match is a LONGER identifier, so the next
    character has to end the name.
    """
    return any(
        span == field or (span.startswith(field) and not _KEY_SHAPE.match(span[len(field)]))
        for span in _SPAN.findall(body)
    )


def test_every_indexed_field_appears_in_the_topic_it_names() -> None:
    """The routing claim itself: the named topic has to NAME the field, as code.

    This is the assertion the index exists for. The two above catch a name that is wrong; this one
    catches a name that is right and pointed at the wrong place, which is the failure a reader
    cannot detect for themselves: they receive a real section, in full, without the word they came
    for. Measured on the first run of the finished index, 19 rows failed exactly this way, and a
    twentieth (``found``) survived a plain substring test and was caught only by the code-span
    requirement in :func:`_named_as_code`.
    """
    parts = resolve_topics(get_guide())
    misrouted: list[str] = []
    for fields, topics in _index_rows():
        for topic in topics:
            body = parts.get(topic)
            if body is None:  # covered by test_every_indexed_topic_exists
                continue
            misrouted.extend(
                f"{field} -> {topic}" for field in fields if not _named_as_code(field, body)
            )
    assert not misrouted, (
        f"{len(misrouted)} index row(s) route a field to a topic that never names it as code: "
        + "; ".join(sorted(misrouted))
        + ". Either explain the field in that topic, or point the row at the topic that does. A "
        "bare English occurrence of the word does not count: that is how a misroute survived once."
    )


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_topic_still_serves_something(topic: str) -> None:
    """A companion floor: no topic key may serve an empty part.

    Cheap, and it closes the one way the routing test above could be satisfied dishonestly, by
    pointing rows at a topic that resolves to nothing at all.
    """
    body = resolve_topics(get_guide())[topic]
    assert body.strip(), f"the topic '{topic}' resolves to an empty part"
