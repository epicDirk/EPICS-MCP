"""Pin the reading behaviour of ``tests/prose_numbers.py``, over SYNTHETIC prose.

S37. The module had no test of its own. Its two recorded repairs, the sentence-boundary look-ahead
and the rule that a gap word may not end in a terminator, could both be reverted with nothing going
red. This module is what closed that, and it is where the limit is now recorded: what these tests
establish is what the module DOES with prose somebody wrote for them, never that the watched prose
is read correctly, which is the comparing half's job.

WHY SYNTHETIC, and not the watched tree: on the prose the guard actually reads, the three spellings
of the boundary class (any letter; a literal ``[A-Z]`` under ``re.IGNORECASE``; and ``[A-Z]`` forced
case-sensitive with ``(?-i:...)``) find an IDENTICAL set of sites and keys. Measured, repeatedly. A
test written against the real files therefore cannot separate them at all, and would report a
behaviour as guarded while every mutation of it stayed green. Constructed input is the only place
the difference exists, so it is the only place the decision can be pinned.

THE RED-PROOF, stated so it is not re-derived: the mutant that makes
:func:`test_a_lower_case_word_after_a_terminator_still_ends_the_sentence` fail is the SCOPED
``(?![.;:]\\s+(?-i:[A-Z]))``. A bare ``[A-Z]`` is not a mutation at all, because ``re.IGNORECASE``
makes it match lower case too, so an author checking their own repair against it measures nothing
and concludes it is safe. It is easy to walk into while trying to test the rule.

SCOPE. This pins what the module DOES, including the limits its own docstring claims, so the scope
statement stops being prose. It says nothing about whether the watched prose is correct; that is
``tests/test_prose_counters.py``, the comparing half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import prose_numbers as pn
from tests.prose_numbers import ProseBlock, iter_blocks, parse_count, sites_of


def _values(text: str) -> list[int]:
    """Every size-naming number the module reads out of one flattened block of prose."""
    return [site.value for site in sites_of(ProseBlock("probe.py", 1, "<module>", text))]


def _blocks(tmp_path: Path, source: str, name: str = "probe.py") -> tuple[pn.ProseBlock, ...]:
    """Run *source* through the real file path: tokenizer, syntax tree, paragraph splitting."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return iter_blocks([(name, path)])


# --- the sentence boundary: the look-ahead, and the decision NOT to require a capital -------------


@pytest.mark.parametrize("terminator", [".", ";", ":"])
def test_a_lower_case_word_after_a_terminator_still_ends_the_sentence(terminator: str) -> None:
    """The rule that a capital-only class would break, which is why the class says "any letter".

    "we counted 7" names no collection, and "tools" belongs to the next sentence. A boundary class
    that only recognised a capital would not fire here, the gap would cross the terminator, and the
    7 would be reported as a claim about tools: the cross-sentence pairing the look-ahead exists to
    prevent. This is the test the scoped mutant in the module docstring turns red.
    """
    assert _values(f"we counted 7{terminator} both tools agree") == []


def test_a_capital_after_a_terminator_ends_the_sentence_too() -> None:
    """The other direction, so the test above cannot pass by ignoring the boundary altogether."""
    assert _values("we counted 7. Both tools agree") == []


def test_a_pairing_inside_a_sentence_survives_a_boundary_later_in_the_block() -> None:
    """The short, valid pairing must be found, and it must not swallow the following sentence.

    The snippet is asserted, not just the value: a match reaching through to "tools" would report
    the same number 7 under a different phrase, and the phrase is half of a site's identity.
    """
    sites = sites_of(ProseBlock("probe.py", 1, "f", "the 7 rows. Both tools agree"))
    assert [(site.value, site.snippet) for site in sites] == [(7, "7 rows")]


def test_a_full_stop_inside_an_abbreviation_is_not_a_sentence_end() -> None:
    """ "7 (resp. 9) rows" states two sizes of two different sets, and both are real claims."""
    assert _values("7 (resp. 9) rows") == [7, 9]


def test_a_gap_word_may_not_end_in_a_terminator() -> None:
    """The second recorded repair. Without it the gap word swallows the terminator itself.

    "etc." would be consumed as an ordinary intervening word, the look-ahead would never see the
    full stop it tests for, and the break would not fire, so the cross-sentence pairing was
    ACCEPTED where the older post-filter had at least rejected it.
    """
    assert _values("we counted 7 etc. Both tools agree") == []


# --- the gap: width, and that nothing is filtered after a match -----------------------------------


