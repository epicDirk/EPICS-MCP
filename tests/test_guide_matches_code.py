"""Drift guard: the guide's tool, env, doctor-status and doctor-plane surfaces must match the code.

The guide is hand-written prose that names MCP tools, ``EPICS_MCP_*`` env vars, and both the plane
statuses ``epics-doctor`` prints and the planes it prints them for. Without a guard, a renamed tool
or a removed env var makes the guide silently wrong, it would tell a fresh session to call a tool
that no longer exists, exactly when the session trusts it. This binds the guide's tool-inventory
section to the ``@mcp.tool`` registrations and its ``EPICS_MCP_*`` mentions to ``EpicsConfig``: the
same anti-drift pattern the repo already uses for README resource URIs.

Four surfaces, and no surface is read one single way, which is worth saying because a summary
claiming otherwise stood here twice. What each guard reads is stated per guard, never per surface:

* the tool inventory, against the registrations. Anchored on markers;
* every ``EPICS_MCP_*`` mention, against ``EpicsConfig``. Whole file: measured, the guide mentions
  those variables on 35 lines and NONE of them falls inside the inventory markers, so an anchored
  scan would read no mention at all;
* the status legend and the statuses named in the prose above it, against ``PlaneStatus`` and
  ``cli_doctor._STATUS_MARK`` (QA-47, widened by QA-49). Its six guards read six different things:
  the tiling guard and the totality guard read no guide text at all (the second one holds the
  legend's DECLARATION against ``PlaneStatus``, so a status may not be left undocumented), the
  legend guard one marked region, the prose-presence guard the other one against the code's own
  failing-status set, the location guard both regions, and the pairing scan the whole text. That
  last one is what catches a SECOND, drifting copy of a
  marked region, which the anchored guards cannot see because ``re.search`` returns the first
  match. It also reaches beyond the guide: a glyph paired with a status name is checked on every
  TRACKED ``docs/*.md`` page too, because a second copy of a guarded number is an unguarded number.
  "Paired" means one of the two written forms ``docs/known-limits.md`` section 14 names, not any
  proximity, and tracked markdown OUTSIDE ``docs/`` is not read at all; both limits are dated there;
* the plane NAMES the report prints, against a real ``run_doctor`` run and against the literals in
  ``services/doctor.py`` (QA-73). Two guards, two comparisons, and the split is the point: the guide
  is held against what the report PRODUCES, while the source literals are held against that same
  runtime set rather than against the guide, because a literal nobody calls is a dead branch and not
  a documentation gap. What a status LINE says was already guarded; what it is CALLED was not.

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
from epics_mcp.services import doctor
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


# --- the doctor PLANE surface (QA-73) -----------------------------------------------------------

_PLANE_INVENTORY_RE = re.compile(
    r"<!-- BEGIN:plane-inventory.*?-->(.*?)<!-- END:plane-inventory -->", re.DOTALL
)
#: A plane name as the report prints it. Deliberately NOT ``_TOOL_TOKEN_RE``: that one requires an
#: underscore, to exclude camelCase REST verbs and single words. Measured, exactly ONE of the seven
#: plane names has an underscore, so reusing it would have dropped the other six out of the
#: comparison in silence and left this guard green against a nearly empty set.
#:
#: The price of dropping the underscore is that EVERY bare lower-case code span between the markers
#: reads as a plane name. The markers therefore fence the name list ALONE, with the explaining prose
#: outside them, which is the difference from the tool inventory: that one may keep prose inside
#: because its own pattern demands an underscore.
#:
#: ⚠️ Both halves of that were learned the hard way, in one sitting. The first version of this
#: comment claimed "the region carries names and nothing else, prose there stays unbackticked",
#: which was already false on the tree that introduced it (``epics-doctor`` sat in the region and
#: survived only because a hyphen ends the match). The repair to that sentence then put ``plane``,
#: ``planes`` and ``config_error`` inside the markers while explaining the very hazard, and the
#: guard went red with three phantom planes. Fencing the list is what makes the rule keepable
#: instead of merely stated.
_PLANE_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]*)`")


def _guide_plane_tokens() -> set[str]:
    match = _PLANE_INVENTORY_RE.search(get_guide())
    assert match, "guide plane-inventory markers not found, the drift anchor broke"
    return set(_PLANE_TOKEN_RE.findall(match.group(1)))


def _plane_taking_helpers(tree: ast.Module) -> set[str]:
    """Every function in the module whose FIRST parameter is named ``plane``.

    Derived rather than declared, and that distinction was measured rather than chosen. The first
    version of this scan carried a hand-kept pair, ``_run_probe`` and ``_disabled``, on the ground
    that the helpers below them take the name as a variable. That is true of their DEFINITIONS and
    false of their CALL SITES: six further calls pass a plane-name literal positionally
    (``_identify``, ``_unverified``, ``_identity_fetch_failure``, ``_backend_down``,
    ``_classify_phoebus_name``). An adversarial probe turned that gap into a working exploit,
    renaming one such literal so the report printed a plane that appeared in NEITHER set while both
    guards stayed green. Reading the parameter name instead cannot miss a helper nobody added to a
    list.
    """
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.args.args
        and node.args.args[0].arg == "plane"
    }


def _source_plane_names() -> set[str]:
    """Every plane name written as a LITERAL in ``services/doctor.py``.

    The second source, and it reads a different axis than the runtime one: the runtime set is what
    ONE config produced, this is what the module spells out. Neither subsumes the other, which is
    the whole reason both exist (see the guard below).

    Two shapes are read, and together they cover every literal on the tree: the first positional
    argument of a call to a plane-taking helper (see :func:`_plane_taking_helpers`), and a
    ``plane=`` keyword. The keyword shape carries five distinct names across eight sites, not the
    single ``plane="live"`` an earlier version of this comment claimed.
    """
    tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))
    helpers = _plane_taking_helpers(tree)
    assert helpers, "no plane-taking helper found in services/doctor.py, the AST anchor broke"
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.id if isinstance(node.func, ast.Name) else ""
        if called in helpers and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
        for keyword in node.keywords:
            if (
                keyword.arg == "plane"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                names.add(keyword.value.value)
    return names


async def _reported_plane_names() -> set[str]:
    """The planes ``run_doctor`` actually reports, read off a real run against an empty config.

    Hermetic because the URLs are BLANKED here, not because the machine happens to have none set.
    ⚠️ That distinction was measured, and the first version of this docstring got it wrong: it said
    "hermetic by construction" while constructing ``EpicsConfig()`` with no arguments at all.
    ``EpicsConfig`` is a ``BaseSettings`` with ``env_prefix="EPICS_MCP_"``, so a bare instantiation
    reads the PROCESS ENVIRONMENT, and ``conftest.py`` deliberately leaves those variables alone.
    Measured with two of them pointed at a discard port, this guard took 11.16 s instead of 0.03 s
    and issued real connects while staying green: the silent failure mode, on precisely the kind of
    machine where a facility is configured. The fields are derived from the model rather than
    listed, so a new URL variable is blanked the day it is added.

    ``rest_get_json`` is stubbed on top, because the retrieval plane and the identity probes call it
    DIRECTLY rather than through the client classes; that is the exact hole the autouse fixture in
    ``tests/test_doctor.py`` documents having measured at 12.1 seconds of real hostname resolution.
    """
    blanked = {name: "" for name in EpicsConfig.model_fields if name.endswith("_url")}
    assert blanked, "EpicsConfig exposes no *_url field, the blanking anchor broke"
    config = EpicsConfig(**blanked)  # type: ignore[arg-type]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("epics_mcp.services.doctor.get_config", lambda: config)
        patch.setattr("epics_mcp.services.doctor.rest_get_json", lambda *_a, **_k: {})
        report = await doctor.run_doctor()
    return {plane.plane for plane in report.planes}


async def test_the_guide_names_every_plane_the_report_prints() -> None:
    """The contract: every plane ``epics-doctor`` prints a line for is named in the shipped guide.

    Why this is not covered by the status guards below. They hold what a line SAYS (its glyph, its
    status word) against the code; nothing held what a line is CALLED. Measured on 2026-08-02, the
    guide's ``## The planes`` bullets are grouped by SERVICE and count six, while the report is
    grouped by CHECK and prints seven: the archiver is probed twice, and ``archiver_retrieval``
    appeared under no bullet at all. Both orderings are individually correct, which is why prose
    alone never noticed; what was missing is the mapping between them.

    Red-proof: delete any name from the plane-inventory region (this reddens with that name), or add
    one nobody prints (this reddens the other way). Both directions are needed: presence alone would
    pass a region that named a plane the report has since dropped.
    """
    guide = _guide_plane_tokens()
    reported = await _reported_plane_names()
    # Non-empty floors (F23), both sides, the same shape as the tool-inventory guard above: set
    # equality passes vacuously as empty == empty if either scanner silently returns nothing.
    assert guide, "the guide's plane inventory is empty, the marker or token anchor broke"
    assert reported, "run_doctor reported no planes at all, the report anchor broke"
    # The findings are NAMED rather than computed inline, and that is what makes the central
    # comparison visible to _ASSERTED_NAMES below. ⚠️ Measured on the first version of this guard:
    # with `guide` and `reported` as the only declared names, both were already satisfied by the two
    # floors above, so deleting this comparison outright left the floor GREEN. A row that its own
    # floor assertions satisfy declares nothing.
    only_in_guide = sorted(guide - reported)
    only_in_report = sorted(reported - guide)
    assert not (only_in_guide or only_in_report), (
        f"guide plane inventory <-> report drift: only-in-guide={only_in_guide} "
        f"only-in-report={only_in_report}. Every plane the report prints a line for has "
        "to be named in the plane-inventory region, in the spelling the line uses."
    )


async def test_every_plane_literal_in_the_source_reaches_the_report() -> None:
    """The second comparison, and it is deliberately NOT against the guide.

    An AST pin held against the GUIDE would report a dead literal (a ``_disabled("x")`` nobody
    calls) as a documentation gap, which it is not: that is a false alarm on the wrong surface. Held
    against the RUNTIME set instead, the same drift says the true thing, in either direction: a
    plane name is spelled in the module but never reaches a report, or a plane reaches the report
    without a literal this scan can see.

    It earns its place because the two sources are blind to different things. The runtime set is
    what ONE config produced, so a plane that only appears under some other configuration would be
    missing from it; the literal set is spelled in the source, so it has that plane. Conversely a
    computed or f-string name is invisible HERE and present in the runtime set. Neither blindness is
    hypothetical in general, though both are on today's tree: all seven planes are unconditional and
    all seven are literals.

    Red-proof: add ``_disabled("ghost")`` to a branch nothing calls (reddens as only-in-source), or
    make a plane's name an f-string (reddens as only-in-report).
    """
    literals = _source_plane_names()
    reported = await _reported_plane_names()
    assert literals, "no plane literals found in services/doctor.py, the AST anchor broke"
    assert reported, "run_doctor reported no planes at all, the report anchor broke"
    # Named for the same reason as in the guard above: a finding the floor cannot see is a row that
    # declares nothing.
    only_in_source = sorted(literals - reported)
    unlit_in_report = sorted(reported - literals)
    assert not (only_in_source or unlit_in_report), (
        f"plane literals <-> report drift: only-in-source={only_in_source} "
        f"only-in-report={unlit_in_report}. This is NOT a documentation finding: a "
        "name spelled in the module that never reaches a report is dead, and a reported plane with "
        "no literal is one this scan cannot see."
    )


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
    {
        "ok",
        "disabled",
        "info",
        "unverified",
        "no_ingest",
        "identity_probe_failed",
        "throttled",
        "config_error",
        "ca_error",
        "api_error",
        "unreachable",
        "disconnected",
        "backend_down",
    }
)
# EMPTY since QA-49 (2026-08-02), and the emptiness is a CONSEQUENCE rather than a decision: the
# legend now names every status, so no status has the prose region as its only home. Both remaining
# buckets are therefore empty, which makes two of this file's assertions structurally vacuous
# (``missing`` and ``surfaced`` below), which is recorded here rather than left to be
# rediscovered. What did NOT move into the vacuum: the prose region's
# presence guard, which is now DERIVED from ``doctor._FAILING_STATUSES`` rather than declared here
# (``test_the_status_prose_still_names_every_failing_status``), and is twice as wide for it.
_IN_THE_STATUS_PROSE: frozenset[PlaneStatus] = frozenset()
# The gap this bucket recorded was CLOSED by QA-49; it is kept, empty, as the declared place where a
# future, deliberate gap would have to be written down. That is not a free pass: putting a status
# here reddens ``test_the_shipped_legend_names_every_plane_status``, which is what turns "what the
# report prints, the shipped handbook explains" from prose into a gate.
_NOT_NAMED_IN_THE_GUIDE: frozenset[PlaneStatus] = frozenset()

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
#:
#: The ``{0,3}`` only ACCEPTS the indentation GFM allows; it does not reject the indentation GFM
#: forbids, and saying otherwise would be a claim this pattern cannot keep. Without a leading pipe
#: the ``\s*`` behind the optional bar swallows any amount of indentation, measured on four and
#: eight spaces and on a tab, and the pipe-less spelling is the one QA-50 deliberately legalised.
#: The rejecting half is therefore a line check inside ``_table_rows``, where it also covers the
#: header and the delimiter, which never reach this pattern at all.
_GLYPH_ROW_RE = re.compile(r"^ {0,3}\|?\s*`([^`]+)`\s*\|\s*`([a-z_]+)`\s*\|")
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
#: ⚠️ NO LONGER EMPTY, and the first entry is recorded with its reason rather than added quietly.
#: ``reads_denied`` is a REPORT FIELD, not a plane status, and it belongs in that paragraph because
#: it is the THIRD driver of exit 3: a refusal can land on a sub-probe whose plane stays healthy,
#: so the run is inconclusive with no status to show for it (BG-DTHR). Naming the exit codes
#: without naming it would leave a reader unable to explain an exit 3 whose plane lines are all
#: ``✓``. It earns its place under the rule above: it IS written in the region.
_NOT_A_STATUS_IN_THE_PROSE: frozenset[str] = frozenset({"reads_denied"})
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
#: written TIGHTLY against a mark it is CONTRASTED with rather than paired to, as in ```ok`/`?```,
#: which reads as the pairing nobody wrote. The example this comment used to give, "the ``ok``
#: versus ``?`` distinction", does NOT exhibit it and was measured rather than assumed: the gap
#: there is eight characters, well past the limit of three, so that sentence and its siblings
#: (``vs``, ``, not``) read as nothing at all.
_GLYPH_CHARACTERS = frozenset("✓✗?!~·i»")
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
    """The TEXT of every code span, in reading order, duplicates kept, each STRIPPED.

    The strip is here, in the reader both markdown passes share, and that placement was measured
    rather than argued. It used to sit at one comparison point inside ``_prose_token_findings``,
    justified by "the pairing pass splits its tokens anyway, so it never had the defect". That
    sentence is false for the FIRST of the pairing pass's two passes: its token pattern forbids
    whitespace inside a span, so a padded span is not read at all, and a documentation page showing
    a WRONG glyph beside a padded status name went unreported. Measured, ```` ` ok ` ```` beside a
    wrong mark yields no pairing while ```` `ok` ```` yields one, and a reader sees the same word
    in both.

    Two more comparison points had the same blind spot for the same reason, and the strip closes
    all three at once: the location guard's forward half read a padded prose mention as MISSING (a
    false red whose invited repair is green and retires the bucket), and its negative half read a
    padded declared-absent status as still absent (a false green that silently makes the recorded
    gap untrue).

    Why a reader is the right place to normalise: CommonMark removes one leading and one trailing
    space from a span when both are present, so a padded span and its unpadded twin render as the
    same word, and every consumer here compares against what a READER sees. Stripping more than
    CommonMark does (tabs, two spaces a side) errs towards REPORTING, which is the loud direction.
    Measured on the tree the strip changes nothing at all: no span on any scanned surface carries
    padding today, all guards stay green, and the pairing pass is byte-identical because it calls
    ``split()``. ``_prose_token_findings`` keeps its own strip, because it takes its world as
    arguments and its pin hands it padded input directly.

    The one place in this file that decides what a code span is. Both readers of markdown here go
    through it: the second pass of ``_glyph_status_pairings`` and the location guard's reverse
    direction. They used to disagree, the pairing pass consuming whole spans while the reverse
    direction matched a backtick-delimited pattern, and a pattern cannot tell an opening backtick
    from a closing one. What that cost is measured in ``_LOWERCASE_TOKEN_RE``.

    A line with an ODD number of backticks is skipped, because markdown pairing cannot be read from
    it and these surfaces carry code spans that run across a line break. Measured 2026-08-20, forty
    such lines across the nine scanned surfaces, none carrying a pairing, and NONE inside either
    marked region of the guide, so the filter changes no result today and makes the assumption loud
    instead of lucky.

    ⚠️ This said "twenty ... across the eight" until GQ-117 and had doubled since 2026-07-30: the
    lines grew with the pages, and ``docs/quick-start.md`` made the eight a nine on 2026-08-18. The
    "none carrying a pairing" half was re-measured with the same reader this function feeds
    (``_glyph_status_pairings`` over the odd lines, zero hits against a live ``_STATUS_MARK``) and
    still holds; only the counts had moved.
    """
    spans: list[str] = []
    for line in text.splitlines():
        if line.count("`") % 2:
            continue
        spans += (span.strip() for span in _CODE_SPAN_RE.findall(line))
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

    Each span is STRIPPED before either half reads it, and that is a correctness fix rather than
    tidiness: the comparison is against what a READER sees, and padding inside a code span is
    invisible to one. CommonMark removes one leading and one trailing space when both are present,
    so ```` ` detail ` ```` renders exactly like ```` `detail` ````; a one-sided ```` `detail ` ````
    renders with a space nobody notices in running text. Measured before this strip, all four
    padded spellings were GREEN while the unpadded one was red, which made two typed spaces the
    cheapest way to retire this direction, cheaper than the allowlist entry the message asks for.

    ⚠️ This strip used to be the ONLY one, and the sentence that justified keeping it here rather
    than in ``_code_spans`` was measured false: it said the pairing pass "splits its tokens anyway,
    so it never had the defect". The pairing pass has TWO passes and only the second splits; the
    first forbids whitespace inside a span, so a padded status name beside a WRONG glyph was not
    read at all. Two further comparison points in the location guard had the same blind spot. The
    reader normalises now, and the strip here is kept because this function takes its world as
    arguments and its pin hands it padded input directly.
    """
    written = {span.strip() for span in spans}
    dead = sorted(token for token in allowlist if token not in written)
    named = {span for span in written if _LOWERCASE_TOKEN_RE.fullmatch(span)}
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


