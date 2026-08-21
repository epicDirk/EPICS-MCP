"""Read the NUMBERS that comments, docstrings and shipped markdown claim, so a test can compare
them to the sets.

S32. Prose here routinely names the size of a set, "all 22 schemas", "7 (resp. 9) rows",
"TEN of the twelve". Nothing checked those, and every typing step re-typed them by hand: in the
S29 11th target 18 counters were pulled over, two QA rounds found 8 of them still wrong, and one
had been wrong *before* that build started. This module is the reading half of the guard; the
comparing half is ``tests/test_prose_counters.py``.

Five decisions are load-bearing, and each was forced by a measurement rather than chosen:

* **Prose is read per PARAGRAPH, not per line.** A claim straddles the 100-column wrap often
  enough that a line-wise regex is not merely lossy but silently lossy, ``test_server.py`` splits
  ``EXACTLY the 11`` / ``mapped fields`` across a docstring wrap and ``ArchiverHistoryResult's 11``
  / ``fields`` across a comment wrap, i.e. both halves of the same claim, 40 lines apart. Runs of
  own-line comments are joined the same way as docstring paragraphs.
* **Paragraph, not whole-block.** Flattening a whole docstring lets a match jump a sentence
  boundary: ``...lives in one module.`` + ``The functions resolve...`` reads as "one module. The
  functions" and would be counted as a claim about functions. A bare ``#`` is the comment spelling
  of a blank line and ends a run, without it the rule held for docstrings and quietly not for
  comments, fusing fifteen author paragraphs into single blocks.
* **NOTHING is filtered after a match; every condition lives inside the pattern.** This one cost
  the same defect twice. First the collection noun was a post-filter: the greedy gap matched "all
  22 schemas finds the same enum" through to "the", the hit was dropped, and ``finditer`` resumed
  *past* the phrase, seven of the estate's most drift-prone claims were lost. Then the sentence
  break was a post-filter and did it again: "the 7 rows. Both tools agree" matched from the 7
  through to "tools", was discarded whole, and the valid "7 rows" inside was never tried.
  **A rejected match is not a neutral event; it consumes the text.** Both now sit in the pattern.
* **A site is keyed by its enclosing QUALNAME, never by line number.** Line numbers move with any
  edit above them, so a table keyed on them rots for a reason that is not a change in the finding:
  the lesson ``tests/test_client_edge_guards.py`` records for its own ``_UNOBSERVED`` table. For
  the same reason the tokenizer is fed through ``io.StringIO``: ``str.splitlines`` also breaks on
  form feed, which the tokenizer does not, and one page separator would desync every row below it.
* **The reader is chosen by SUFFIX, and markdown is read by the same patterns.** Measured for
  ``[GQ-123]``: of the seventeen wrong numbers ``[GQ-117]`` repaired in one day, not one sat at a
  place this detector had in view, and three sat at a place it matches EXACTLY in a file nobody had
  put in the watch list. The binding limit was never the pattern and never the vocabulary, it was
  that ``iter_blocks`` called ``ast.parse`` and could therefore open nothing but ``.py``. The
  markdown half keys a block by its nearest heading instead of a qualname, on a soundness condition
  a consumer asserts rather than assumes, see :func:`ambiguous_headings`.

Honest scope, in full, because the numbers invite over-reading. This finds a number that is either
PAIRED with one of the closed ``COLLECTION_NOUNS`` across at most **two** intervening words, or in
the ``N of the M`` shape where the noun is elided. It therefore does NOT see: a size named with any
other noun ("modes", "halves", "planes", measured, about 60 such pairings exist in the watched
prose); a number above twenty spelled as a word; a hyphenated compound ("twenty-two" reads as 20);
a gap of three or more words; and numbers inside f-string assertion messages, which are neither
comments nor docstrings. "Three independent red directions" names a size and is invisible here by
design, those are claims about test bodies, which no constant can settle. The consumer states the
same limits in its own docstring rather than implying coverage.

In markdown the same list holds and two more join it, both by choice rather than by accident: a
number inside a FENCED code block is never read, because a fence carries identifiers and version
pins rather than sentences; and a table ROW is one block, so a number cannot pair with a noun in a
neighbouring row but still can with one in a neighbouring CELL of its own row. Closing the second
needs a cell splitter, and no measured case in this repository asks for one.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Number words the prose actually uses, plus headroom. Deliberately a closed list: a generic
# word-to-int parser would happily read "one" out of "one of the two halves", where the article is
# not a count.
#
# The list deliberately runs past what the watched prose currently spells, and deliberately names no
# figure for how far past: THIS FILE IS UNWATCHED, so a count written into it rots exactly the way
# the counts it guards do. The previous wording named one, and it was wrong.
_WORD_VALUES: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

# The collection nouns a number may be paired with. Closed on purpose, see the module docstring.
COLLECTION_NOUNS: frozenset[str] = frozenset(
    {
        "arrays",
        "assertions",
        "checkers",
        "entries",
        "fields",
        "functions",
        "guards",
        "keys",
        "members",
        "modules",
        "paths",
        "rows",
        "schemas",
        "tests",
        "tools",
        "validators",
    }
)


def _alternation(words: Iterable[str]) -> str:
    """Longest alternative first, so a prefix cannot shadow a longer member of the same list."""
    return "|".join(sorted(words, key=len, reverse=True))


# A number is digits (``200_000``), never a decimal fraction or a hyphenated compound: the
# lookbehind rejects ``v2.0``, ``ISO-8601`` and ``DS-4A``, which this prose is full of.
_NUMBER = rf"(?<![-\w.])(?<!\d\.)(?:\d[\d_]*|{_alternation(_WORD_VALUES)})\b"

# A full stop followed by a LETTER ends a sentence; followed by anything else it is an
# abbreviation ("7 (resp. 9) rows", where BOTH numbers are real claims).
#
# The class says "any letter" because that is what the rule has always done: both patterns compile
# with re.IGNORECASE, which made the older ``[A-Z]`` spelling match lower case too. It read as a
# capital-letter rule and was not one.
#
# ⛔ DO NOT "fix" this by requiring a capital. Counting a boundary as ``[.;:]`` + whitespace + a
# letter, inside the comment runs and docstring paragraphs this module reads across the five watched
# files, JUST OVER HALF of the boundaries are followed by a lower-case word (an identifier, a quoted
# term, a continued clause). A capital-only rule stops recognising that half, which is the
# mechanism that re-introduces the cross-sentence pairing this look-ahead exists to prevent, the
# defect the module docstring records.
#
# ⚠️ HONESTLY SCOPED, because the sentence above is about a MECHANISM and not about an effect: on
# today's watched prose a truly case-sensitive class is INERT. Measured all three ways, any
# letter, literal ``[A-Z]`` under ``re.IGNORECASE``, and ``[A-Z]`` forced case-sensitive with
# ``(?-i:[A-Z])``, and all three find an IDENTICAL set of sites and keys. So nothing goes red if
# someone makes the change; the risk is prospective, and the decision is held by
# ``tests/test_prose_numbers.py`` on constructed input, which is the only place the difference
# exists. Swapping ``[A-Z]`` for the honest class was verified the same way and also changes
# nothing.
#
# ⛔ MAKING THE CLASS CASE-SENSITIVE IS THE FORBIDDEN REPAIR, and the share is what carries that.
# Counting a boundary as ``[.;:]`` + whitespace + a letter, inside the comment runs and docstring
# paragraphs this module actually reads across the watched files, JUST OVER HALF are followed by a
# lower-case word: an identifier, a quoted term, a continued clause. A capital-only rule stops
# recognising those and re-introduces the cross-sentence pairing the look-ahead exists to prevent.
# Re-derive the share over the blocks :func:`iter_blocks` returns, and count the LETTER rather than
# any non-space: a terminator plus whitespace plus any non-space is a different, larger denominator.
#
# ⚠️ A BARE ``[A-Z]`` CANNOT EVEN ASK THE QUESTION: under ``re.IGNORECASE`` it matches lower case
# too, so the only honest mutant, and the only red-proof for a test over this rule, is the SCOPED
# ``(?-i:[A-Z])``. Measured: it pairs "we counted 7. both tools agree" across the stop, which the
# shipped class refuses.
#
# The SHARE, not a pair of absolutes, is what carries the decision. An absolute pair lived here
# twice and rotted twice, because nothing re-runs it: this module is deliberately unwatched. And
# count the LETTER, not any non-space: a terminator plus whitespace plus ANY non-space is a
# different, larger denominator, which is how the retired pair silently mixed two counting rules.
_BREAK = r"(?![.;:]\s+[^\W\d_])"

# A number, at most two intervening words, then one of the closed nouns.
#
# Two properties are load-bearing and were each bought with a measured defect:
#   * The noun alternation is INSIDE the pattern, so backtracking finds a gap size that works. A
#     post-filter would drop the match AND consume the text (see the module docstring).
#   * The sentence break is a look-ahead INSIDE the gap, not a filter on the finished match. As a
#     filter it had the same defect one layer up: "the 7 rows. Both tools agree" matched from the
#     7 through to "tools", was discarded whole, and the valid "7 rows" inside it was never tried.
#     The look-ahead alone was not enough either: a gap word ending in "." swallowed the very
#     terminator the look-ahead tests for, so the break never fired and the cross-sentence pairing
#     was ACCEPTED where the old filter had at least rejected it. The gap word therefore may not
#     end in a terminator, which is what makes backtracking try the shorter, correct pairing.
# The gap classes are DISJOINT (separators are whitespace-or-punctuation-then-whitespace, words are
# non-space): overlapping classes made the partition ambiguous, and a run of dashes, this estate
# rules its sections with 75 of them, cost minutes of backtracking.
_GAP = rf"(?:{_BREAK}[^\w\s]*\s+[^\s]*[^\s.;:]){{0,2}}{_BREAK}[^\w\s]*\s+"
_PAIRED = re.compile(rf"{_NUMBER}{_GAP}(?:{_alternation(COLLECTION_NOUNS)})\b", re.IGNORECASE)

# "TEN of the twelve", "13 of the 20", "Five of the 16", the noun is elided, so _PAIRED is blind
# to these. They are among the most drift-prone claims here, so they get their own shape.
_OF_THE = re.compile(rf"{_NUMBER}\s+of\s+(?:the|those|these)\s+{_NUMBER}", re.IGNORECASE)

_NUMBER_TOKEN = re.compile(_NUMBER, re.IGNORECASE)


@dataclass(frozen=True)
class ProseBlock:
    """One comment run or docstring paragraph, flattened to a single line."""

    path: str
    lineno: int
    qualname: str
    text: str

    def where(self) -> str:
        """``path:line`` plus the enclosing scope, what a failure message should print."""
        return f"{self.path}:{self.lineno} [{self.qualname}]"


@dataclass(frozen=True)
class ProseSite:
    """One number inside a phrase that names a size, addressable inside its block."""

    block: ProseBlock
    offset: int
    value: int
    snippet: str

    @property
    def key(self) -> tuple[str, str, str, int]:
        """The stable identity: file, enclosing scope, phrase, value. Survives edits above it."""
        return (self.block.path, self.block.qualname, self.snippet, self.value)


def parse_count(token: str) -> int | None:
    """``"22"``/``"200_000"``/``"TEN"`` to an int; ``None`` for anything outside the closed list."""
    cleaned = token.replace("_", "")
    if cleaned.isdigit():
        return int(cleaned)
    return _WORD_VALUES.get(token.lower())


def _scope_spans(tree: ast.Module) -> list[tuple[int, int, str]]:
    """``(first line, last line, qualname)`` for every class and function, widest first."""
    spans: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                end = child.end_lineno if child.end_lineno is not None else child.lineno
                spans.append((child.lineno, end, qualname))
                walk(child, qualname)
            else:
                walk(child, prefix)

    walk(tree, "")
    # Widest first, so a scan that keeps the LAST hit ends on the innermost enclosing scope.
    spans.sort(key=lambda span: span[1] - span[0], reverse=True)
    return spans


def _qualname_at(spans: Sequence[tuple[int, int, str]], lineno: int) -> str:
    """The innermost class/function containing *lineno*, or ``<module>``."""
    found = "<module>"
    for start, end, qualname in spans:
        if start <= lineno <= end:
            found = qualname
    return found


def _paragraphs(text: str, first_lineno: int) -> list[tuple[int, str]]:
    """Split on blank lines; each paragraph becomes one line, keeping its own start line."""
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = first_lineno
    for offset, line in enumerate(text.splitlines()):
        if line.strip():
            if not buffer:
                start = first_lineno + offset
            buffer.append(line.strip())
        elif buffer:
            out.append((start, " ".join(buffer)))
            buffer = []
    if buffer:
        out.append((start, " ".join(buffer)))
    return out


def _comment_blocks(
    path: str, source: str, spans: Sequence[tuple[int, int, str]]
) -> list[ProseBlock]:
    """Runs of own-line comments at one indentation, joined into one paragraph each.

    A bare ``#`` ends the run: it is the comment spelling of a blank line. Without that the
    "paragraph, not whole-block" rule would hold for docstrings and quietly not for comments:
    measured, that fused fifteen author paragraphs into single blocks, one of them 1200 characters
    long, and produced a pairing that reached across ``) install ->`` into an unrelated noun.
    """
    out: list[ProseBlock] = []
    run: list[str] = []
    run_start = 0
    previous_row = -2
    previous_col = -1

    def flush() -> None:
        if run:
            out.append(ProseBlock(path, run_start, _qualname_at(spans, run_start), " ".join(run)))

    # io.StringIO, not splitlines(): str.splitlines also breaks on form feed and friends, which the
    # tokenizer does not, so one page separator would desync every row below it, and rows feed the
    # qualname lookup, i.e. exactly the key this module promises stays stable.
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        row, col = token.start
        text = token.string.lstrip("#").strip()
        if not text:
            flush()
            run = []
            previous_row, previous_col = row, col
            continue
        if run and row == previous_row + 1 and col == previous_col:
            run.append(text)
        else:
            flush()
            run = [text]
            run_start = row
        previous_row, previous_col = row, col
    flush()
    return out


def _docstring_blocks(
    path: str, tree: ast.Module, spans: Sequence[tuple[int, int, str]]
) -> list[ProseBlock]:
    """Every docstring, paragraph by paragraph, with the file line each paragraph starts on."""
    out: list[ProseBlock] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        literal = node.body[0].value
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            continue
        qualname = "<module>" if isinstance(node, ast.Module) else _qualname_at(spans, node.lineno)
        for lineno, paragraph in _paragraphs(literal.value, literal.lineno):
            out.append(ProseBlock(path, lineno, qualname, paragraph))
    return out


# A markdown line that opens or closes a fenced code block. Backtick and tilde fences both, and
# the closing fence of a run is whichever character opened it, which is why the opener is kept.
_FENCE = re.compile(r"^\s*(```+|~~~+)")

# An ATX heading. Setext headings (an ``===``/``---`` underline) are deliberately NOT recognised:
# measured over the tracked markdown of this repository there is not one, and a rule with no
# subject is a rule nobody can test.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# A thematic break, or the underline a setext heading would use. Either way it carries no prose and
# must not glue the paragraphs on both sides of it together.
_RULE = re.compile(r"^\s*([-*_=])(?:\s*\1){2,}\s*$")

# A list item's marker. A new marker STARTS a block, an indented continuation line joins the one
# above it. Without this every bullet of a list fuses into a single paragraph and a pairing reaches
# across an item boundary, which is the defect the "paragraph, not whole-block" rule exists to
# prevent and which was measured on this very guide: "the other three display tools take a
# context_cap only" fused with the bullet below it.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _markdown_headings(source: str) -> list[tuple[int, str, str]]:
    """``(line, own title, full chain)`` for every ATX heading, outermost heading first.

    Exposed so a caller can assert the precondition :func:`_markdown_blocks` rests on, rather than
    assume it: see :func:`ambiguous_headings`.
    """
    out: list[tuple[int, str, str]] = []
    chain: list[tuple[int, str]] = []
    fence: str | None = None
    for lineno, line in enumerate(source.splitlines(), start=1):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        heading = _HEADING.match(line)
        if not heading:
            continue
        level = len(heading.group(1))
        title = heading.group(2).strip()
        chain = [entry for entry in chain if entry[0] < level]
        chain.append((level, title))
        out.append((lineno, title, " > ".join(name for _level, name in chain)))
    return out


def ambiguous_headings(source: str | Path) -> dict[str, list[str]]:
    """Heading titles that occur under MORE THAN ONE chain, with the chains that share them.

    Takes text OR a path, and the path half is not a convenience. The consumer's own precondition
    test asserts that ``_parsed`` is the only place ``tests/test_prose_counters.py`` reads a file,
    so that the provenance tracer cannot go blind; a check that opened a markdown file over there
    would break it. Reading here keeps the file access in the module that already does all of it,
    which is the same scope that rule has always had, :func:`iter_blocks` reads files too.

    :func:`_markdown_blocks` keys a block by its NEAREST heading rather than by the whole chain,
    which is a readability decision with a soundness condition attached: two sections may not share
    a title, or one section's frozen row could be satisfied by the other's phrase. The decision was
    made on two measurements, both taken 2026-08-21 with this reader. Shortening cost almost nothing
    in stability: over the shipped guide's 82 commits since 2026-07-01 the full chain moved on 14
    and the nearest heading on 13, so the extra ancestor only ever added brittleness. And it bought
    a great deal in readability: that file's longest chain ran to 187 characters against 98 for its
    longest single heading, and those keys are typed into an inventory a human has to read.

    The condition is NOT assumed to hold. This function is what a consumer asserts it with, the way
    the provenance tracer asserts its own precondition instead of commenting on it. Measured over
    this repository's tracked markdown, the only file with ambiguous headings is ``CHANGELOG.md``,
    whose release sections legitimately repeat, and which is out of the watch list for its own
    reasons anyway.
    """
    text = source.read_text(encoding="utf-8-sig") if isinstance(source, Path) else source
    chains: dict[str, set[str]] = {}
    for _lineno, title, chain in _markdown_headings(text):
        chains.setdefault(title, set()).add(chain)
    return {title: sorted(seen) for title, seen in chains.items() if len(seen) > 1}


def _markdown_blocks(path: str, source: str) -> list[ProseBlock]:
    """Every markdown paragraph, keyed by the NEAREST HEADING it sits under.

    The key has to be as stable as the Python side's qualname, and for the same reason: a table
    keyed on line numbers rots on any edit above it, for a reason that is not a change in the
    finding. A heading moves only when the document is restructured, which is exactly when a
    reader SHOULD look again. Why the nearest heading and not the whole chain, with the two
    measurements that decided it, is written at :func:`ambiguous_headings`, together with the
    soundness condition that choice carries.

    Five line kinds end a block and each was chosen against a measured alternative:

    * a blank line, the paragraph rule the docstring side already uses;
    * a THEMATIC BREAK, which is the same rule for a line of dashes: this estate rules its sections
      with them, and gluing the paragraphs on both sides together fuses two unrelated ones;
    * a HEADING, which additionally becomes a block of its own. It has to: this repository's
      shipped guide states a size inside one, ``### Olog write posture (all four write tools)``,
      and a reader that treated headings as structure alone would be blind to it;
    * a LIST MARKER, see ``_LIST_ITEM``;
    * a TABLE ROW, which is a record rather than a sentence. Each row stands alone, so a number in
      one cell cannot pair with a noun in a neighbouring row. It can still pair ACROSS CELLS of the
      same row, and that limit is stated rather than closed, because closing it needs a cell
      splitter and no measured case asks for one.

    Fenced code is skipped whole. A fence carries identifiers and version pins, not prose, and the
    detector's own lookbehind was written for the same distinction.
    """
    out: list[ProseBlock] = []
    run: list[str] = []
    run_start = 0
    heading_title = "<document>"
    fence: str | None = None

    def flush() -> None:
        nonlocal run
        if run:
            out.append(ProseBlock(path, run_start, heading_title, " ".join(run)))
            run = []

    # splitlines() rather than the tokenizer's reader: there is no tokenizer here, and a form feed
    # inside markdown is prose to this reader like any other whitespace.
    for lineno, line in enumerate(source.splitlines(), start=1):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            continue
        if opener:
            flush()
            fence = opener.group(1)
            continue
        stripped = line.strip()
        if not stripped or _RULE.match(line):
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            heading_title = heading.group(2).strip()
            out.append(ProseBlock(path, lineno, heading_title, heading_title))
            continue
        if stripped.startswith("|") or _LIST_ITEM.match(line):
            flush()
            run_start = lineno
        elif not run:
            run_start = lineno
        run.append(stripped)
        if stripped.startswith("|"):
            flush()
    flush()
    return out


def iter_blocks(targets: Iterable[tuple[str, Path]]) -> tuple[ProseBlock, ...]:
    """Every comment run, docstring paragraph and markdown paragraph in *targets*, in order.

    *targets* pairs the label a failure message should print with the file to read, so the caller
    decides how a path is spelled and this module never depends on where the repository sits.

    The reader is chosen by SUFFIX, and that is the whole of the markdown extension: the patterns,
    the vocabulary and the site keying are shared, so a number in the shipped guide is read by the
    same rule as a number in a docstring. Anything that is not ``.md`` is parsed as Python, which
    keeps the default the one every existing caller already relies on.
    """
    blocks: list[ProseBlock] = []
    for label, path in targets:
        source = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".md":
            blocks.extend(_markdown_blocks(label, source))
            continue
        tree = ast.parse(source)
        spans = _scope_spans(tree)
        blocks.extend(_comment_blocks(label, source, spans))
        blocks.extend(_docstring_blocks(label, tree, spans))
    return tuple(sorted(blocks, key=lambda block: (block.path, block.lineno, block.text)))


def _spans_in(block: ProseBlock) -> list[tuple[int, int]]:
    """Character spans of every size-naming phrase in *block*.

    Sentence breaks are excluded by the patterns themselves, never here: a filter applied to a
    finished match discards the text along with the match, so a valid short pairing inside a long
    invalid one is lost. That is the same defect the noun alternation was moved inline to avoid.
    """
    return [
        match.span() for pattern in (_PAIRED, _OF_THE) for match in pattern.finditer(block.text)
    ]


def sites_of(block: ProseBlock) -> tuple[ProseSite, ...]:
    """Every number inside a size-naming phrase of *block*.

    One site per NUMBER, not per phrase: "7 (resp. 9) rows" states two sizes of two different
    sets, and a guard that only saw the 7 would leave the 9 exactly as unwatched as before.
    """
    found: dict[int, ProseSite] = {}
    for start, end in _spans_in(block):
        snippet = " ".join(block.text[start:end].split()).lower()
        for token in _NUMBER_TOKEN.finditer(block.text, start, end):
            value = parse_count(token.group(0))
            if value is not None:
                found.setdefault(token.start(), ProseSite(block, token.start(), value, snippet))
    return tuple(found[offset] for offset in sorted(found))


def iter_sites(blocks: Iterable[ProseBlock]) -> tuple[ProseSite, ...]:
    """Every size-naming number across *blocks*, in the order the blocks were given."""
    return tuple(site for block in blocks for site in sites_of(block))
