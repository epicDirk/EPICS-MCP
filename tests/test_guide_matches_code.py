"""Drift guard: the guide's tool, env and doctor-status surfaces must match the code.

The guide is hand-written prose that names MCP tools, ``EPICS_MCP_*`` env vars and the plane
statuses ``epics-doctor`` prints. Without a guard, a renamed tool or a removed env var makes the
guide silently wrong, it would tell a fresh session to call a tool that no longer exists, exactly
when the session trusts it. This binds the guide's tool-inventory section to the ``@mcp.tool``
registrations and its ``EPICS_MCP_*`` mentions to ``EpicsConfig``: the same anti-drift pattern the
repo already uses for README resource URIs.

Three surfaces, each anchored on a marked region rather than on a line number:

* the tool inventory, against the registrations;
* every ``EPICS_MCP_*`` mention, against ``EpicsConfig``;
* the status legend and the statuses named in the prose above it, against ``PlaneStatus`` and
  ``cli_doctor._STATUS_MARK`` (QA-47). This one reaches beyond the guide: a glyph paired with a
  status name is also checked on ``docs/tools.md`` and ``docs/deployment.md``, because a second
  copy of a guarded fact is an unguarded fact.

What is still deliberately outside: the free-form Archiver MGMT verbs (``getAllPVs`` /
``getPVsForThisAppliance``) are manual REST recipes with no implementing tool, so they are
documented in prose and never checked here, and the legend's Meaning column is prose that has to
stay free to improve. What the status half does NOT check is written down and dated in
``docs/known-limits.md`` section 14.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import get_args

from epics_mcp import cli_doctor
from epics_mcp.config import EpicsConfig
from epics_mcp.resources import get_guide
from epics_mcp.services.doctor import PlaneStatus

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src" / "epics_mcp"
_SERVER = _SRC / "server.py"
_DISPLAY_TOOLS = _SRC / "display_tools.py"


def _tools_decorated_with_mcp_tool(source: str) -> set[str]:
    """Async functions decorated with ``@mcp.tool(...)``, the server.py inline tool set."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                names.add(node.name)
    return names