def _indent_columns(line: str) -> int:
    """The leading indentation of a line in COLUMNS, the unit GFM measures it in.

    A tab does not count as one character, it advances to the next multiple of four, so ``" \\t"``
    is one space plus a jump to column four: four columns of indentation, an indented code block,
    and a legend that renders as no table at all. Counting CHARACTERS instead reads that as one
    space and lets it through, which is what the check below did until this was measured.

    Measured with a renderer over 108 cases (18 prefixes by three placements by both pipe
    spellings): the character rule accepts 28 spellings under which the shipped shape produces zero
    table rows, this one accepts 16, and neither produces a single false red. What closes the
    remaining 14 is the delimiter-only whitespace rule in ``_table_rows``, not a wider margin here.
    """
    column = 0
    for character in line:
        if character == " ":
            column += 1
        elif character == "\t":
            column += 4 - column % 4
        else:
            break
    return column


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
    a wall of literal pipes for every human reader, while this parser went on reading every row and
    all the guards stayed green: one cell too few, a bare ``|``, ``| | |``, ``|:|:|:|``, and a
    plain ``---``. The same holds for a BLANK LINE between two rows, which ends the table in GFM.
    Since the guide is package data served to a reader who has no repository around it, a legend
    that stopped being a legend is the failure this whole file exists to prevent.

    ⚠️ INDENTATION is a fifth way to break the render, and it was the one this check missed while
    claiming to hold the renderer's rule. Four COLUMNS of indentation make GFM read a line as an
    indented code block, so the table ends there or never starts. Measured with a renderer on the
    shipped shape: indenting the HEADER that far, or the DELIMITER, gives zero table rows for a
    reader, and the parser here read every row and stayed green, because both ``body`` and
    ``_row_cells`` strip. The mirror case was a false RED: a table indented by one to three columns
    renders perfectly and was rejected, and only in the spelling that KEPT its leading pipe, since
    the pattern anchored that pipe at column zero. Both directions are held now, and the rejecting
    half is a line check here rather than a prefix in ``_GLYPH_ROW_RE``, because a pattern whose
    optional bar is followed by ``\\s*`` cannot reject indentation in the pipe-less spelling at all.

    ⚠️ COLUMNS, not characters, and that distinction was a second measured hole rather than a
    refinement. The first version of this check read a leading tab and a run of leading spaces, and
    a tab is neither: it advances to the next multiple of four, so ``" \\t"``, ``"  \\t"`` and
    ``"   \\t"`` are four columns while looking like one, two or three characters. Measured with a
    renderer over 108 cases, the character rule let 28 spellings through that render as no table.
    The count lives in ``_indent_columns``.

    What that check is NOT: a markdown parser. It holds the delimiter rule, the blank-line rule, the
    indentation limit, the delimiter's leading-whitespace rule and the row width, which are what the
    measured breakages violate, and no more. The rest of the GFM table grammar stays outside, dated
    in ``docs/known-limits.md`` section 14. A renderer was probed and rejected for two reasons: it
    would make a guard's verdict depend on a third-party version, and the only one already in the
    tree arrives transitively rather than declared.

    Floors, all BEFORE any caller compares sets, the ordering
    ``test_guide_tool_inventory_matches_registrations`` already uses. A floor placed after a
    comparison is dead code: the comparison fails first and reports a documentation gap where in
    truth the anchor broke. They are: the markers were found (in ``_guide_region``), the region
    carries a header AND a delimiter AND at least one row, no blank line splits the table, no line
    is indented past the margin GFM allows, the delimiter does not open on a whitespace character
    GFM refuses to read as indentation, the delimiter is one GFM accepts for that header, and
    every remaining line parses at the declared width. The size floor is a single assertion on
    purpose, because it already covers both the emptied table and the table with no rows left; a
    separate ``assert rows`` after it would be unreachable.
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
    # BEFORE _row_cells, which strips unconditionally and would erase the evidence. Every line of
    # the block is checked, header and delimiter included: those two never reach _GLYPH_ROW_RE, and
    # measured with a renderer, indenting EITHER of them past the margin makes the legend render as
    # no table at all while this parser went on reading every row and stayed green.
    over_indented = [line for line in body if _indent_columns(line) >= 4]
    assert not over_indented, (
        f"a line of the {where} is indented past the three columns GFM allows: "
        f"{over_indented[0]!r} starts at column "
        f"{_indent_columns(over_indented[0])}, counting a tab to the next multiple of four. GFM "
        "reads that as an indented code block rather than as part of a table, so the whole legend "
        "renders as literal text for a reader while this parser goes on reading rows. Up to three "
        "columns are legal and are read."
    )
    header, delimiter, *rest = body
    # Only the DELIMITER, and the scope was measured rather than chosen. A leading whitespace
    # character that is neither a space nor a tab is not indentation to GFM, so it does not end the
    # table: before the HEADER or before a ROW the renderer swallows it and produces the full table,
    # and rejecting it there costs 28 false reds on markdown that renders. Before the DELIMITER it
    # is fatal, because that line has to match the delimiter grammar from its first character, and
    # `_row_cells` strips it away before this parser can notice. Measured over the same 108 cases:
    # scoped here the false greens fall from 16 to 2 and no false red appears; applied to every line
    # the 28 arrive at once. The residue is named in `docs/known-limits.md` section 14.
    assert not (delimiter[:1].isspace() and delimiter[:1] not in " \t"), (
        f"the delimiter row of the {where} begins with the whitespace character "
        f"{delimiter[:1]!r}, which GFM does not read as indentation: the line stops being a "
        "delimiter and the whole block renders as literal pipes, while this parser strips the "
        "character away and goes on reading rows. Use a space, or no whitespace at all."
    )
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
    that nobody wrote. Because the two spellings agree on this TREE, no tree-driven guard can go
    red on it, which is why the property is held on CONSTRUCTED input instead, by
    ``test_the_span_pass_matches_every_span_before_it_filters_on_tokens``. That pin was written
    after the sentence here claimed for one round that section 14 recorded the order rather than
    holding it; the recording alone had left the shorter spelling green in all 1702 tests.

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
    stale: it was written when six statuses stood outside the legend, three of them named nowhere
    at all. QA-49 closed that, so the legend is total and both other buckets are empty. What this
    comparison carries alone is therefore no longer a set of statuses but a SHAPE: a bucket entry
    that names something ``PlaneStatus`` does not have, or names it twice. The question "may a
    status be left undocumented" moved to
    ``test_the_shipped_legend_names_every_plane_status``, which is a different question and is
    asked separately on purpose.

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