def test_a_greedy_gap_does_not_consume_the_phrase_it_failed_on() -> None:
    """The defect that cost the same lesson twice: a rejected match is not a neutral event.

    With the collection noun as a post-filter, the gap matched "22 schemas finds the same" through
    to "enum", the hit was dropped, and ``finditer`` resumed PAST the phrase, so the valid pairing
    inside it was never tried.
    """
    assert _values("all 22 schemas finds the same enum") == [22]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the 22 schemas", [22]),
        ("the 22 shipped schemas", [22]),
        ("the 22 shipped typed schemas", [22]),
        ("the 22 shipped typed extra schemas", []),
    ],
)
def test_the_gap_spans_at_most_two_intervening_words(text: str, expected: list[int]) -> None:
    """At most two, a documented limit rather than an accident: the last case is blind by design."""
    assert _values(text) == expected


def test_the_elided_noun_shape_is_read_as_well() -> None:
    """The elided shape names two sizes with the noun left out; both are highly drift-prone."""
    assert _values("TEN of the twelve") == [10, 12]
    assert _values("13 of the 20") == [13, 20]


def test_one_site_per_number_not_per_phrase() -> None:
    """A phrase naming two sizes yields two addressable sites, at their own offsets."""
    sites = sites_of(ProseBlock("probe.py", 1, "f", "7 (resp. 9) rows"))
    assert [(site.value, site.offset) for site in sites] == [(7, 0), (9, 9)]


# --- what a number is, and what it is not ---------------------------------------------------------


@pytest.mark.parametrize("text", ["v2.0 tools", "ISO-8601 fields", "DS-4A rows", "3.5 rows"])
def test_a_version_an_identifier_and_a_fraction_are_not_sizes(text: str) -> None:
    """The lookbehinds, against the shapes this prose is full of."""
    assert _values(text) == []


def test_an_underscore_separated_number_is_one_number() -> None:
    assert _values("200_000 keys") == [200000]


@pytest.mark.parametrize(
    "text",
    [
        "about 60 such pairings",  # the noun is outside the closed list
        "three independent modes",  # likewise, and deliberately so
        "twenty-two schemas",  # a hyphenated compound
    ],
)
def test_the_documented_blind_spots_stay_blind(text: str) -> None:
    """The scope statement in the module docstring, as a test rather than as a promise.

    These are not defects. Widening any of them is a decision with its own cost, and it should
    turn this test red rather than pass unnoticed.
    """
    assert _values(text) == []


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("22", 22),
        ("200_000", 200000),
        ("TEN", 10),
        ("ten", 10),
        ("twenty", 20),
        ("ninety", None),  # past the closed list
        ("", None),
        ("v2", None),
    ],
)
def test_parse_count_reads_digits_and_the_closed_word_list(
    token: str, expected: int | None
) -> None:
    assert parse_count(token) == expected


# --- the reading half: paragraphs, comment runs, scopes, and the tokenizer ------------------------


def test_a_claim_wrapped_across_a_docstring_line_is_still_one_claim(tmp_path: Path) -> None:
    """Prose is read per paragraph, because a line-wise regex is silently lossy on a wrap."""
    source = '''def f():
    """The guard reads EXACTLY the 11
    mapped fields and nothing else."""
'''
    blocks = _blocks(tmp_path, source, "wrap.py")
    assert [site.value for block in blocks for site in sites_of(block)] == [11]


def test_a_blank_line_ends_a_docstring_paragraph(tmp_path: Path) -> None:
    """Paragraph, not whole-block: flattening a docstring lets a match jump a paragraph break.

    Both halves are asserted. The split prose yields nothing, and the same text flattened yields
    the pairing, so this measures the rule and not merely the absence of a hit.
    """
    source = '''def f():
    """The table below lists 22

    schemas are validated elsewhere."""
'''
    blocks = _blocks(tmp_path, source, "para.py")
    assert [site.value for block in blocks for site in sites_of(block)] == []
    assert _values(" ".join(block.text for block in blocks)) == [22]


def test_a_bare_hash_ends_a_comment_run(tmp_path: Path) -> None:
    """A bare ``#`` is the comment spelling of a blank line, and without it runs fuse.

    Measured the same way as the paragraph rule: the fused form DOES produce a pairing, so the
    separator is load-bearing rather than decorative.
    """
    separated = (
        "# everything lives in one module\n#\n# the functions resolve their own names\nx = 1\n"
    )
    fused = "# everything lives in one module\n# the functions resolve their own names\nx = 1\n"
    blocks = _blocks(tmp_path, separated, "sep.py")
    assert [site.value for block in blocks for site in sites_of(block)] == []
    fused_blocks = _blocks(tmp_path, fused, "fused.py")
    assert [site.value for block in fused_blocks for site in sites_of(block)] == [1]