def _toplevel_async_defs(source: str) -> set[str]:
    """Top-level async function names, display_tools.py registers each as a tool."""
    return {
        node.name
        for node in ast.iter_child_nodes(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _registered_tool_names() -> set[str]:
    core = _tools_decorated_with_mcp_tool(_SERVER.read_text(encoding="utf-8"))
    # AST-scan display_tools.py (not import) so the set holds with OR without the [displays] extra.
    displays = _toplevel_async_defs(_DISPLAY_TOOLS.read_text(encoding="utf-8"))
    return core | displays


_INVENTORY_RE = re.compile(
    r"<!-- BEGIN:tool-inventory.*?-->(.*?)<!-- END:tool-inventory -->", re.DOTALL
)
# Backticked snake_case tokens (every tool name has an underscore; excludes camelCase REST verbs,
# ``.RTYP``, ``[displays]``, and single words).
_TOOL_TOKEN_RE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
_ENV_RE = re.compile(r"EPICS_MCP_[A-Z][A-Z0-9_]*")


def _guide_tool_tokens() -> set[str]:
    match = _INVENTORY_RE.search(get_guide())
    assert match, "guide tool-inventory markers not found, the drift anchor broke"
    return set(_TOOL_TOKEN_RE.findall(match.group(1)))


def test_guide_tool_inventory_matches_registrations() -> None:
    """Set equality both ways: no dead tool named in the guide (drift), no registered tool missing
    from the guide (the Knowledge-Persistence-Policy's teeth, a new tool must be documented)."""
    guide = _guide_tool_tokens()
    code = _registered_tool_names()
    # Non-empty floor (F23): set-equality passes vacuously as empty == empty if either
    # scanner silently returns nothing (renamed decorator, reformatted inventory). Anchor
    # both sides, like test_every_level_parameter_points_at_list_log_levels does.
    assert guide, "guide tool inventory is empty, the inventory/token anchor broke"
    assert code, "no registered tools found, the @mcp.tool / display AST anchor broke"
    assert guide == code, (
        f"guide tool inventory ↔ code drift: only-in-guide={sorted(guide - code)} "
        f"only-in-code={sorted(code - guide)}"
    )


def test_guide_env_vars_exist() -> None:
    allowed = {"EPICS_MCP_" + name.upper() for name in EpicsConfig.model_fields}
    mentioned = set(_ENV_RE.findall(get_guide()))
    # Non-empty floor (F23): the subset check passes vacuously when mentioned is empty.
    # A silently-vanished EPICS_MCP_* surface would keep this guard green while guarding
    # nothing; the guide documents the config, so >=1 mention is a real invariant.
    assert mentioned, "guide mentions no EPICS_MCP_* vars, the env anchor broke"
    unknown = mentioned - allowed
    assert not unknown, f"guide mentions unknown EPICS_MCP_* env vars: {sorted(unknown)}"


def _level_param_descriptions(source: str) -> dict[str, str]:
    """``{tool_name: description}`` for every tool parameter literally named ``level``.

    Reads the ``Field(description=...)`` out of the ``Annotated[...]`` annotation. Adjacent string
    literals inside the parentheses are folded into one constant by the parser, so a multi-line
    description arrives here as a single string.
    """
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            if arg.arg != "level" or not isinstance(arg.annotation, ast.Subscript):
                continue
            for elt in ast.walk(arg.annotation):
                if not (isinstance(elt, ast.Call) and getattr(elt.func, "id", "") == "Field"):
                    continue
                for kw in elt.keywords:
                    if kw.arg == "description" and isinstance(kw.value, ast.Constant):
                        found[node.name] = str(kw.value.value)
    return found


def test_every_level_parameter_points_at_list_log_levels() -> None:
    """Levels are SITE-CONFIGURABLE, so no description may imply a fixed enum, each one has to send
    the caller to ``list_log_levels``.

    This is the only guard on these strings: nothing else in the suite reads a parameter
    description, so without it the four ``level`` surfaces drift apart silently, which is exactly
    how ``update_log_entry`` came to say only "New entry level/type. Omit to leave unchanged" while
    its siblings said more.
    """
    descriptions = _level_param_descriptions(_SERVER.read_text(encoding="utf-8"))
    assert descriptions, "no level parameters found, the AST anchor broke"
    missing = sorted(name for name, text in descriptions.items() if "list_log_levels" not in text)
    assert not missing, f"level description does not mention list_log_levels: {missing}"


# --- the doctor status surface (QA-47) ----------------------------------------------------------

# Which marked region of the SHIPPED guide names each ``PlaneStatus``. The buckets are LOCATIONS,
# not meanings ("named in the glyph legend", "named in the prose paragraph above it", "named in
# neither"), so a status that gets documented later simply moves bucket, and the move stays a true
# statement about where it now stands.
#
# Declared rather than derived, and that was measured rather than assumed: no boolean combination
# of the code-side facts (the status frozensets in ``services/doctor.py``, the ``_REMEDY`` keys,
# "carries a mark of its own") produces the legend's membership. Which statuses the legend carries
# is an editorial choice, and only an explicit partition lets the tiling test below prove the three
# buckets cover ``PlaneStatus`` exactly: the same shape, and the same reason, as the three sets
# ``test_status_partition_is_total_and_disjoint`` tiles.
#
# Typed on ``PlaneStatus`` rather than on ``str``, and the LITERAL element type is the point of the
# annotation: it is what turns a dead or misspelled bucket entry into a mypy error naming the
# offending literal, which the tiling test below cannot do on its own (a typo and a rename look
# alike to it). Keep each bucket a literal set of constants. Widening them back to
# ``frozenset[str]`` is the cheap green repair that silently takes the lint away, and a bucket built
# from a comprehension is a mypy error whose cheapest repair is to delete the annotation.
_IN_THE_GLYPH_TABLE: frozenset[PlaneStatus] = frozenset(
    {"ok", "unverified", "identity_probe_failed", "config_error", "backend_down", "no_ingest"}
)
_IN_THE_STATUS_PROSE: frozenset[PlaneStatus] = frozenset({"ca_error", "api_error", "unreachable"})
# A MEASURED GAP, not a decision (2026-07-30): the shipped guide never names these three by their
# status name, and their marks have no legend in it at all. Declared here so the gap is
# machine-visible and cannot go stale in silence, rather than living only in a backlog; documenting
# any of them inside a marked region reddens the location test below, which is the point.
_NOT_NAMED_IN_THE_GUIDE: frozenset[PlaneStatus] = frozenset({"disabled", "info", "disconnected"})

_GLYPH_TABLE_RE = re.compile(
    r"<!-- BEGIN:status-glyphs.*?-->(.*?)<!-- END:status-glyphs -->", re.DOTALL
)
_STATUS_PROSE_RE = re.compile(
    r"<!-- BEGIN:status-prose.*?-->(.*?)<!-- END:status-prose -->", re.DOTALL
)
# ``| `<mark>` | `<status>` | ...``: the two cells this file compares. The Meaning column is
# deliberately not captured, a guard over it would pin prose that has to stay free to improve
# (``docs/known-limits.md`` section 13 rejects exactly that for the sibling remedy guard).
_GLYPH_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([a-z_]+)`\s*\|")
_BACKTICKED_RE = re.compile(r"`([^`\s]+)`")
#: EVERY code span of a line, including the whitespace-free ones. Matching them ALL and filtering
#: afterwards is what keeps the backtick pairing honest, see ``_glyph_status_pairings``.
_CODE_SPAN_RE = re.compile(r"`([^`]*)`")
#: The characters this project uses as epics-doctor marks. DECLARED rather than derived from
#: ``_STATUS_MARK.values()`` on purpose: a mark RETIRED from the mapping has to stay recognisable,
#: or the scan stops seeing the stale documentation copies at the exact moment they become stale.
#: Measured on this tree by retiring ``~``: the derived rule went on reading fewer pairings than
#: exist and reported NONE of the stale ones, while the declared class reports every one. The set
#: only grows, and ``test_the_glyph_class_covers_every_mark_the_cli_prints`` reddens if a mark is
#: added to the CLI without being added here.
#:
#: Do NOT replace this with ``token in marks.values()``: that IS the blindness above, and it is also
#: the cheapest green repair, so it is named here rather than left to be rediscovered. Only the
#: property pin ``test_a_retired_mark_is_still_read_as_a_pairing`` sees that revert; a floor over
#: the FORM of the rule stays vacuously green under it.
#:
#: Two more open rules were built, measured and rejected. Both buy the same stale pairings, and both
#: pay for them with false reds on correct FUTURE prose that the declared class does not produce:
#:
#: * "a single character that is not a digit" reads a ``Y``/``N`` support column, a placeholder
#:   ``x`` and the ``-`` of an empty table cell as marks.
#: * "not alphanumeric OR a current mark" still reads that ``-``.
#:
#: What the declared class does NOT repair, because the current rule misreads it too: a status name
#: written next to a mark that is CONTRASTED with it rather than paired to it, as in "the ``ok``
#: versus ``?`` distinction". That form is recorded in ``docs/known-limits.md`` section 14.
_GLYPH_CHARACTERS = frozenset("✓✗?!~·i")
# The prose surfaces that re-state a pairing the legend already carries. They do NOT ship in the
# wheel, unlike the guide, but a wrong glyph in them misleads a reader just the same and the cost
# of covering them is one path each.
_DOC_SURFACES = ("docs/tools.md", "docs/deployment.md")


def _guide_region(pattern: re.Pattern[str], name: str) -> str:
    match = pattern.search(get_guide())
    assert match, f"the guide's {name} markers were not found, the drift anchor broke"
    return match.group(1)


def _glyph_rows() -> list[tuple[str, str]]:
    """The ``(mark, status)`` cells of the shipped legend: every row, or a red test.

    Both floors sit BEFORE any caller compares sets, the ordering
    ``test_guide_tool_inventory_matches_registrations`` already uses (its anchor assertions run
    before its set equality). Either floor placed after a comparison is dead code: the comparison
    fails first and reports a documentation gap where in truth the anchor broke.
    """
    rows: list[tuple[str, str]] = []
    for line in _guide_region(_GLYPH_TABLE_RE, "status-glyphs").splitlines():
        if not line.startswith("|") or line.startswith(("| Mark ", "|--")):
            continue
        match = _GLYPH_ROW_RE.match(line)
        assert match, (
            "a row of the shipped glyph legend does not parse as `mark` | `status` | ...: "
            f"{line!r}. Without this floor it would drop out of the comparison in silence and the "
            "guard would check less than it claims to."
        )
        rows.append((match.group(1), match.group(2)))
    assert rows, "the shipped glyph legend parsed no rows at all, the table anchor broke"
    return rows


def _glyph_status_pairings(text: str, marks: Mapping[str, str]) -> list[tuple[str, str]]:
    """Every ``(status, mark)`` the text sets side by side, in either order.

    Consecutive backticked tokens separated by at most three characters, none of them a word
    character: ```no_ingest` (`~`)``, ```!`
    `identity_probe_failed``` and the legend's own ``| `~` |
    `no_ingest` |``. Pairs are read from OVERLAPPING neighbours rather
    than by scanning left to right and consuming: a non-consuming scan is what makes
    ``exit `3`) and `~``` stop hiding the pairing on either side of it.

    Whitespace is collapsed PER BLOCK, blocks being split on a blank line. Collapsing whole RUNS of
    whitespace rather than newlines alone is what carries a pairing over an INDENTED line break:
    measured, ``docs/deployment.md`` splits one across exactly that, and joining the lines with a
    single space still left the indentation in the gap, pushing it past three characters. The
    sibling pairings in the same sentence were read while that one was dropped, which is the worst
    shape of a gap: silent and selective. Per block rather than over the whole text, because
    flattening the file into one line would let a paragraph ENDING on a status name pair with the
    next one BEGINNING on a mark. Measured, both variants read the same pairings on this tree
    today, so the stricter one is free and closes that class of false red before it can appear.

    A SECOND pass reads the other written form: a single code span holding exactly two tokens, one
    a status and the other a mark, as in ```✓ ok```. That form exists twice on these surfaces and
    was unreachable for the pass above, whose token regex forbids whitespace inside a span.

    The ORDER of the two regex operations in that pass is deliberate. Every span of the line is
    matched FIRST and the token filter applied AFTER. The shorter spelling, one regex that requires
    whitespace INSIDE the span, skips a whitespace-free span and then re-anchors on that span's
    CLOSING backtick, so the prose BETWEEN two code spans is read as a span itself. Measured on
    ``docs/tools.md``, that variant invents dozens of such fragments, several of them exactly two
    tokens long (``) and``, ``, so``, ``need the``).

    Its honest reach, measured rather than asserted, because the tempting claim overstates it: on
    this tree the two spellings produce the IDENTICAL pairing list, and they still do when the mark
    test is widened to any single character, so nothing here is being rescued from a false red
    today. What actually keeps those fragments harmless is the two-token filter below, NOT the
    declared mark class. The order earns its place as defence in depth: a future document that puts
    a status name and a declared mark on either side of such a fragment WOULD be read as a pairing
    that nobody wrote. Because the two spellings agree today, no test can go red on this, which is
    why it is written here and recorded in ``docs/known-limits.md`` section 14 rather than pinned.

    Exactly two tokens, so the pass reads only spans that are NOTHING BUT a status and a mark. A
    longer span is prose in code formatting, and its first two words are not thereby written as a
    pair. Stated honestly, because the tempting justification does not survive: the separator forms
    that motivated this filter (```ok | unverified```, ```ok / unverified```,
    ```ok - confirmed```) are false reds only under an OPEN mark rule; measured under the declared
    mark class they read as nothing either way, and relaxing the filter changes no result on this
    tree at all. It is a conservative narrowing, not a fix for an observed false red.

    There is no separate whitespace test, and its absence is deliberate rather than an oversight:
    ``str.split()`` and ``re`` treat the identical 29 Unicode characters as whitespace (checked
    over the whole codepoint range, the difference is empty in both directions), so two tokens
    already prove whitespace stood between them and a second condition would be unreachable code.

    A line with an ODD number of backticks is skipped, because markdown pairing cannot be read from
    it and the surfaces carry multi-line code spans. Measured, 20 such lines across them, none
    carrying a pairing, so the filter changes no result today and makes the assumption loud instead
    of lucky.

    The STATUS half is looked up in ``marks``; the MARK half is tested against the declared
    ``_GLYPH_CHARACTERS``, and the asymmetry is the point (see that constant). ``marks`` arrives as
    a parameter rather than as a module lookup so both halves can be pinned on constructed input
    without monkeypatching a module, which is also this project's "no hidden state, inject it" rule.
    """
    pairings: list[tuple[str, str]] = []
    for block in re.split(r"\n[ \t]*\n", text):
        folded = re.sub(r"\s+", " ", block)
        for left, right in pairwise(list(_BACKTICKED_RE.finditer(folded))):
            gap = folded[left.end() : right.start()]
            if len(gap) > 3 or re.search(r"\w", gap):
                continue
            for status, mark in ((left.group(1), right.group(1)), (right.group(1), left.group(1))):
                if status in marks and mark in _GLYPH_CHARACTERS:
                    pairings.append((status, mark))
    for line in text.splitlines():
        if line.count("`") % 2:
            continue
        for span in _CODE_SPAN_RE.finditer(line):
            tokens = span.group(1).split()
            if len(tokens) != 2:
                continue
            for status, mark in ((tokens[0], tokens[1]), (tokens[1], tokens[0])):
                if status in marks and mark in _GLYPH_CHARACTERS:
                    pairings.append((status, mark))
    return pairings


def test_the_guide_status_buckets_tile_plane_status() -> None:
    """Every plane status is declared as documented in one marked region of the shipped guide, or
    as documented in neither. A NEW status belongs to no bucket, so it goes red here until somebody
    decides where it is written down, and that is the whole of QA-47: the legend ships inside the
    wheel as ``epics-pv://guide``, and a status missing from it used to be invisible.

    The union is compared as a SET, and the count stands BESIDE it rather than in place of it. Three
    equal cardinalities are blind to a RENAME, because a rename is cardinality-neutral: measured on
    this tree, renaming a status in ``PlaneStatus`` and ``_STATUS_MARK`` while leaving the buckets
    alone was green here for all twelve statuses, and for the six named outside the legend it was
    green in every other guard in this file too, so the buckets would go on naming a status that no
    longer exists. ``declared == len(union)`` is kept alongside for the DOUBLE-LISTING case, which
    set equality alone cannot see. The message below already printed both set differences, and that
    was the tell: a message that prints a set difference states a claim the assertion has to make.

    Red-proof: rename a status without moving its bucket entry, add a Literal value without
    bucketing it, drop one from a bucket, or list one twice.
    """
    buckets = (_IN_THE_GLYPH_TABLE, _IN_THE_STATUS_PROSE, _NOT_NAMED_IN_THE_GUIDE)
    statuses = set(get_args(PlaneStatus))
    assert statuses, "PlaneStatus yielded no values, the Literal anchor broke"
    declared = sum(len(bucket) for bucket in buckets)
    union: set[PlaneStatus] = set().union(*buckets)
    assert union == statuses and declared == len(union), (
        f"the guide-location buckets no longer tile PlaneStatus: declared={declared} "
        f"distinct={len(union)} statuses={len(statuses)}; "
        f"only-in-buckets={sorted(union - statuses)} "
        f"only-in-PlaneStatus={sorted(statuses - union)} "
        "(a declared count above the distinct one means a status is listed in two buckets)"
    )


def test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints() -> None:
    """The legend is held against ``cli_doctor._STATUS_MARK`` in both directions: no row for a
    status that no longer exists, no declared status without a row, and every Mark cell equal to
    the glyph the CLI actually renders.

    Nothing read this table before. It is package data served as ``epics-pv://guide``, so a wrong
    glyph reaches a reader who has no repository around it to check against, and the only thing
    between the two was a hand comparison somebody had to remember to make.

    Red-proof: delete a row, insert one, change a Mark cell, break a marker, unbacktick a cell.
    """
    rows = _glyph_rows()
    documented = {status for _, status in rows}
    assert documented == _IN_THE_GLYPH_TABLE, (
        "the shipped glyph legend drifted from its declaration: "
        f"only-in-guide={sorted(documented - _IN_THE_GLYPH_TABLE)} "
        f"only-in-declaration={sorted(_IN_THE_GLYPH_TABLE - documented)}"
    )
    wrong = [
        f"{status}: the guide shows {mark!r}, the CLI prints "
        f"{cli_doctor._STATUS_MARK.get(status)!r}"
        for mark, status in rows
        if mark != cli_doctor._STATUS_MARK.get(status)
    ]
    assert not wrong, (
        "the shipped legend advertises a different glyph than the CLI renders:\n  "
        + "\n  ".join(wrong)
    )


def test_every_glyph_status_pairing_in_the_docs_agrees_with_the_render_marks() -> None:
    """A status is paired with its glyph in more places than the legend, and a second copy of a
    guarded fact is an unguarded fact (``docs/known-limits.md`` says it in those words). Measured
    on the shipped guide alone, four such pairings stand OUTSIDE the legend, one of them split
    across a line break.

    Derived rather than declared, deliberately: there is no list to keep in step. Every backticked
    pairing found on any of these surfaces has to agree with ``_STATUS_MARK``, so a prose copy that
    drifts goes red without anybody having had to register it first.

    The floor is per surface rather than a total: a file the scan reads as empty is the way this
    goes vacuous, and it is a different failure from a file that has drifted.

    Red-proof: change a mark next to a status name anywhere on these three surfaces.
    """
    surfaces = {"the shipped guide": get_guide()} | {
        rel: (_ROOT / rel).read_text(encoding="utf-8") for rel in _DOC_SURFACES
    }
    mismatched: list[str] = []
    for where, text in surfaces.items():
        pairings = _glyph_status_pairings(text, cli_doctor._STATUS_MARK)
        assert pairings, (
            f"{where} yielded no glyph/status pairing at all, so the scan is not reading what it "
            "claims to: either the extraction broke or the file stopped documenting the statuses"
        )
        mismatched += [
            f"{where}: `{status}` is shown with {mark!r}, the CLI prints "
            f"{cli_doctor._STATUS_MARK[status]!r}"
            for status, mark in pairings
            if cli_doctor._STATUS_MARK[status] != mark
        ]
    assert not mismatched, (
        "a documented glyph disagrees with the one epics-doctor renders:\n  "
        + "\n  ".join(mismatched)
    )


# --- the extractor's own properties, pinned on constructed input --------------------------------
#
# These read no repository file at all. The guard above proves the extractor works on the tree as it
# stands TODAY; the pins below prove the PROPERTIES it was changed to have, so a later revert goes
# red here instead of going quietly green over prose nobody re-measures.


def test_the_glyph_class_covers_every_mark_the_cli_prints() -> None:
    """A floor on the FORM of the rule: the declared class may never be narrower than the marks it
    has to recognise.

    This is the price a declared class charges, and it is worth stating what it does and does not
    buy. It catches a NEW mark that nobody added to ``_GLYPH_CHARACTERS``, which is the one failure
    mode a derived rule cannot have. It deliberately does NOT catch the reverse of QA-50 (rewiring
    the mark test back to the mapping's values): the class would still cover every current mark, so
    this floor stays VACUOUSLY green under that edit. That is the pin below, not this.

    Red-proof: add a mark to ``_STATUS_MARK`` without adding its character here.
    """
    missing = sorted(set(cli_doctor._STATUS_MARK.values()) - _GLYPH_CHARACTERS)
    assert not missing, (
        f"epics-doctor prints marks the pairing scan cannot recognise: {missing}. Add them to "
        "_GLYPH_CHARACTERS; that set only ever grows, because a retired mark has to stay readable."
    )


def test_a_retired_mark_is_still_read_as_a_pairing() -> None:
    """The property QA-50 bought: a mark the CLI has STOPPED printing is still read as a mark, so
    the documentation copies that still show it are found at the moment they go stale.

    Constructed input rather than the tree, deliberately: on the real files this property is
    invisible, because no mark has been retired yet. Testing the mark half against
    ``marks.values()`` reads a retired glyph as ordinary prose, and every stale copy drops out of
    the scan in silence. That is the defect, and it is invisible to the floor above.

    Red-proof: put ``mark in marks.values()`` back into ``_glyph_status_pairings``.
    """
    retired = {**cli_doctor._STATUS_MARK, "no_ingest": "%"}
    assert "~" not in retired.values(), (
        "this pin no longer retires a mark, so it proves nothing: some other status now carries "
        "the glyph the fixture withdraws. Withdraw one that is unique to a single status."
    )
    found = _glyph_status_pairings("the appliance is `no_ingest` (`~`)", retired)
    assert ("no_ingest", "~") in found, (
        f"a documentation copy still showing a RETIRED mark was not read as a pairing: {found}. "
        "The mark half has to be tested against _GLYPH_CHARACTERS, never against marks.values()."
    )


def test_a_pairing_survives_an_indented_line_break() -> None:
    """The property step 3 bought: a pairing split across a line break is read even when the
    continuation line is INDENTED.

    Constructed input, because on the tree this reads as an ordinary pairing once it works, so
    nothing here would go red if the folding regressed. Folding line breaks alone leaves the
    indentation standing in the gap, which pushes it past the three-character limit while the
    sibling pairings in the same sentence are still read. That is what ``docs/deployment.md`` does.

    Red-proof: fold with ``" ".join(text.splitlines())`` instead of collapsing whitespace runs.
    """
    found = _glyph_status_pairings(
        "the honest states are `~`\n    (`no_ingest`, exit `0`) and more", cli_doctor._STATUS_MARK
    )
    assert ("no_ingest", "~") in found, (
        f"a pairing split across an INDENTED line break was not read: {found}. Whitespace RUNS "
        "have to collapse to one space before the gap is measured, not just the newline."
    )


def test_a_pairing_inside_one_code_span_is_read() -> None:
    """The property step 4 bought: a status and its mark written inside a SINGLE code span are read
    as a pairing.

    Constructed input, for the same reason as the two pins above: once the pass works, the two real
    occurrences read as ordinary pairings and nothing would go red if it were removed. The
    neighbour pass cannot reach this form at all, because its token regex forbids whitespace inside
    a span.

    Red-proof: delete the code-span pass. Narrowing its regex to the whitespace-inside spelling is
    NOT a red-proof for this pin and was measured rather than assumed: that variant still matches
    this input, and on the whole tree it yields a byte-identical pairing list. The order of
    operations it breaks is therefore recorded in ``docs/known-limits.md`` section 14, not pinned.
    """
    found = _glyph_status_pairings("reported `✓ ok` for a dead container", cli_doctor._STATUS_MARK)
    assert ("ok", "✓") in found, (
        f"a status and its mark inside ONE code span were not read as a pairing: {found}. The span "
        "pass has to match every span first and filter on the token count afterwards."
    )


def test_the_declared_status_locations_still_describe_the_guide() -> None:
    """The two location buckets are held against the two marked regions, in both directions: a
    status declared as named in the prose has to be named there, and a status declared as named
    NOWHERE must not turn up in either region.

    The negative half is why the guide carries two marked regions instead of one. Searching the
    WHOLE guide was probed and rejected: measured, ``disabled``, ``info`` and ``disconnected`` are
    ordinary words there in three foreign senses (an Olog level, an alarm config value, and
    ``diagnose_connection``'s own ``State``, which the guide already code-formats a sibling of), so
    a whole-file search reddens on an edit that documented something else entirely, and the obvious
    repair for that red is fully GREEN while quietly retiring the gap this bucket exists to record.
    Confining both halves to the marked regions costs a false negative (a status documented in a
    third place goes unnoticed) and buys the absence of a false positive with a green wrong repair.

    The match is an exact, CASE-SENSITIVE backticked token, stated rather than left to the reader:
    case-insensitively the guide already carries ``Disabled`` and ``Info`` in those foreign senses,
    and this test would be red on the day it was written.

    Red-proof: unbacktick a prose status name; write a declared-absent one into a region.
    """
    legend = _guide_region(_GLYPH_TABLE_RE, "status-glyphs")
    prose = _guide_region(_STATUS_PROSE_RE, "status-prose")
    missing = sorted(status for status in _IN_THE_STATUS_PROSE if f"`{status}`" not in prose)
    assert not missing, (
        f"declared as named in the guide's status prose, but not found there: {missing}. Either "
        "the mention moved out of the marked region, or the bucket needs to say so."
    )
    surfaced = sorted(
        status
        for status in _NOT_NAMED_IN_THE_GUIDE
        if f"`{status}`" in prose or f"`{status}`" in legend
    )
    assert not surfaced, (
        f"declared as named NOWHERE in the shipped guide, but now named in it: {surfaced}. If the "
        "status really is documented as an epics-doctor status, move it to the bucket for the "
        "region it stands in; do not move it because a DIFFERENT namespace borrowed the word."
    )