def test_the_shipped_legend_names_every_plane_status() -> None:
    """The legend is TOTAL: every ``PlaneStatus`` is documented in it, with no exception (QA-49).

    This is the one guard that turns "what the report prints, the shipped handbook explains" from a
    sentence somebody wrote into a gate. Without it that promise is prose, and prose is the category
    that rots: the buckets above only require a status to be sorted SOMEWHERE, and "named nowhere in
    the guide" is one of the three places it may legally land.

    Why it is not redundant with the tiling guard, measured rather than argued. Take the one edit
    that reopens the gap this ticket closed, in BOTH its halves: delete a row from the shipped
    legend AND move that status into ``_NOT_NAMED_IN_THE_GUIDE`` in the same change. Then the tiling
    guard is green (the three buckets still tile ``PlaneStatus`` exactly), the legend guard is green
    (the table and its declaration still agree), the pairing scan is green (no glyph disagrees with
    the CLI) and the location guard is green (the status really is named nowhere). All four say
    nothing. Only this one reddens.

    ⚠️ The red-proof is deliberately spelled in two halves, because the cheaper ONE-file variant
    reads as evidence that this guard is redundant and it is not. Moving only the declaration, with
    the guide untouched, reddens THREE guards (this one, the legend guard on ``only-in-guide``, and
    the location guard on ``surfaced``), which invites the reading "somebody else already covers
    it". They cover the half-done edit, not the complete one.

    Red-proof: delete the ``disabled`` row from the ``status-glyphs`` region AND add ``"disabled"``
    to ``_NOT_NAMED_IN_THE_GUIDE`` in the same edit; only this guard goes red.
    """
    unlisted = sorted(set(get_args(PlaneStatus)) - _IN_THE_GLYPH_TABLE)
    assert not unlisted, (
        f"epics-doctor can print {unlisted}, and the shipped legend does not name them. Every "
        "status the report shows has to be explained by the handbook that travels with it, without "
        "exception: give each one a row in the status-glyphs region and declare it in "
        "_IN_THE_GLYPH_TABLE. Do NOT repair this by moving the status into another bucket; those "
        "record where a status IS documented, not permission to leave it undocumented."
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
    page plus the shipped guide, so a new documentation page is covered the day it is TRACKED
    rather than the day somebody remembers to register it. Tracked, not written: ``git ls-files``
    reads the index, so a page sitting in the working tree with a wrong glyph in it passes, and
    reddens on the ``git add``. Measured in a throwaway repository, both halves. CI runs on the
    committed tree, so what that costs is the local run before staging. It used to be a hard-wired
    pair, which left five of the seven tracked pages unguarded and made ``CLAUDE.md``'s "in the
    guide or in docs/" an overstatement.

    The floor is per surface rather than a total, because a file the scan reads as empty is a
    different failure from a file that has drifted, and a total hides the first inside the second.
    It applies to the three surfaces a pairing is EXPECTED on, not to all of them: measured, the
    other six tracked pages carry no pairing today, so a floor over every page would be red on the
    first day.

    Reach, stated rather than implied: the floor covers 3 of the 9 surfaces that are read. On the
    guide it is satisfied by the legend rows that
    ``test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints`` compares anyway, so it adds
    nothing there beyond what that guard already holds. The six unfloored pages are read without
    one, which catches a GLOBAL extraction break and not a page-specific one.

    ⚠️ These three figures read five, 3 of 8 and five until GQ-117, and all three were right when
    written on 2026-07-30. ``docs/quick-start.md`` was added on 2026-08-18 (``bf4fbf8``) and moved
    every one of them by one, which nothing noticed: the count is derived from ``git ls-files`` at
    run time, so the guard kept passing while the prose describing it went stale. Re-derive rather
    than trust: ``len(_tracked_doc_pages())`` pages, plus the guide, against
    ``len(_PAIRING_EXPECTED)``. The legend-row figure was dropped instead of corrected, for the
    reason the same repair records at ``_STATUS_MARK``: that number moves with every new status.

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

    Constructed input, same reason. Measured with a renderer on the legend as it stood then (six
    rows): one blank line before the last row rendered six ``<tr>`` instead of seven and dropped
    the last row into a paragraph of literal pipes, while this parser still returned every row and
    every guard stayed green. The legend has twelve rows since QA-49; the property is the same and
    the measurement is dated rather than re-run, because it is about the RENDERER, not the count.
    The filter that hid it is the ``line.strip()`` this function uses to find the
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


def _indented_legend(*, head: str = "", delim: str = "", body: str = "") -> str:
    """A legend whose header, delimiter and rows can be indented independently."""
    rows = ["| `✓` | `ok` | the service named itself |", "| `~` | `no_ingest` | not ingesting |"]
    header = head + "| Mark | Status | Meaning |"
    return "\n".join(["", header, delim + "|---|---|---|", *[body + row for row in rows], ""])


def test_indentation_is_held_against_the_margin_gfm_allows() -> None:
    """The property this step bought: the indentation limit is part of the renderer's rule, and it
    is checked on EVERY line of the block rather than on the rows alone.

    Both directions were measured with a real CommonMark renderer against the shipped shape, and
    both were wrong before this step. Indenting the HEADER by four spaces, or the DELIMITER, makes
    the renderer produce NO table at all, zero rows for somebody reading the shipped guide, while
    this parser read every row and every guard stayed green: a false GREEN of exactly the class the
    delimiter rule was written to close. Indenting the whole table by one to three spaces renders
    perfectly and was REJECTED, and only in the spelling that kept its leading pipe: a false RED on
    legal markdown, in the pipe-less spelling QA-50 went out of its way to legalise.

    Why the check is here and not a prefix in ``_GLYPH_ROW_RE``, which would look like the same
    test: the header and the delimiter never reach that pattern, and in the pipe-less spelling the
    ``\\s*`` behind its optional bar swallows any indentation, measured on four and eight spaces
    and on a tab. A prefix can widen what is ACCEPTED; only a line check can reject.

    Rejected on the way, and named so nobody reaches for it: DEDENTING the region before parsing.
    It is the cheapest repair and it turns this guard around. A legend indented by four spaces
    throughout renders as no table, and a dedent would make this parser read it happily, so a
    correct red becomes a false green.

    ⚠️ The MIXED spellings below are the ones the first version of this check missed while claiming
    to hold the renderer's rule, and they were invisible to the 56-case cross-check that came with
    it, whose seven indentations were runs of spaces and a lone tab. A tab after one, two or three
    spaces reaches column four, so ``" \\t"`` renders as no table and read as one character of
    indentation. That is why the count lives in ``_indent_columns`` and is asserted in the message.

    ⚠️ The last block is the green half of the delimiter-only whitespace rule, and it is what keeps
    that rule from being widened. A leading non-breaking space before the HEADER, or before a
    pipe-less ROW, renders as a full table; rejecting it there would cost 28 false reds, measured.
    Only the DELIMITER placement is fatal, and only that one is rejected.

    Red-proof: delete the ``over_indented`` check, and the first ``pytest.raises`` below fails;
    count characters instead of columns, and the mixed spellings pass; widen the delimiter rule to
    every line, and the last block fails.
    """
    for pad in ("", " ", "  ", "   "):
        rows = _table_rows(_indented_legend(head=pad, delim=pad, body=pad), "fixture legend")
        assert [status for _, status in rows] == ["ok", "no_ingest"], (
            f"an indentation of {len(pad)} space(s) was rejected or misread: {rows}. GFM allows up "
            "to three and renders the table unchanged."
        )
    no_pipe = _indented_legend(head="  ", delim="  ", body="  ").replace("  | `", "  `")
    read = [status for _, status in _table_rows(no_pipe, "fixture legend")]
    assert read == ["ok", "no_ingest"], (
        f"an indented row WITHOUT its leading pipe was rejected, read {read}. That spelling is "
        "legal GFM and was deliberately legalised here; the limit must not take it back."
    )
    for kind in ("head", "delim", "body"):
        for pad in ("    ", "        ", "\t", "\t ", " \t", "  \t", "   \t"):
            with pytest.raises(AssertionError, match="indented past the three columns GFM allows"):
                _table_rows(_indented_legend(**{kind: pad}), "fixture legend")
    # The count is asserted, not just the rejection: a character rule would report the wrong one,
    # and " \t" is the spelling on which the two rules disagree.
    with pytest.raises(AssertionError, match=r"starts at column 4"):
        _table_rows(_indented_legend(head=" \t"), "fixture legend")

    # The delimiter-only whitespace rule, both halves. The renderer was consulted for each: before
    # the delimiter a non-breaking space gives a reader zero table rows, before the header or a
    # pipe-less row it gives the full table.
    with pytest.raises(AssertionError, match="does not read as indentation"):
        _table_rows(_indented_legend(delim="\xa0"), "fixture legend")
    pipe_less_rows = _indented_legend(body="\xa0").replace("\xa0| `", "\xa0`")
    for spelling in (_indented_legend(head="\xa0"), pipe_less_rows):
        kept = [status for _, status in _table_rows(spelling, "fixture legend")]
        assert kept == ["ok", "no_ingest"], (
            f"a leading non-breaking space outside the delimiter was rejected, read {kept}. GFM "
            "renders that table in full, and widening the delimiter rule to every line costs 28 "
            "false reds, measured."
        )


def test_a_row_whose_cell_count_drifts_from_the_header_is_rejected() -> None:
    """The third of the three rules this parser holds, and the one nothing pinned.

    The delimiter rule and the blank-line rule each got a pin when they were built; the row width
    was written in the same step and left unheld, which measured as a silent revert: deleting the
    width assertion left every test in the file green, because ``_legend_fixture`` and the shipped
    legend both carry rows of the declared width, so the assertion never fires on the tree.

    Both directions are checked, because GFM pads a short row and truncates a long one, so either
    drift makes the rendered table stop matching the one this guard compares.

    Red-proof: delete the width assertion inside the row loop of ``_table_rows``.
    """
    for row, cells in (("| `~` | `no_ingest` |", 2), ("| `~` | `no_ingest` | a | b |", 4)):
        region = _legend_fixture().replace("| `~` | `no_ingest` | not ingesting |", row)
        with pytest.raises(AssertionError, match=f"has {cells} cells against the header's 3"):
            _table_rows(region, "fixture legend")
    kept = [status for _, status in _table_rows(_legend_fixture(), "fixture legend")]
    assert kept == ["ok", "no_ingest"], (
        f"control: rows OF the declared width were rejected, read {kept}. Without this half the "
        "cheapest way to pass the two assertions above would be to reject every row."
    )


def test_a_line_of_odd_backtick_parity_is_skipped_by_the_span_pass() -> None:
    """The property behind the odd-parity filter, which was documented and never held.

    Markdown pairing cannot be read from a line whose backticks do not pair, and these documents
    carry code spans that run across a line break. Measured on the tree the filter changes no
    result today (22 pairings with it and 22 without), so nothing here goes red if it is deleted:
    exactly the case a pin on constructed input exists for.

    The second assertion is the control that turns this into a property rather than a ban: the same
    line at EVEN parity is read, so the filter is about pairing and not about the content.

    Red-proof: delete the parity check in ``_code_spans``.
    """
    odd = "reported `✓ ok` for a dead container`"
    assert odd.count("`") % 2, "this fixture lost its odd parity, so it pins nothing"
    assert _glyph_status_pairings(odd, cli_doctor._STATUS_MARK) == [], (
        "a line whose backticks do not pair was read as a pairing. Markdown cannot say which of "
        "them opens a span, so the line is skipped rather than guessed at."
    )
    even = odd[:-1]
    assert ("ok", "✓") in _glyph_status_pairings(even, cli_doctor._STATUS_MARK), (
        "control: the same line at even parity was NOT read, so the assertion above passes for the "
        "wrong reason and would survive the span pass being deleted outright."
    )


def test_the_span_pass_matches_every_span_before_it_filters_on_tokens() -> None:
    """The property step 4 of the previous round bought, recorded as unpinnable and pinned here.

    The pass matches EVERY span of the text first and applies the two-token filter afterwards. The
    shorter spelling, one regex requiring whitespace inside the span, skips a whitespace-free span
    and re-anchors on its closing backtick, so the prose BETWEEN two spans is read as a span. Both
    spellings agree on this tree, which is why no tree-driven guard can see the difference, and why
    the property was merely written down until this test existed.

    The first assertion is the control and it is what makes the second one a finding: on this
    input the reverted spelling DOES invent the pairing, so the emptiness below is a statement
    about the rule rather than about an extractor that has quietly stopped reading. The third
    assertion is the non-empty floor this file requires beside any emptiness check.

    Red-proof: replace ``_code_spans(text)`` in the span pass with
    ``re.findall(r"`([^`]*\\s[^`]*)`", text)``.
    """
    text = "`x`ok ✓`y`"
    invented = [span.split() for span in re.findall(r"`([^`]*\s[^`]*)`", text)]
    assert invented == [["ok", "✓"]], (
        f"control: the shorter spelling was expected to read the prose between two spans as a span "
        f"of exactly two tokens on this input, it read {invented}. Without that this pin proves "
        "nothing about the order of the two operations."
    )
    assert _glyph_status_pairings(text, cli_doctor._STATUS_MARK) == [], (
        "prose standing between two code spans was read as a pairing. Every span has to be matched "
        "first and the token filter applied to what came out, never the other way round."
    )
    written = "`x` `✓ ok` `y`"
    assert ("ok", "✓") in _glyph_status_pairings(written, cli_doctor._STATUS_MARK), (
        "floor: a pairing written inside ONE span is no longer read at all, so the emptiness above "
        "would pass for a span pass that had been deleted rather than reordered."
    )


def test_the_span_pass_reads_a_span_of_exactly_two_tokens_and_no_longer_one() -> None:
    """The two-token filter, which this page called unpinnable for two rounds.

    It was listed among the things NOT held, on the claim that it "cannot be reddened by
    any input". That is a universal built out of a TREE measurement, and the measurement it rests on
    says something narrower: relaxing the filter changes no result on this tree. Measured, the two
    rules disagree on constructed input at once, so the property is pinnable exactly like the eleven
    others this file pins, and the odd-backtick filter one section above got that treatment in the
    same round for the same tree-invisibility.

    ``len(tokens) != 2`` versus ``len(tokens) < 2``, the relaxation the docstring of
    ``_glyph_status_pairings`` names and rejects ("a longer span is prose in code formatting, and
    its first two words are not thereby written as a pair"). The pass only ever inspects
    ``tokens[0]`` and ``tokens[1]``, so any span whose first two tokens are a status and a mark
    followed by more words separates them.

    The first assertion is the control, in the shape this file requires beside an emptiness check:
    on the same input the relaxed rule DOES read the pairing, so the emptiness below is a statement
    about the rule and not about a pass that has stopped reading. The last one is the non-empty
    floor.

    Red-proof: change ``if len(tokens) != 2`` to ``< 2`` in ``_glyph_status_pairings``.
    """
    longer = "`✓ ok extra`"
    relaxed = [
        pair
        for span in _code_spans(longer)
        if len(span.split()) >= 2
        for pair in ((span.split()[0], span.split()[1]), (span.split()[1], span.split()[0]))
        if pair[0] in cli_doctor._STATUS_MARK and pair[1] in _GLYPH_CHARACTERS
    ]
    assert relaxed == [("ok", "✓")], (
        f"control: the relaxed rule was expected to read this three-token span as a pairing, it "
        f"read {relaxed}. Without that the emptiness below says nothing about the filter."
    )
    assert _glyph_status_pairings(longer, cli_doctor._STATUS_MARK) == [], (
        "a span of three tokens was read as a pairing. Only a span that is NOTHING BUT a status "
        "and a mark is one; the first two words of a sentence in code formatting are not written "
        "as a pair, which is what the filter says and what nothing held until now."
    )
    assert ("ok", "✓") in _glyph_status_pairings("`✓ ok`", cli_doctor._STATUS_MARK), (
        "floor: the two-token form itself is no longer read, so the emptiness above would pass for "
        "a span pass that had been deleted rather than narrowed."
    )


def test_a_renamed_status_is_reported_though_the_three_counts_still_agree() -> None:
    """The property QA-50's first step bought: the buckets are compared as SETS.

    A rename is cardinality-neutral, so three equal sizes cannot see it. Measured on the tree,
    renaming any of the twelve statuses while leaving the buckets alone was green under the old
    count-only assertion, all twelve times, and for the three then named in neither region it was
    green in every other guard in this file too. (That second clause is dated 2026-07-30: QA-49
    has since put every status in the legend, so no status is named in neither region any more.
    The property this pin holds is unaffected, a rename stays cardinality-neutral.)

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
    while the sentence naming it stayed in the guide, was GREEN in all four guards that existed
    then, and the tiling guard FORCES that bucket removal in the same edit, so the path is the
    likely one rather than an exotic one. Measured on the tree afterwards (2026-07-30), the
    direction caught three of the six statuses then named outside the legend; the other three were
    named in neither region and were held by the set comparison alone. Since QA-49 the legend names
    every status, so the reverse direction and the legend guard both reach all twelve, and this
    direction is what still makes the marked region's own promise true for the prose.

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
    NOT a red-proof for THIS pin and was measured rather than assumed: that variant still matches
    this input, and on the whole tree it yields a byte-identical pairing list. The order of
    operations it breaks has its own pin,
    ``test_the_span_pass_matches_every_span_before_it_filters_on_tokens``, on an input where the
    two spellings disagree. Until that one existed the order was written down
    and held by nothing.
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


def test_a_padded_code_span_is_read_as_the_word_a_reader_sees() -> None:
    """The property this step bought: whitespace inside a code span cannot hide a token.

    The reverse direction compares against what a READER sees, and padding is invisible to one.
    CommonMark drops one leading and one trailing space when both are present, so a padded span
    and its unpadded twin render as the same word. Measured before the strip: the unpadded spelling
    was reported and ALL FOUR padded ones were green, which made two typed spaces a cheaper way out
    of this check than the allowlist entry its own failure message asks for. That is the shape this
    file rejects everywhere else: a repair that is greener than the one the message steers to.

    The second and third assertions are the controls. A padded REAL status must not start being
    reported, or the strip would trade one false green for a false red; and the ``dead`` floor has
    to see through padding as well, or an entry written with padding would be called unwritten.

    Red-proof: drop the ``.strip()`` from ``_prose_token_findings``; the first assertion fails on
    the first padded spelling.
    """
    for padded in (" detail ", "detail ", " detail", "  detail  "):
        _, unknown = _prose_token_findings({"ok", padded}, {"ok"}, frozenset())
        assert unknown == ["detail"], (
            f"a padded code span hid a non-status token: {padded!r} gave {unknown}. A reader sees "
            "the same word as in the unpadded spelling, so the guard has to as well."
        )
    _, unknown = _prose_token_findings({"ok", " ca_error "}, {"ok", "ca_error"}, frozenset())
    assert not unknown, f"control: a padded real status was reported as unknown: {unknown}"
    dead, _ = _prose_token_findings({" detail "}, set(), frozenset({"detail"}))
    assert not dead, f"control: an allowlist entry written with padding was called dead: {dead}"


def test_the_declared_status_locations_still_describe_the_guide() -> None:
    """The two location buckets are held against the two marked regions, in both directions: a
    status declared as named in the prose has to be named there, and a status declared as named
    NOWHERE must not turn up in either region.

    ⚠️ Both of those buckets are EMPTY since QA-49, so both of those halves iterate over nothing.
    They are kept because they define what the buckets MEAN, and a future deliberate gap would
    reactivate them; what they no longer do is carry weight today. The third direction below does,
    and it is what makes the marker printed above the prose region true. Which guard carries what,
    and which assertions are vacuous, is stated here rather than left to be rediscovered.

    The negative half is why the guide carries two marked regions instead of one. Searching the
    WHOLE guide was probed and rejected, and the argument rested on TWO of the three then
    declared-absent names rather than three: ``Disabled`` (guide line 675 today, 647 when this was
    written, and it moves whenever the legend above it grows) is an alarm ``command`` value and
    ``Info`` is an Olog level, both capitalised, so only a case-INSENSITIVE whole-file search
    reddens on them. ⚠️ The sentence that stood here about the third name was measured FALSE on
    2026-08-02: it claimed ``disconnected`` appears in the guide "never as a code span", and it
    appears as one twice, outside both marked regions, in the ``ConnectionState`` namespace of
    ``diagnose_connection``. That makes it the same case as the other two rather than a weaker one,
    which strengthens the argument it was cited against. Two names are enough,
    because a whole-file search reddens on an edit that documented something else entirely, and the
    obvious repair for that red is fully GREEN while quietly retiring the gap this bucket exists to
    record. Confining both halves to the marked regions costs a false negative (a status documented
    in a third place goes unnoticed) and buys the absence of a false positive with a green wrong
    repair.

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
    in all four guards that existed then (2026-07-30). That is the likely path rather than an
    exotic one, because the tiling guard FORCES the bucket to be cleared in the same edit. So every
    lower-case identifier written as a code span in the prose region has to be a plane status.
    (A status that also has a legend row is caught by the legend guard as well; those three were
    caught by nothing. Since QA-49 every status has a legend row, so this direction is no longer
    the only cover for any of them, and it is kept because it is the only one that reads the PROSE
    region and therefore the only one that makes the marker printed above it true.)

    Only the prose region, not the legend. The legend region carries ``degraded_planes`` and
    ``name``, so extending this half there would need two allowlist entries on the first day, and an
    allowlist that is needed immediately is an excuse rather than a promise. What this direction
    does NOT catch, and what therefore stays unheld: a status name outside both
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


def test_the_status_prose_still_names_every_failing_status() -> None:
    """The prose region's PRESENCE guard, derived from the code rather than declared (QA-49).

    It replaces a half that QA-49 would otherwise have removed silently, and that is worth writing
    down because the loss was invisible until it was probed. Before this ticket, ``missing`` above
    required the three prose-only statuses to still be written in the marked region; measured,
    deleting `` `unreachable` `` from the exit-1 sentence reddened it. Once the legend names every
    status, ``_IN_THE_STATUS_PROSE`` is empty, that half iterates over nothing, and the same
    deletion is green in all five guards. The marked region still promises the reader its statuses
    are "drift-guarded against PlaneStatus", and without this guard that promise would only hold in
    the DELETE direction (a removed status reddens the reverse half) and not in the PRESENCE one.

    DERIVED, not declared, and that is the whole improvement. A declared list can be emptied one
    entry at a time, which decision OO (c) measured on this file's own allowlist: declaring a token
    away was exactly as effective as deleting the check and easier to justify. ``_FAILING_STATUSES``
    is the set the sentence is ABOUT, it lives in the code the guide documents, and nobody can trim
    it here without changing what ``epics-doctor`` treats as a failure.

    Twice the reach for it: the declared half covered three statuses, this covers six. What it
    deliberately does not check is the sentence's wording, only that each name is written there as
    a code span, for the reason section 13 of ``docs/known-limits.md`` gives about prose guards.

    Red-proof: remove `` `disconnected` `` (or any other failing status) from the exit-1 sentence in
    the ``status-prose`` region.
    """
    prose = set(_code_spans(_guide_region(_STATUS_PROSE_RE, "status-prose")))
    assert doctor._FAILING_STATUSES, "the failing-status set is empty, the code anchor broke"
    unwritten = sorted(doctor._FAILING_STATUSES - prose)
    assert not unwritten, (
        f"the guide's status prose no longer names {unwritten}, which epics-doctor treats as a "
        "hard failure. The marked region tells the reader its statuses are drift-guarded, so a "
        "failing status has to be written there as a code span. Add it to the exit-1 sentence; do "
        "not narrow this guard, it reads the code's own set on purpose."
    )


# --- the WIRING, pinned by running a guard on a doctored world -----------------------------------
#
# The pins above hold the HELPERS on constructed input, and that is not the same as holding the
# guards. Measured against real pytest: reverting the set comparison, the reverse direction or the
# duplicate count AT THE GUARD, while leaving the helper and its pin in place, left all 1702 tests
# green, which is exactly what the pins were built to prevent. The helper goes on being correct and
# nobody asks it anything. Only reverting the leading-pipe rule reddened, and only because that
# behaviour lives inside the reader the guard itself calls.
#
# So these three run the GUARD, on a world doctored for one line, and require its own message. They
# patch a module global rather than injecting, which is the opposite of what the helpers above do,
# and the difference is not a lapse: a helper takes its world as arguments and can be handed a
# fixture, while a guard takes none, so there is nothing to inject. Patching the source is the only
# way to ask a guard a question. Keep them; simplifying them back into helper pins reopens the seam
# they were written to close.
#
# What they do NOT cover, stated so the next reader does not have to measure it: their own removal.
# No gate in this repository sees a deleted test, and an emptied test body is reported as passed by
# pytest and ignored by ruff, whose rule set here carries no flake8-pytest checks.
#
# ⚠️ A wiring pin is expensive per assertion, and for one round only three of them existed while the
# four status guards of the time carried twelve assert statements (2026-07-30; there are six guards
# and fifteen assertions since QA-49, and the three added by it are NOT individually revert-measured
# the way those twelve were). Measured against the full suite, TEN of the
# remaining reverts were silent, including both of the sentences this file exists to make true. So
# the breadth is carried by _ASSERTED_NAMES below, which is cheap and weaker, and the two central
# comparisons carry a wiring pin on top. What each grade buys is written where it is checked.


#: The names each listed guard asserts ON, declared so that DELETING an assertion is loud. Read off
#: the ``test`` expression of every ``assert`` in the guards below; the message is not read, because
#: a reverted assertion often leaves its message behind. No count is stated here on purpose: the
#: population grows every round, nothing reads this comment, and a stale figure beside a derived set
#: reads as a measurement. Re-derive it from the dict.
#:
#: Declared rather than derived, for the reason ``_IN_THE_GLYPH_TABLE`` gives: a derived expectation
#: is satisfied by whatever the file happens to say today, which is the one thing this cannot check.
#:
#: ⚠️ A guard that is NOT listed here is covered by nothing, and nothing says so: the floor below
#: compares ``declared - found`` and has no reverse direction, so a NEW guard added without a row
#: would be silently unfloored. That gap is recorded in ``docs/known-limits.md`` section 14.
#: Adding a guard means adding its row in the same edit.
#:
#: ⚠️ And a row has to name the FINDING, not just the inputs. A row whose names are already
#: satisfied by its guard's own non-empty floors leaves the central comparison deletable in silence,
#: which is the sham shape this floor exists to prevent; measured on the QA-73 pair before their
#: finding variables were named.
_ASSERTED_NAMES: dict[str, frozenset[str]] = {
    "test_the_guide_status_buckets_tile_plane_status": frozenset(
        {"statuses", "only_in_buckets", "only_in_plane_status", "double_listed"}
    ),
    "test_the_shipped_legend_names_every_plane_status": frozenset({"unlisted"}),
    "test_the_status_prose_still_names_every_failing_status": frozenset({"doctor", "unwritten"}),
    "test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints": frozenset(
        {"repeated", "documented", "_IN_THE_GLYPH_TABLE", "wrong"}
    ),
    "test_every_glyph_status_pairing_in_the_docs_agrees_with_the_render_marks": frozenset(
        {"absent", "pairings", "where", "_PAIRING_EXPECTED", "mismatched"}
    ),
    "test_the_declared_status_locations_still_describe_the_guide": frozenset(
        {"missing", "surfaced", "dead", "unknown"}
    ),
    # QA-73. Not status guards, but the same floor applies for the same reason, and the dict has no
    # reverse direction: a guard added without a row here is unfloored and nothing says so.
    # ⚠️ The FINDING names carry the row, not the input names. Measured on the first version, which
    # declared only {"guide", "reported"} and {"literals", "reported"}: both were already satisfied
    # by each guard's own non-empty floors, so the central comparison could be deleted outright with
    # this floor staying green. A row satisfied by its guard's floors declares nothing.
    "test_the_guide_names_every_plane_the_report_prints": frozenset(
        {"guide", "reported", "only_in_guide", "only_in_report"}
    ),
    "test_every_plane_literal_in_the_source_reaches_the_report": frozenset(
        {"literals", "reported", "only_in_source", "unlit_in_report"}
    ),
}


def test_every_status_guard_still_asserts_on_what_it_computes() -> None:
    """A floor over every guard in ``_ASSERTED_NAMES``: an assertion cannot be deleted in silence.

    The wiring pins above each hold ONE property, by running its guard on a doctored world. That is
    the strong grade and it is expensive. This is the cheap one, and its whole job is breadth:
    measured against the full suite before it existed, ten of the twelve assert statements in the
    four guards of the time could be removed with all 1710 tests green, among them the legend's
    mark comparison and the pairing scan's mismatch comparison, which are the two sentences this
    file exists to make true.

    What it buys: every DELETED assertion and every RENAMED finding variable goes red here, twelve
    of twelve, measured by removing each statement in turn (2026-07-30). ⚠️ Later rounds added more
    guards and assertions (QA-49, then QA-73) which ride the same MECHANISM but were not put through
    that per-statement sweep. The claim is therefore about the twelve that were measured, and the
    current totals are deliberately NOT restated here: they move every round, nothing reads this
    docstring, and a stale count would read as a measurement. Re-derive them from
    ``_ASSERTED_NAMES`` instead.

    ⚠️ ``ast.AsyncFunctionDef`` is read alongside ``ast.FunctionDef``, and that is load-bearing
    rather than tidy: the QA-73 guards are ``async def``, and reading only the sync node type would
    have left them out of ``found`` while their rows stood in the dict. That does not fail open,
    it fails LOUD on the ``absent`` assertion below, which is why it was caught; a guard whose row
    simply never matched would be the dangerous shape.

    What it does NOT buy, stated rather than implied: an assertion that stays and stops meaning
    anything. A finding list emptied where it is built (``wrong = []``) leaves the name standing in
    the assert and passes here. That is why the two central comparisons carry a wiring pin as well,
    and why this floor is a floor rather than a replacement.

    Red-proof: delete any assert statement from any guard listed in ``_ASSERTED_NAMES``, or rename
    one of their finding variables.
    """
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    found = {
        node.name: {
            name.id
            for statement in ast.walk(node)
            if isinstance(statement, ast.Assert)
            for name in ast.walk(statement.test)
            if isinstance(name, ast.Name)
        }
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in _ASSERTED_NAMES
    }
    absent = sorted(set(_ASSERTED_NAMES) - set(found))
    assert not absent, (
        f"a declared status guard is not in this file any more: {absent}. If it was renamed, move "
        "the name here as well; do not let it drop out, because its whole row would leave with it."
    )
    lost = {
        guard: sorted(declared - found[guard])
        for guard, declared in _ASSERTED_NAMES.items()
        if declared - found[guard]
    }
    assert not lost, (
        f"a status guard no longer asserts on something it computes: {lost}. Either the assertion "
        "was deleted, in which case the guard now checks less than it claims to, or the variable "
        "was renamed, in which case _ASSERTED_NAMES has to move with it."
    )


def test_a_padded_span_cannot_hide_a_status_from_the_location_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Padding no longer hides a name from the guard's NEGATIVE half either.

    ``test_a_padded_code_span_is_read_as_the_word_a_reader_sees`` holds the helper. This holds the
    guard, and it has to, because the two halves that compare against the region's spans directly
    are not the helper: they are three lines inside
    ``test_the_declared_status_locations_still_describe_the_guide``.

    Measured before the reader was made to strip, against the REAL guide doctored inside its
    markers: the bare spelling reddened with "declared as named NOWHERE", and all four padded
    spellings were green while rendering identically for a reader. So two typed spaces retired the
    gap that ``_NOT_NAMED_IN_THE_GUIDE`` exists to record, and the guide's own sentence promising
    that documenting one of those statuses "goes red the moment" it happens was untrue.

    ⚠️ The doctored world has to remove the ``disabled`` LEGEND ROW as well, and getting that wrong
    turns this pin into a guard that checks nothing. QA-49 made the legend total, so every status is
    now in it; the negative half reads ``status in prose or status in legend``, and with the row
    still there it reports "named NOWHERE" from the LEGEND, whatever the doctored prose says.
    Measured in exactly that state: all five spellings raise the expected message even with the
    ``.strip()`` removed, so the pin passes while its own red-proof is applied. Patching only the
    bucket, which is the obvious repair when this pin first fails, buys precisely that.

    Hence the third control below, and it is the one this pin was missing. A positive control
    ("the bare spelling must redden too") cannot tell a working guard from one that reddens on
    everything, which is what the state above was. The NEGATIVE control asserts the doctored world
    is green BEFORE the sentence is added, so the red has to come from the sentence.

    Red-proof: drop the ``.strip()`` from ``_code_spans``; the four padded spellings go green.
    """
    end = "<!-- END:status-prose -->"
    # The legend row for ``disabled`` has to go, or the negative half fires on the legend and this
    # pin never exercises the prose at all. Bound once and asserted, so a reformatted row is loud.
    without_row = re.sub(r"(?m)^\|\s*`·`\s*\|\s*`disabled`\s*\|.*\n", "", get_guide(), count=1)
    assert without_row != get_guide(), (
        "the `disabled` legend row was not found in the shipped guide, so this pin would test the "
        "legend rather than the prose. If the row was reformatted, move this pattern with it."
    )
    monkeypatch.setitem(globals(), "_NOT_NAMED_IN_THE_GUIDE", frozenset({"disabled"}))
    # NEGATIVE control first: without the added sentence the doctored world must be GREEN, or every
    # assertion below would pass for a guard that reddens on anything.
    monkeypatch.setitem(globals(), "get_guide", lambda text=without_row: text)
    test_the_declared_status_locations_still_describe_the_guide()
    for spelling in ("` disabled `", "`disabled `", "` disabled`", "`  disabled  `", "`disabled`"):
        doctored = without_row.replace(end, f"\nThe {spelling} plane is skipped.\n\n" + end, 1)
        assert doctored != without_row, (
            "the status-prose end marker moved, this pin patched nothing"
        )
        monkeypatch.setitem(globals(), "get_guide", lambda text=doctored: text)
        with pytest.raises(AssertionError, match="named NOWHERE"):
            test_the_declared_status_locations_still_describe_the_guide()


def test_the_legend_guard_reports_a_duplicate_row_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The duplicate count is still ASKED FOR by the legend guard.

    ``test_a_legend_row_listed_twice_is_reported`` proves ``_repeated_statuses`` finds a repeat;
    this proves the guard still calls it. Measured before this pin: replacing the guard's
    ``repeated = _repeated_statuses(rows)`` with an empty list left every test in the suite green,
    including that pin, so the property was documented rather than held.

    Red-proof: drop the two ``repeated`` lines from
    ``test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints``.
    """
    doubled = [("✓", "ok"), ("✓", "ok"), ("~", "no_ingest")]
    monkeypatch.setitem(globals(), "_glyph_rows", lambda: doubled)
    with pytest.raises(AssertionError, match="more than one row"):
        test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints()


def test_the_tiling_guard_reports_a_rename_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SET comparison is still ASKED FOR by the tiling guard.

    ``test_a_renamed_status_is_reported_though_the_three_counts_still_agree`` proves
    ``_tiling_findings`` reports a rename; this proves the guard still asserts on what it returns.
    Measured before this pin: putting the retired three-count form back into the guard, while the
    helper and its pin stayed untouched, left all 1702 tests green.

    Red-proof: assert ``sum(map(len, buckets)) == len(union) == len(statuses)`` in the guard again.
    """
    renamed = frozenset({"ok_renamed" if s == "ok" else s for s in _IN_THE_GLYPH_TABLE})
    monkeypatch.setitem(globals(), "_IN_THE_GLYPH_TABLE", renamed)
    with pytest.raises(AssertionError, match="no longer tile PlaneStatus"):
        test_the_guide_status_buckets_tile_plane_status()


def test_the_location_guard_reports_an_unknown_prose_token_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reverse direction is still ASKED FOR by the location guard.

    ``test_a_prose_token_that_is_not_a_status_is_reported`` proves ``_prose_token_findings`` reports
    a stranger; this proves the guard still asks it about the shipped prose. Measured before this
    pin: dropping the call from the guard left all 1702 tests green.

    The doctored world is the real guide with one sentence added inside the marked prose region, so
    the pin exercises the same extraction path the guard uses, markers and all.

    Red-proof: drop the ``_prose_token_findings`` call from
    ``test_the_declared_status_locations_still_describe_the_guide``.
    """
    end = "<!-- END:status-prose -->"
    doctored = get_guide().replace(end, "\nThe `nonesuch` field is printed too.\n\n" + end, 1)
    assert doctored != get_guide(), "the status-prose end marker moved, this pin patched nothing"
    monkeypatch.setitem(globals(), "get_guide", lambda: doctored)
    with pytest.raises(AssertionError, match="as if epics-doctor printed them"):
        test_the_declared_status_locations_still_describe_the_guide()


def test_the_legend_guard_reports_a_wrong_glyph_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MARK comparison is still ASKED FOR by the legend guard.

    This is one of the two sentences the whole file exists to make true: the shipped legend shows
    the glyph ``epics-doctor`` renders. Measured before this pin, against the full suite: deleting
    the comparison left all 1710 tests green, because the tree agrees and nothing else reads that
    cell. A promise nothing asks for is the shape this file spent three rounds removing.

    The doctored rows keep the declared status SET and carry no duplicate, so the guard's other two
    assertions are satisfied and only the mark comparison can fire. The second half is the control:
    the same rows with the right glyph must pass, or the pin would hold "the guard raises" rather
    than "the guard compares marks".

    Red-proof: drop the two ``wrong`` lines from
    ``test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints``.
    """
    correct = [(cli_doctor._STATUS_MARK[status], status) for status in sorted(_IN_THE_GLYPH_TABLE)]
    wrong_mark = next(m for m in _GLYPH_CHARACTERS if m != correct[0][0])
    monkeypatch.setitem(
        globals(), "_glyph_rows", lambda: [(wrong_mark, correct[0][1]), *correct[1:]]
    )
    with pytest.raises(AssertionError, match="advertises a different glyph"):
        test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints()
    monkeypatch.setitem(globals(), "_glyph_rows", lambda: correct)
    test_the_shipped_glyph_legend_carries_the_marks_the_cli_prints()


def test_the_pairing_guard_reports_a_wrong_glyph_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MISMATCH comparison is still ASKED FOR by the cross-page pairing scan.

    The other of the two sentences the file exists to make true: every glyph documented beside a
    status name, on the guide or on any tracked page, is the one the CLI renders. Measured before
    this pin, against the full suite: deleting the comparison left all 1710 tests green.

    The wrong pairing is injected OUTSIDE both marked regions and uses a mark that is not a
    lower-case identifier, and both choices are deliberate: inside a marked region, or with the
    ``info`` mark, the doctored guide reddens the LOCATION guard as well and this pin would pass for
    the wrong reason.

    ⚠️ This pin walks the same population the guard walks, so it SKIPS with it when the git binary
    is missing, the one skip ``_tracked_doc_pages`` allows. It is named here rather than worked
    around, because doctoring the population instead would trip the ``absent`` input floor first.

    ⚠️ The fixture mark is FILTERED and then SORTED, and both halves are a repair rather than a
    style choice. This used to read ``next(m for m in _GLYPH_CHARACTERS if m != ...)``, which draws
    from a ``frozenset``: iteration order over a set of one-character strings follows the
    per-process hash seed, so the draw was a coin flip. When it landed on ``i``, the only ALNUM
    member of the class, the assertion below fired and this pin went red over its own fixture
    rather than over anything it guards. Measured on the unmodified tree, the same single test
    under twelve hash seeds: **10 green, 2 red**, red exactly in the two runs that drew ``i``
    (``PYTHONHASHSEED=7`` reproduces it every time). Choosing the mark instead of drawing it makes
    that impossible, and the assertion below stays as a floor on the CLASS: it now says "no usable
    mark exists at all", which is a real change to ``_GLYPH_CHARACTERS`` rather than bad luck.

    Red-proof: drop the two ``mismatched`` lines from
    ``test_every_glyph_status_pairing_in_the_docs_agrees_with_the_render_marks``.
    """
    usable = sorted(
        m for m in _GLYPH_CHARACTERS if m != cli_doctor._STATUS_MARK["ok"] and not m.isalnum()
    )
    assert usable, (
        "every mark besides ok's is a lower-case identifier, so no fixture mark is left that "
        "leaves the location guard alone; see this docstring before widening the choice."
    )
    wrong_mark = usable[0]
    doctored = get_guide() + f"\n\nA healthy plane reports `ok` (`{wrong_mark}`).\n"
    monkeypatch.setitem(globals(), "get_guide", lambda: doctored)
    with pytest.raises(AssertionError, match="disagrees with the one epics-doctor renders"):
        test_every_glyph_status_pairing_in_the_docs_agrees_with_the_render_marks()