def test_adjacent_comment_lines_at_one_indentation_join(tmp_path: Path) -> None:
    """The comment counterpart of the wrapped docstring claim."""
    source = "# the guard reads EXACTLY the 11\n# mapped fields and nothing else\nx = 1\n"
    blocks = _blocks(tmp_path, source, "run.py")
    assert [site.value for block in blocks for site in sites_of(block)] == [11]


def test_a_site_is_keyed_by_its_enclosing_scope(tmp_path: Path) -> None:
    """The innermost class or function, which is what makes a key survive edits above it."""
    source = """class C:
    def m(self):
        # all 22 schemas here
        pass
"""
    blocks = _blocks(tmp_path, source, "qual.py")
    assert [block.qualname for block in blocks] == ["C.m"]


def test_the_key_ignores_the_line_number() -> None:
    """A table keyed on line numbers rots for a reason that is not a change in the finding."""
    early = sites_of(ProseBlock("probe.py", 1, "f", "all 22 schemas"))[0]
    late = sites_of(ProseBlock("probe.py", 999, "f", "all 22 schemas"))[0]
    assert early.key == late.key == ("probe.py", "f", "22 schemas", 22)


def test_a_form_feed_does_not_desync_the_reported_lines(tmp_path: Path) -> None:
    """Why the tokenizer is fed through ``io.StringIO`` rather than ``str.splitlines``.

    ``str.splitlines`` breaks on a form feed and the tokenizer does not, so one page separator
    would shift every row below it, and rows feed the scope lookup, which is the key this module
    promises stays stable. The assertion on ``splitlines`` is what makes the risk visible: it
    reports one entry more than the file has lines.
    """
    source = "# first with 22 schemas\n\fx = 1\n# second with 13 rows\n"
    assert len(source.splitlines()) == 4, "the premise of this test: splitlines over-counts here"
    blocks = _blocks(tmp_path, source, "feed.py")
    assert [block.lineno for block in blocks] == [1, 3]


# --- the markdown reader: same patterns, a different way of finding the blocks --------------------
#
# Added for [GQ-123]. The reason it exists is measured rather than argued: of the seventeen wrong
# numbers [GQ-117] repaired in one day, not one sat where this detector could see it, and three sat
# at a place it matches exactly, in a file the watch list did not carry. The limit was the READER.


