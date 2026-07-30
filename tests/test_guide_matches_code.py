"""Drift guard: the guide's tool, env and doctor-status surfaces must match the code.

The guide is hand-written prose that names MCP tools, ``EPICS_MCP_*`` env vars and the plane
statuses ``epics-doctor`` prints. Without a guard, a renamed tool or a removed env var makes the
guide silently wrong, it would tell a fresh session to call a tool that no longer exists, exactly
when the session trusts it. This binds the guide's tool-inventory section to the ``@mcp.tool``
registrations and its ``EPICS_MCP_*`` mentions to ``EpicsConfig``: the same anti-drift pattern the
repo already uses for README resource URIs.

Three surfaces, and no surface is read one single way, which is worth saying because a summary
claiming otherwise stood here twice. What each guard reads is stated per guard, never per surface:

* the tool inventory, against the registrations. Anchored on markers;
* every ``EPICS_MCP_*`` mention, against ``EpicsConfig``. Whole file: measured, the guide mentions
  those variables on 35 lines and NONE of them falls inside the inventory markers, so an anchored
  scan would read no mention at all;
* the status legend and the statuses named in the prose above it, against ``PlaneStatus`` and
  ``cli_doctor._STATUS_MARK`` (QA-47). Its four guards read four different things: the tiling guard
  reads no guide text at all, the legend guard one marked region, the location guard both of them,
  and the pairing scan the whole text. That last one is what catches a SECOND, drifting copy of a
  marked region, which the anchored guards cannot see because ``re.search`` returns the first
  match. It also reaches beyond the guide: a glyph paired with a status name is checked on every
  TRACKED ``docs/*.md`` page too, because a second copy of a guarded number is an unguarded number.

What is still deliberately outside: the free-form Archiver MGMT verbs (``getAllPVs`` /
``getPVsForThisAppliance``) are manual REST recipes with no implementing tool, so they are
documented in prose and never checked here, and the legend's Meaning column is prose that has to
stay free to improve. What the status half does NOT check is written down and dated in
``docs/known-limits.md`` section 14.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import get_args

import pytest

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
#: The leading pipe is OPTIONAL, because GFM does not require it and a row without one renders
#: identically. Reading it as mandatory dropped such a row in silence, see ``_glyph_rows``.
_GLYPH_ROW_RE = re.compile(r"^\|?\s*`([^`]+)`\s*\|\s*`([a-z_]+)`\s*\|")
#: One cell of a GFM delimiter row: dashes, with optional alignment colons. Applied per cell and
#: alongside a width comparison against the header, because that pair IS the rule a renderer
#: applies. A character class over the whole line looks like the same test and is not: it accepts
#: five spellings under which the legend renders as no table at all (see ``_table_rows``).
_DELIMITER_CELL_RE = re.compile(r":?-+:?")
_BACKTICKED_RE = re.compile(r"`([^`\s]+)`")
#: A lower-case identifier, the shape every status name is written in. Applied to the TEXT of an
#: already-extracted code span (``_code_spans``), never to the raw markdown. Used for the REVERSE
#: direction of the location guard: not "is this declared status still named there" but "is every
#: name written there still a status".
#:
#: It carried its own backticks once, and that was a defect in both directions, because a regex
#: cannot tell an opening backtick from a closing one. Measured on
#: ``see `--json`ok`detail` here``: the backticked form read ``ok``, which is PROSE between two
#: spans, and missed ``detail``, which is a real span, so a genuine non-status token stayed
#: invisible (a false green) while an invented one reddened. Extracting the spans first cannot
#: have that failure: it consumes each span whole.
_LOWERCASE_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")
#: Code spans in the guide's status prose that are lower-case identifiers but are NOT plane
#: statuses. EMPTY today, and that emptiness is the promise: the shipped guide says the statuses
#: named in that paragraph are drift-guarded, and an allowlist that is needed on day one would be an
#: excuse rather than a guard. A legitimate new non-status word there is DECLARED here, which the
#: failure message says in as many words; the repair is to declare it, never to delete the check.
#:
#: That instruction only costs anything because every entry has to EARN its place: an entry that is
#: not written in the region is reported (``_prose_token_findings``). Without that floor "declare
#: it" and "delete the check" are the same move at different prices, which was measured rather than
#: argued: a dead entry was green, a real ``PlaneStatus`` declared away was green, and declaring all
#: five of the region's identifiers at once was green too, leaving the reverse direction inert while
#: the shipped marker went on promising it.
_NOT_A_STATUS_IN_THE_PROSE: frozenset[str] = frozenset()
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
#: Naming ONE forbidden spelling protects one spelling. Writing this line as
#: ``frozenset(cli_doctor._STATUS_MARK.values())`` buys the identical blindness, and it is the more
#: likely edit, because it is what anybody reaches for on seeing a magic literal. Measured, it was
#: green in every guard in this file and cost exactly what the named revert costs: with ``~``
#: retired, 22 pairings and 6 stale copies reported become 16 and 0. The pin above cannot see it,
#: and not by accident: it retires a mark at CALL time, while a derived constant is built at IMPORT
#: time from the mapping that still has it. ``mypy`` cannot separate them either, both being
#: ``frozenset[str]``. So the independence is pinned on the SOURCE, by
#: ``test_the_glyph_class_is_declared_and_not_borrowed_from_the_mapping``.
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
# The surfaces a pairing is EXPECTED on, and therefore the only ones carrying a per-surface floor.
# Every other tracked docs page is READ, but not required to contain anything: measured, the other
# five carry no pairing at all today, so a floor over all of them would be red on the first day.
#
# These three stay LITERAL names on purpose, and the tuple is read TWICE: once as a floor on the
# INPUT, before any page is read, and once as a floor on each page's OUTPUT. Before the page list
# came from git, a renamed docs/tools.md WAS a loud FileNotFoundError; a derived list simply stops
# containing it, and the page would then drop out of the scan together with its floor, because the
# output floor lives inside the loop over the pages that ARE there.
#
# ⚠️ Those two readings are why removing a name from here is never a small edit. When a page
# legitimately stops documenting statuses its output floor reddens, and deleting its name is the
# repair that message invites; measured, that also retires the input floor, so a LATER rename of
# the same page drops it out of the scan without a word. Retire a name only together with the page.
# And the entry for the shipped guide can only ever satisfy the input floor, never fail it: that
# key is put into the population unconditionally, one line above the check.
_PAIRING_EXPECTED = ("the shipped guide", "docs/tools.md", "docs/deployment.md")


def _tracked_doc_pages() -> tuple[str, ...]:
    """Every tracked ``docs/*.md`` page, spelled the way git spells it.

    Resolved against ``git ls-files``, NEVER against the disk, and that is a portability fact
    rather than a preference: Windows folds case in the filesystem and Linux does not, so a
    disk-based population is green on the author's machine and red on the runner, the worst
    direction for a guard to be wrong in. The sibling guard ``tests/test_doc_links.py`` carries the
    measurement behind that rule. The pathspec matches across ``/`` (checked: ``src/*.py`` returns
    the files under ``src/epics_mcp/``), so a page in a subdirectory is covered too; the
    non-recursive trap belongs to ``Path.glob``, not to a git pathspec.

    Its own population rather than a helper borrowed from a sibling module, which is the shape
    ``tests/test_product_name.py`` argues for on the same question: a shared helper carries the
    sibling's exclusions, and inheriting them silently narrows a scan meant to be exhaustive here.

    Error discipline taken from ``tests/test_guide.py``: ONLY a missing git binary may skip. A git
    call that ran and FAILED has to fail loudly, because a silent skip there switches the scan off
    in exactly the environment drift it should be reporting.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(_ROOT), "ls-files", "docs/*.md"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except FileNotFoundError as exc:  # pragma: no cover - environment without git
        pytest.skip(f"git binary unavailable, cannot enumerate the docs pages: {exc}")
    except subprocess.SubprocessError as exc:  # pragma: no cover - broken git environment
        pytest.fail(
            f"git ls-files failed ({exc}), so the glyph scan did NOT read docs/; fix the git "
            "environment (ownership/repo state) instead of letting the scan shrink in silence"
        )
    return tuple(sorted(line.strip() for line in listing.splitlines() if line.strip()))


def _guide_region(pattern: re.Pattern[str], name: str) -> str:
    match = pattern.search(get_guide())
    assert match, f"the guide's {name} markers were not found, the drift anchor broke"
    return match.group(1)


def _code_spans(text: str) -> list[str]:
    """The TEXT of every code span, in reading order, duplicates kept.

    The one place in this file that decides what a code span is. Both readers of markdown here go
    through it: the second pass of ``_glyph_status_pairings`` and the location guard's reverse
    direction. They used to disagree, the pairing pass consuming whole spans while the reverse
    direction matched a backtick-delimited pattern, and a pattern cannot tell an opening backtick
    from a closing one. What that cost is measured in ``_LOWERCASE_TOKEN_RE``.

    A line with an ODD number of backticks is skipped, because markdown pairing cannot be read from
    it and these surfaces carry code spans that run across a line break. Measured, twenty such lines
    across the eight scanned surfaces, none carrying a pairing, and NONE inside either marked region
    of the guide, so the filter changes no result today and makes the assumption loud instead of
    lucky.
    """
    spans: list[str] = []
    for line in text.splitlines():
        if line.count("`") % 2:
            continue
        spans += _CODE_SPAN_RE.findall(line)
    return spans


def _prose_token_findings(
    spans: set[str], statuses: set[str], allowlist: frozenset[str]
) -> tuple[list[str], list[str]]:
    """``(dead, unknown)`` for the status paragraph: entries that earn nothing, tokens nobody knows.

    Both halves of the reverse direction, in one place and taking their world as arguments, so the
    properties below can be pinned on constructed input without monkeypatching a module. That is
    the same shape, and the same reason, as ``marks`` being a parameter of
    ``_glyph_status_pairings``, and it is this project's "no hidden state, inject it" rule.

    ``dead`` is a floor on the DECLARATION and is reported first, before the comparison it guards:
    an allowlist entry that is not written in the region weakens the check without anyone noticing.
    ``unknown`` is the comparison itself.
    """
    dead = sorted(token for token in allowlist if token not in spans)
    named = {span for span in spans if _LOWERCASE_TOKEN_RE.fullmatch(span)}
    return dead, sorted(named - allowlist - statuses)


def _tiling_findings(
    buckets: Sequence[frozenset[str]], statuses: set[str]
) -> tuple[list[str], list[str], int]:
    """``(only_in_buckets, only_in_statuses, double_listed)`` for the guide-location partition.

    The set differences and the double-listing count, together and taking their world as
    arguments, so the property below can be pinned on constructed input. Three equal cardinalities
    are blind to a RENAME, which is cardinality-neutral, and set equality alone is blind to a
    DOUBLE listing, so both halves have to be returned and both have to be asserted.
    """
    union = set().union(*buckets)
    return sorted(union - statuses), sorted(statuses - union), sum(map(len, buckets)) - len(union)


def _repeated_statuses(rows: Sequence[tuple[str, str]]) -> list[str]:
    """Statuses a legend lists more than once, before the rows are reduced to a set.

    Reducing to a set is what the caller does and what an exact duplicate row survives: two equal
    rows collapse to one member and agree with the CLI individually. Counting first is the only
    place that sees it.
    """
    return sorted(status for status, seen in Counter(s for _, s in rows).items() if seen > 1)


def _row_cells(line: str) -> list[str]:
    """The cells of one GFM table row: the pipe-separated parts, outer pipes dropped.

    The outer pipes are OPTIONAL in GFM and a row without them renders identically, so they are
    stripped rather than required. An escaped ``\\|`` inside a cell would be miscounted here;
    measured, no row of the shipped legend carries one, and a row that grew one would fail the
    width comparison loudly rather than quietly.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _table_rows(region: str, where: str) -> list[tuple[str, str]]:
    """The ``(mark, status)`` cells of a legend region, read the way markdown defines a table.

    POSITIONAL rather than by literal prefix: the first non-blank line is the header, the second is
    the delimiter, and every line after that has to parse. The old filter kept only lines beginning
    with a pipe and skipped the two it recognised by their opening text, so it decided what a table
    row is from its spelling rather than from its place. A legal GFM row WITHOUT a leading pipe
    therefore dropped out in silence, and the caller then reported the status as undocumented: the
    exact documentation-gap message this floor exists to prevent, on a table that was complete.

    Measured, four legal and render-identical edits are read instead of reddening: a row with no
    leading pipe, a delimiter respelled as ``| --- | --- | --- |``, alignment colons in the
    delimiter, and a renamed header column. Everything that is red today for a real reason stays
    red.

    ⚠️ The delimiter is held against the rule the RENDERER applies, not against a character class,
    and that distinction was a measured hole rather than a refinement. GFM makes a table only when
    the delimiter row has the SAME number of cells as the header and every cell is dashes with
    optional alignment colons. A class of "pipes, dashes, colons and spaces" accepts five spellings
    that fail that rule, and under each of them the shipped legend renders as NO TABLE AT ALL,
    a wall of literal pipes for every human reader, while this parser went on reading six rows and
    all four guards stayed green: one cell too few, a bare ``|``, ``| | |``, ``|:|:|:|``, and a
    plain ``---``. The same holds for a BLANK LINE between two rows, which ends the table in GFM.
    Since the guide is package data served to a reader who has no repository around it, a legend
    that stopped being a legend is the failure this whole file exists to prevent.

    What that check is NOT: a markdown parser. It holds the delimiter rule and the blank-line rule,
    which are what the measured breakages violate, and no more. The rest of the GFM table grammar
    stays outside, dated in ``docs/known-limits.md`` section 14. A renderer was probed and rejected
    for two reasons: it would make a guard's verdict depend on a third-party version, and the only
    one already in the tree arrives transitively rather than declared.

    Floors, all BEFORE any caller compares sets, the ordering
    ``test_guide_tool_inventory_matches_registrations`` already uses. A floor placed after a
    comparison is dead code: the comparison fails first and reports a documentation gap where in
    truth the anchor broke. They are: the markers were found (in ``_guide_region``), the region
    carries a header AND a delimiter AND at least one row, no blank line splits the table, the
    delimiter is one GFM accepts for that header, and every remaining line parses at the declared
    width. The size floor is a single assertion on purpose, because it already covers both the
    emptied table and the table with no rows left; a separate ``assert rows`` after it would be
    unreachable.
    """
    lines = region.splitlines()
    body = [line for line in lines if line.strip()]
    assert len(body) >= 3, (
        f"the {where} has {len(body)} non-blank lines, so it cannot carry a header, a delimiter "
        "and at least one row: the table anchor broke."
    )
    filled = [i for i, line in enumerate(lines) if line.strip()]
    split = [i for i in range(filled[0], filled[-1]) if not lines[i].strip()]
    assert not split, (
        f"a blank line stands inside the {where}, at offset {split[0] - filled[0] + 1} of the "
        "table. GFM ends a table there, so everything below it renders as literal pipes while "
        "this parser goes on reading rows. Remove the blank line or move it outside the markers."
    )
    header, delimiter, *rest = body
    width = len(_row_cells(header))
    cells = _row_cells(delimiter)
    assert len(cells) == width and all(_DELIMITER_CELL_RE.fullmatch(c) for c in cells), (
        f"the second line of the {where} is not a delimiter row GFM accepts: {delimiter!r} has "
        f"{len(cells)} cell(s) against the header's {width} (header {header!r}). Without a "
        "matching delimiter the whole block renders as text, not as a table."
    )
    rows: list[tuple[str, str]] = []
    for line in rest:
        match = _GLYPH_ROW_RE.match(line)
        assert match, (
            f"a row of the {where} does not parse as `mark` | `status` | ...: {line!r}. Without "
            "this floor it would drop out of the comparison in silence and the guard would check "
            "less than it claims to."
        )
        assert len(_row_cells(line)) == width, (
            f"a row of the {where} has {len(_row_cells(line))} cells against the header's {width}: "
            f"{line!r}. GFM pads or truncates such a row, so the rendered table stops matching the "
            "one this guard compares."
        )
        rows.append((match.group(1), match.group(2)))
    return rows


def _glyph_rows() -> list[tuple[str, str]]:
    """The shipped legend's ``(mark, status)`` cells."""
    return _table_rows(_guide_region(_GLYPH_TABLE_RE, "status-glyphs"), "shipped glyph legend")


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

    Both the span extraction and the odd-backtick skip live in ``_code_spans``, which is also what
    the location guard's reverse direction reads, so the two cannot drift apart on the question of
    what a code span is.

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
    for span in _code_spans(text):
        tokens = span.split()
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

    The union is compared as a SET, and the double-listing count stands BESIDE it rather than in
    place of it. Three equal cardinalities are blind to a RENAME, because a rename is
    cardinality-neutral: measured on this tree, renaming a status in ``PlaneStatus`` and
    ``_STATUS_MARK`` while leaving the buckets alone was green here for all twelve statuses, so the
    buckets would go on naming a status that no longer exists. Set equality alone is in turn blind
    to a DOUBLE listing, which is why both halves are asserted. The message below already printed
    both set differences, and that was the tell: a message that prints a set difference states a
    claim the assertion has to make.

    How much of that this guard carries alone, re-measured after the location guard gained its
    reverse direction, because the sentence that stood here was written before it and had gone
    stale: of the six statuses named outside the legend, the THREE named in the prose region are
    caught by ``test_the_declared_status_locations_still_describe_the_guide`` as well. The three
    named in NEITHER region, ``disabled``, ``disconnected`` and ``info``, are caught by nothing but
    the set comparison here.

    Red-proof: ``test_a_renamed_status_is_reported_though_the_three_counts_still_agree``, which
    pins the property on constructed input, plus the tree itself under a rename, an unbucketed
    Literal value, a dropped bucket entry, or one listed twice.
    """
    buckets = (_IN_THE_GLYPH_TABLE, _IN_THE_STATUS_PROSE, _NOT_NAMED_IN_THE_GUIDE)
    statuses = set(get_args(PlaneStatus))
    assert statuses, "PlaneStatus yielded no values, the Literal anchor broke"
    only_in_buckets, only_in_plane_status, double_listed = _tiling_findings(buckets, statuses)
    assert not (only_in_buckets or only_in_plane_status or double_listed), (
        f"the guide-location buckets no longer tile PlaneStatus: "
        f"only-in-buckets={only_in_buckets} only-in-PlaneStatus={only_in_plane_status} "
        f"listed-in-two-buckets={double_listed}"
    )


def test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints() -> None:
    """The legend is held against ``cli_doctor._STATUS_MARK`` in both directions: no row for a
    status that no longer exists, no declared status without a row, and every Mark cell equal to
    the glyph the CLI actually renders.

    Nothing read this table before. It is package data served as ``epics-pv://guide``, so a wrong
    glyph reaches a reader who has no repository around it to check against, and the only thing
    between the two was a hand comparison somebody had to remember to make.

    Comparing the rows as a SET is what this guard is for, and it is also what an exact duplicate
    row hides: two identical rows collapse to one member and agree with the CLI individually, so
    the legend could list a status twice and stay green in both halves. Measured, that is the only
    duplicate shape that gets through: a copy carrying a DIFFERENT mark is already caught by the
    mark comparison below. The rows are therefore counted before they are reduced, the same shape
    as the count kept beside the tiling guard's set comparison and for the same reason. What it
    costs a reader is a legend that says the same thing twice, which is why this was carried as
    cosmetic rather than as a defect.

    Red-proof: delete a row, insert one, change a Mark cell, break a marker, unbacktick a cell,
    duplicate a row EXACTLY.

    """
    rows = _glyph_rows()
    repeated = _repeated_statuses(rows)
    assert not repeated, (
        f"the shipped glyph legend carries more than one row for {repeated}. A reader sees two "
        "rows that can disagree, and reducing the rows to a set below would hide it."
    )
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
    guarded number is an unguarded number, which is what ``docs/known-limits.md`` says in those
    words. On the shipped guide alone, such pairings stand OUTSIDE the legend, one of them split
    across a line break and one written inside a single code span. No count is given: it moved
    twice while this scan was being repaired, and a figure beside a derived set can only drift.

    Derived rather than declared on BOTH axes, deliberately: there is no list of pairings to keep
    in step, and there is no list of pages either. The population is every tracked ``docs/*.md``
    page plus the shipped guide, so a new documentation page is covered the day it is added rather
    than the day somebody remembers to register it. It used to be a hard-wired pair, which left
    five of the seven tracked pages unguarded and made ``CLAUDE.md``'s "in the guide or in docs/"
    an overstatement.

    The floor is per surface rather than a total, because a file the scan reads as empty is a
    different failure from a file that has drifted, and a total hides the first inside the second.
    It applies to the three surfaces a pairing is EXPECTED on, not to all of them: measured, the
    other five tracked pages carry no pairing today, so a floor over every page would be red on the
    first day.

    Reach, stated rather than implied: the floor covers 3 of the 8 surfaces that are read. On the
    guide it is satisfied by the six legend rows that
    ``test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints`` compares anyway, so it adds
    nothing there beyond what that guard already holds. The five unfloored pages are read without
    one, which catches a GLOBAL extraction break and not a page-specific one.

    Red-proof: change a mark next to a status name on any tracked page; rename ``docs/tools.md``.
    """
    surfaces = {"the shipped guide": get_guide()} | {
        rel: (_ROOT / rel).read_text(encoding="utf-8") for rel in _tracked_doc_pages()
    }
    absent = sorted(set(_PAIRING_EXPECTED) - set(surfaces))
    assert not absent, (
        f"a surface this scan expects pairings on is not in the population: {absent}. If the page "
        "was renamed, move the name here as well; do not let it drop out of the scan, because its "
        "floor would leave with it and the loss would be silent."
    )
    mismatched: list[str] = []
    for where, text in surfaces.items():
        pairings = _glyph_status_pairings(text, cli_doctor._STATUS_MARK)
        assert pairings or where not in _PAIRING_EXPECTED, (
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


def test_the_glyph_class_is_declared_and_not_borrowed_from_the_mapping() -> None:
    """The mark class must not READ ``_STATUS_MARK``, and that is checked on the source text.

    Naming a forbidden repair in a comment protects the spelling that was named. The comment on
    ``_GLYPH_CHARACTERS`` forbids ``token in marks.values()`` and the pin below catches exactly
    that; writing the CONSTANT as ``frozenset(cli_doctor._STATUS_MARK.values())`` buys the identical
    blindness and was green in every guard in this file. It is also the likelier edit, being what
    anybody reaches for on seeing a magic literal.

    Why no runtime check can do it: a derived constant is built at IMPORT time, from a mapping that
    still holds every current mark, so at the moment any test runs the two are indistinguishable.
    Measured, they are equal today, character for character. The difference only appears once a mark
    is RETIRED, which is the one moment this scan exists for and the one moment nobody is looking.
    ``mypy`` cannot separate them either: both are ``frozenset[str]``.

    So the property is read off the syntax, which is what the file already does to find the tool
    registrations in ``server.py`` and what ``scripts/guard_audit.py`` does to the suite. Any name
    or attribute in the assigned expression fails it, except the container being called, so a
    literal stays free to be spelled as a string, a set or a list.

    Red-proof: assign ``frozenset(cli_doctor._STATUS_MARK.values())``.
    """
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assigned = [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_GLYPH_CHARACTERS" for t in node.targets)
    ]
    # Non-empty floor: an AST scan that finds nothing passes every check that follows it.
    assert len(assigned) == 1, (
        f"expected one assignment to _GLYPH_CHARACTERS in this file, found {len(assigned)}. The "
        "source anchor broke, so this pin was about to check nothing."
    )
    borrowed = sorted(
        {
            ast.unparse(node)
            for node in ast.walk(assigned[0])
            if isinstance(node, ast.Attribute)
            or (isinstance(node, ast.Name) and node.id not in {"frozenset", "set"})
        }
    )
    assert not borrowed, (
        f"_GLYPH_CHARACTERS reads {borrowed} instead of declaring its characters. A class derived "
        "from the mark mapping stops recognising a mark at the moment it is RETIRED, which is the "
        "one drift this scan exists to catch, and no runtime test can see the difference before "
        "that moment. Write the characters out."
    )


def _legend_fixture(delimiter: str = "|---|---|---|", *, split: bool = False) -> str:
    """A minimal legend region shaped like the shipped one, for the two table pins below."""
    rows = ["| `✓` | `ok` | the service named itself |", "| `~` | `no_ingest` | not ingesting |"]
    if split:
        rows.insert(1, "")
    return "\n".join(["", "| Mark | Status | Meaning |", delimiter, *rows, ""])


def test_a_delimiter_row_a_renderer_would_reject_is_rejected_here() -> None:
    """The property this step bought: the delimiter is held against the rule GFM applies, which is
    a width comparison against the header plus a per-cell shape, not a character class.

    Constructed input, and it has to be: on the tree the delimiter is correct, so nothing would go
    red if this regressed. Each rejected spelling below was measured with a real CommonMark
    renderer to produce NO table at all, zero rows, while the parser here went on reading six and
    every guard stayed green. The guide is package data read by somebody with no repository around
    it, so a legend that renders as a wall of pipes is the failure this file exists to prevent.

    The accepted half is not decoration. Two of those three spellings are edits QA-50 deliberately
    made legal, and a delimiter rule tight enough to reject them would have undone that.

    Red-proof: match the delimiter against ``r"^\\|?[\\s|:-]+$"`` again, which accepts all five.
    """
    for delimiter in ("|---|---|", "|", "| | |", "|:|:|:|", "---"):
        with pytest.raises(AssertionError, match="delimiter row"):
            _table_rows(_legend_fixture(delimiter), "fixture legend")
    for delimiter in ("|---|---|---|", "| --- | --- | --- |", "|:---:|:---:|---:|"):
        rows = _table_rows(_legend_fixture(delimiter), "fixture legend")
        assert [status for _, status in rows] == ["ok", "no_ingest"], (
            f"a delimiter GFM accepts was rejected or misread: {delimiter!r} gave {rows}. The two "
            "spacing spellings are edits this parser was changed to allow."
        )


def test_a_blank_line_inside_the_legend_is_rejected() -> None:
    """The property this step bought: a blank line between two rows ends the table.

    Constructed input, same reason. Measured with a renderer on the real legend: one blank line
    before the last row renders six ``<tr>`` instead of seven and drops the last row into a
    paragraph of literal pipes, while this parser still returned all six rows and every guard
    stayed green. The filter that hid it is the ``line.strip()`` this function uses to find the
    header, which is right for the marker padding around the table and wrong inside it.

    The accepted half pins that padding: blank lines OUTSIDE the table, which every marked region
    carries, must stay legal.

    Red-proof: delete the split check.
    """
    with pytest.raises(AssertionError, match="blank line"):
        _table_rows(_legend_fixture(split=True), "fixture legend")
    padded = "\n\n" + _legend_fixture().strip("\n") + "\n\n"
    assert len(_table_rows(padded, "fixture legend")) == 2, (
        "blank lines around the table were rejected; they are the marker padding every marked "
        "region carries and have to stay legal."
    )


def test_a_renamed_status_is_reported_though_the_three_counts_still_agree() -> None:
    """The property QA-50's first step bought: the buckets are compared as SETS.

    A rename is cardinality-neutral, so three equal sizes cannot see it. Measured on the tree,
    renaming any of the twelve statuses while leaving the buckets alone was green under the old
    count-only assertion, all twelve times, and for the three named in neither region it was green
    in every other guard in this file too.

    The second assertion is the control that turns the first into a finding rather than a
    demonstration: on the SAME input the retired three-count form is satisfied. Without it, "the
    set comparison reddens" would only say the set comparison works.

    Red-proof: compare ``declared == len(union) == len(statuses)`` again.
    """
    buckets = (frozenset({"ok"}), frozenset({"ca_error"}), frozenset({"info"}))
    only_in_buckets, only_in_statuses, double_listed = _tiling_findings(
        buckets, {"ok", "ca_error", "info_renamed"}
    )
    assert (only_in_buckets, only_in_statuses, double_listed) == (["info"], ["info_renamed"], 0), (
        f"a renamed status was not reported: only-in-buckets={only_in_buckets} "
        f"only-in-statuses={only_in_statuses} double-listed={double_listed}"
    )
    assert sum(map(len, buckets)) == len(set().union(*buckets)) == 3, (
        "control: the retired three-count form is satisfied by this input, which is what made a "
        "rename invisible and what the set comparison exists to repair"
    )
    _, _, doubled = _tiling_findings((frozenset({"ok"}), frozenset({"ok"})), {"ok"})
    assert doubled == 1, "a status listed in two buckets was not reported; set equality hides it"


def test_a_legend_row_listed_twice_is_reported() -> None:
    """The property QA-50's last step bought: the rows are counted before they are reduced.

    Two identical rows collapse to one set member and agree with the CLI individually, so a legend
    could list a status twice and stay green in both halves of its guard. The two assertions below
    are the control: on the same rows the set comparison and the mark comparison, which are the
    whole of that guard, are both satisfied.

    Red-proof: compare ``{status for _, status in rows}`` without counting first.
    """
    rows = [("✓", "ok"), ("✓", "ok"), ("~", "no_ingest")]
    assert _repeated_statuses(rows) == ["ok"], (
        f"a status listed twice in the legend was not reported: {_repeated_statuses(rows)}"
    )
    marks = {"ok": "✓", "no_ingest": "~"}
    assert {status for _, status in rows} == set(marks), (
        "control: the set comparison is satisfied by the duplicate, which is why it hides it"
    )
    assert not [s for m, s in rows if m != marks[s]], (
        "control: the mark comparison is satisfied as well, both rows being identical"
    )


def test_a_row_without_its_leading_pipe_is_read_not_dropped() -> None:
    """The property QA-50's fifth step bought: a legal GFM row without its leading pipe is a row.

    The old filter kept only lines beginning with a pipe, so such a row left the comparison in
    silence and the caller then reported its status as undocumented: the documentation-gap message
    the floor exists to prevent, on a table that was complete.

    The second assertion is the control, and it is what makes this a defect rather than a
    preference: the retired filter keeps ONE of the two rows on the same input, and says nothing.

    Red-proof: require the leading pipe in ``_GLYPH_ROW_RE`` again.
    """
    region = _legend_fixture().replace("| `~` | `no_ingest`", "`~` | `no_ingest`")
    rows = _table_rows(region, "fixture legend")
    assert [status for _, status in rows] == ["ok", "no_ingest"], (
        f"a row without its leading pipe was not read: {rows}. GFM does not require the outer "
        "pipes and such a row renders identically."
    )
    kept = [
        line
        for line in region.splitlines()
        if line.startswith("|") and not line.startswith(("| Mark ", "|--"))
    ]
    assert len(kept) == 1, (
        f"control: the retired prefix filter was expected to keep exactly one of the two rows, "
        f"kept {len(kept)}. That silent drop is what this pin records."
    )


def test_a_prose_token_that_is_not_a_status_is_reported() -> None:
    """The property QA-50's seventh step bought: the status paragraph's reverse direction exists.

    Before it, a status removed from ``PlaneStatus``, from ``_STATUS_MARK`` and from its bucket,
    while the sentence naming it stayed in the guide, was GREEN in all four guards, and the tiling
    guard FORCES that bucket removal in the same edit, so the path is the likely one rather than an
    exotic one. Measured on the tree afterwards, the direction catches three of the six statuses
    named outside the legend; the other three are named in neither region and are still held by the
    set comparison alone.

    The second assertion is the control: a token that IS a status must pass, or the direction would
    just be a ban on writing anything.

    Red-proof: return an empty ``unknown`` from ``_prose_token_findings``.
    """
    _, unknown = _prose_token_findings({"ok", "detail"}, {"ok"}, frozenset())
    assert unknown == ["detail"], (
        f"a lower-case token the paragraph names as if the CLI printed it was not reported: "
        f"{unknown}. That is the promise the shipped marker above the paragraph makes."
    )
    _, unknown = _prose_token_findings({"ok", "ca_error"}, {"ok", "ca_error"}, frozenset())
    assert not unknown, f"control: real statuses were reported as unknown: {unknown}"


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


def test_a_span_behind_another_span_is_read_and_the_prose_between_them_is_not() -> None:
    """One property with two faces: what is a code span is decided by CONSUMING spans, never by a
    pattern that carries its own backticks.

    A pattern cannot tell an opening backtick from a closing one, so it can re-anchor on a closing
    backtick and read the prose that follows as if it were a span. Both faces are the same defect
    and both are pinned here, because on the tree today neither can fire: measured, the guide's two
    marked regions contain no adjacent spans at all, so nothing would go red if this regressed.

    * a real span that FOLLOWS another span was invisible, which is a false GREEN: a genuine
      non-status token could stand in the guarded paragraph and the reverse direction would not
      report it;
    * the prose BETWEEN two spans was read as one, which is a false RED whose obvious repair is to
      move the word into a "named" bucket, and that repair is green while retiring the measured gap
      the bucket exists to record.

    Red-proof: give ``_LOWERCASE_TOKEN_RE`` its backticks back and match it against the region
    instead of against extracted spans. Measured on this input, that variant inverts BOTH
    assertions below at once.
    """
    found = _code_spans("see `--json`ok`detail` here")
    assert "detail" in found, (
        f"a code span standing directly behind another one was not read: {found}. A backticked "
        "pattern re-anchors on the closing backtick and loses it; extract the spans instead."
    )
    assert "ok" not in found, (
        f"prose standing BETWEEN two code spans was read as a span: {found}. Only text a pair of "
        "backticks encloses is a span."
    )


def test_a_declared_non_status_nobody_wrote_is_reported() -> None:
    """The property this step bought: an allowlist entry has to EARN its place by being written in
    the region it exempts.

    Constructed input, and here that is not a convenience but the only possibility: the allowlist
    is empty today, so the floor is vacuously green on the tree and would stay green if it were
    deleted. Its whole value is what it does to a NON-empty allowlist, which does not exist yet.

    Why it exists: the constant's own comment says the repair for a new non-status word is to
    declare it, "never to delete the check". Measured before this floor, declaring was exactly as
    effective as deleting and easier to justify. Declaring all five identifiers the region carries
    left every guard green with the reverse direction doing nothing at all.

    The second half is the control, and it is the half that keeps the floor honest: an entry that
    IS written must be accepted and must not resurface as unknown. Without it the cheapest way to
    pass this pin would be to reject every allowlist entry.

    Red-proof: drop the ``dead`` computation from ``_prose_token_findings``, or return it empty.
    """
    dead, unknown = _prose_token_findings({"ok"}, {"ok"}, frozenset({"detail"}))
    assert dead == ["detail"], (
        f"a declared non-status that the region does not write was not reported: dead={dead}. "
        "An allowlist entry nobody wrote silently widens the exemption."
    )
    assert not unknown, f"the dead entry leaked into the unknown list as well: {unknown}"
    dead, unknown = _prose_token_findings({"ok", "detail"}, {"ok"}, frozenset({"detail"}))
    assert not dead and not unknown, (
        f"a declared non-status that IS written was rejected: dead={dead} unknown={unknown}. The "
        "floor has to accept an earned entry, or declaring becomes impossible instead of costly."
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

    All three halves compare against the TEXT of an extracted code span (``_code_spans``), never
    against the raw markdown. A substring test over the region reads a name that is not a span at
    all: measured, writing ``empty `*_URL`info`0`)`` into the prose made the negative half report
    ``info`` as newly named, where ``info`` is plain prose between two spans, and the repair that
    message steers to was GREEN in exactly that state, retiring the gap the bucket records.

    The comparison is exact and CASE-SENSITIVE. That is fixed here so the behaviour is defined, NOT
    because case-insensitivity would break this guard: measured, the guide's ``Disabled`` and
    ``Info`` both stand OUTSIDE the two marked regions, so a case-insensitive match confined to the
    regions is green as well. The case rule was what the rejected WHOLE-FILE variant needed, and it
    was carried over here with a justification that no longer applied.

    A THIRD direction makes the shipped guide's own promise true. That paragraph is introduced by a
    marker reading "the plane statuses named outside the legend below are drift-guarded against
    PlaneStatus", and the two halves above did not deliver it. Measured for each of the three
    statuses whose only home is that paragraph: removing one from ``PlaneStatus``, from
    ``_STATUS_MARK`` and from its bucket, while the sentence naming it stays in the guide, was GREEN
    in all four guards. That is the likely path rather than an exotic one, because the tiling guard
    FORCES the bucket to be cleared in the same edit. So every lower-case identifier written as a
    code span in the prose region has to be a plane status. (A status that also has a legend row is
    caught by the legend guard as well; these three were caught by nothing.)

    Only the prose region, not the legend. The legend region carries ``degraded_planes`` and
    ``name``, so extending this half there would need two allowlist entries on the first day, and an
    allowlist that is needed immediately is an excuse rather than a promise. What this direction
    does NOT catch, and what therefore stays in ``docs/known-limits.md``: a status name outside both
    regions, one written without backticks, and one written with a capital.

    Red-proof: unbacktick a prose status name; write a declared-absent one into a region; remove a
    status from ``PlaneStatus`` and its bucket while the guide goes on naming it.
    """
    statuses = set(get_args(PlaneStatus))
    legend = set(_code_spans(_guide_region(_GLYPH_TABLE_RE, "status-glyphs")))
    prose = set(_code_spans(_guide_region(_STATUS_PROSE_RE, "status-prose")))
    missing = sorted(status for status in _IN_THE_STATUS_PROSE if status not in prose)
    assert not missing, (
        f"declared as named in the guide's status prose, but not found there: {missing}. Either "
        "the mention moved out of the marked region, or the bucket needs to say so."
    )
    surfaced = sorted(
        status for status in _NOT_NAMED_IN_THE_GUIDE if status in prose or status in legend
    )
    assert not surfaced, (
        f"declared as named NOWHERE in the shipped guide, but now named in it: {surfaced}. If the "
        "status really is documented as an epics-doctor status, move it to the bucket for the "
        "region it stands in; do not move it because a DIFFERENT namespace borrowed the word."
    )
    dead, unknown = _prose_token_findings(prose, statuses, _NOT_A_STATUS_IN_THE_PROSE)
    assert not dead, (
        f"_NOT_A_STATUS_IN_THE_PROSE declares {dead}, but the guide's status prose does not write "
        "them. Remove the entry: an allowlist that keeps a word nobody wrote is how this check "
        "gets emptied one line at a time, and emptying it is what its own comment forbids."
    )
    assert not unknown, (
        f"the guide's status prose names {unknown} as if epics-doctor printed them, but they are "
        "not PlaneStatus values. If the status was removed, the sentence has to go with it; if the "
        "token was never a status, declare it in _NOT_A_STATUS_IN_THE_PROSE."
    )