def _md(tmp_path: Path, source: str, name: str = "probe.md") -> tuple[pn.ProseBlock, ...]:
    """Run *source* through the real file path, so the suffix dispatch is exercised too."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return iter_blocks([(name, path)])


def test_a_markdown_file_is_read_by_suffix_not_parsed_as_python(tmp_path: Path) -> None:
    """The whole extension in one assertion: ``ast.parse`` would raise on this text."""
    blocks = _md(tmp_path, "# Title\n\nThe four write tools are gated.\n")
    assert [site.value for block in blocks for site in sites_of(block)] == [4]


def test_a_block_is_keyed_by_its_nearest_heading(tmp_path: Path) -> None:
    """The markdown answer to "never key on a line number", and it is the nearest heading."""
    source = "# Guide\n\n## Palette\n\nThe four write tools are gated.\n"
    blocks = _md(tmp_path, source)
    assert [block.qualname for block in blocks] == ["Guide", "Palette", "Palette"]


def test_text_before_the_first_heading_is_keyed_as_the_document(tmp_path: Path) -> None:
    """A file may open with prose, and that prose still needs a key that is not a line number."""
    blocks = _md(tmp_path, "The four write tools are gated.\n\n# Guide\n")
    assert blocks[0].qualname == "<document>"


def test_a_heading_is_itself_read_as_prose(tmp_path: Path) -> None:
    """Not a convenience: the SHIPPED guide states a size inside one.

    ``### Olog write posture (all four write tools)`` is a real heading of
    ``src/epics_mcp/operator_guide.md``, and a reader that treated headings as structure alone
    would be blind to a claim in the text an operator reads first.
    """
    blocks = _md(tmp_path, "# Guide\n\n### Olog write posture (all four write tools)\n\nText.\n")
    assert [site.value for block in blocks for site in sites_of(block)] == [4]


def test_a_fenced_code_block_is_not_prose(tmp_path: Path) -> None:
    """A fence carries identifiers and version pins, and a size claim inside one is not a claim."""
    source = "# Guide\n\n```\nthe four write tools\n```\n\nThe two round-tripping tools differ.\n"
    assert [site.value for block in _md(tmp_path, source) for site in sites_of(block)] == [2]


def test_a_tilde_fence_closes_only_on_a_tilde(tmp_path: Path) -> None:
    """The closing fence is the character that opened the run, else a nested one ends it early."""
    source = "# Guide\n\n~~~\n```\nthe four write tools\n```\n~~~\n\nThe two tools differ.\n"
    assert [site.value for block in _md(tmp_path, source) for site in sites_of(block)] == [2]


def test_each_list_item_is_its_own_block(tmp_path: Path) -> None:
    """Measured on the shipped guide: fusing a list lets a pairing reach across an item boundary.

    Without this rule the ``three`` of the first bullet and the ``tools`` of the second are two
    words apart in one flattened block, and the reader reports a size claim nobody wrote.
    """
    source = "# Guide\n\n- the other three\n- tools take a cap\n"
    assert [site.value for block in _md(tmp_path, source) for site in sites_of(block)] == []
    fused = sites_of(ProseBlock("probe.md", 1, "Guide", "- the other three - tools take a cap"))
    assert [site.value for site in fused] == [3], "the premise: fusing the items invents a claim"


def test_a_wrapped_list_item_stays_one_block(tmp_path: Path) -> None:
    """The other direction, and it is the reason the rule keys on the MARKER and not on the line.

    A claim straddles the wrap in markdown exactly as it does in a docstring, so a continuation
    line has to join the item above it or the guard becomes silently lossy.
    """
    blocks = _md(tmp_path, "# Guide\n\n- the four\n  write tools are gated\n")
    assert [site.value for block in blocks for site in sites_of(block)] == [4]


def test_a_table_row_is_its_own_block(tmp_path: Path) -> None:
    """A row is a record, not a sentence, so a number may not pair with a noun one row below.

    The input is chosen so the rule is load-bearing rather than decorative: flattened, these two
    rows read ``| four | | tools |`` and the detector DOES report a size claim there, measured. A
    row pair that produced nothing either way would leave this test green under its own mutant.
    """
    source = "# Guide\n\n| four |\n| tools |\n"
    assert [site.value for block in _md(tmp_path, source) for site in sites_of(block)] == []
    fused = sites_of(ProseBlock("probe.md", 1, "Guide", "| four | | tools |"))
    assert [site.value for site in fused] == [4], "the premise: fusing the rows invents a claim"


def test_a_thematic_break_ends_a_paragraph(tmp_path: Path) -> None:
    """``---`` rules this estate's sections; gluing across one fuses two unrelated paragraphs."""
    source = "# Guide\n\nthe four\n\n---\n\nwrite tools are gated\n"
    assert [site.value for block in _md(tmp_path, source) for site in sites_of(block)] == []
    fused = sites_of(ProseBlock("probe.md", 1, "Guide", "the four --- write tools are gated"))
    assert [site.value for site in fused] == [4], "the premise: gluing across the rule invents one"


def test_ambiguous_headings_reports_a_repeated_section_title(tmp_path: Path) -> None:
    """The soundness condition of keying on the NEAREST heading, made decidable.

    Two sections sharing a title would share a key, and one section's inventory row could then be
    satisfied by the other's phrase. The consumer asserts this is empty for every file it watches;
    here it is proved that the check can see the case at all, which is the positive control for
    every empty answer it gives elsewhere.
    """
    source = "# Guide\n\n## A\n\n### Notes\n\n## B\n\n### Notes\n"
    ambiguous = pn.ambiguous_headings(source)
    assert ambiguous == {"Notes": ["Guide > A > Notes", "Guide > B > Notes"]}
    assert pn.ambiguous_headings("# Guide\n\n## A\n\n## B\n") == {}


def test_the_markdown_reader_leaves_the_python_reader_alone(tmp_path: Path) -> None:
    """The suffix dispatch may not change what a ``.py`` file reads, which is the regression."""
    source = '"""Module with all 22 schemas."""\n\n# and 13 rows here\n'
    blocks = _blocks(tmp_path, source, "still_python.py")
    assert sorted(site.value for block in blocks for site in sites_of(block)) == [13, 22]
