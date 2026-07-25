"""Read the NUMBERS that comments and docstrings claim, so a test can compare them to the sets.

S32. Prose here routinely names the size of a set — "all 22 schemas", "7 (resp. 9) rows",
"TEN of the twelve". Nothing checked those, and every typing step re-typed them by hand: in the
S29 11th target 18 counters were pulled over, two QA rounds found 8 of them still wrong, and one
had been wrong *before* that build started. This module is the reading half of the guard; the
comparing half is ``tests/test_prose_counters.py``.

Four decisions are load-bearing, and each was forced by a measurement rather than chosen:

* **Prose is read per PARAGRAPH, not per line.** A claim straddles the 100-column wrap often
  enough that a line-wise regex is not merely lossy but silently lossy — ``test_server.py`` splits
  ``EXACTLY the 11`` / ``mapped fields`` across a docstring wrap and ``ArchiverHistoryResult's 11``
  / ``fields`` across a comment wrap, i.e. both halves of the same claim, 40 lines apart. Runs of
  own-line comments are joined the same way as docstring paragraphs.
* **Paragraph, not whole-block.** Flattening a whole docstring lets a match jump a sentence
  boundary: ``…lives in one module.`` + ``The functions resolve…`` reads as "one module. The
  functions" and would be counted as a claim about functions.
* **The collection noun is part of the PATTERN, never a filter applied afterwards.** The first
  version matched "a number, a gap, then any word" and dropped the hit when the word was not a
  collection noun. Measured, that lost 7 of the estate's most drift-prone claims: the gap is
  greedy, so "all 22 schemas finds the same enum" matched through to "the", was rejected, and
  ``finditer`` then resumed *past* the phrase — the real pairing was never tried. A rejected match
  is not a neutral event; it consumes the text.
* **A site is keyed by its enclosing QUALNAME, never by line number.** Line numbers move with any
  edit above them, so a table keyed on them rots for a reason that is not a change in the finding —
  the lesson ``tests/test_client_edge_guards.py`` records for its own ``_UNOBSERVED`` table.

Honest scope, because the numbers invite over-reading: this finds a number PAIRED with one of a
closed list of collection nouns, or the ``N of the M`` shape where the noun is elided. It does not
find every statement about a size. "Three independent red directions" names a size and is invisible
here — by design: those are claims about test bodies, which no constant can settle. The consumer
states the closed list in its own docstring rather than implying coverage.
"""

from __future__ import annotations

import ast
import re
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Number words the prose actually uses, plus headroom. Deliberately a closed list: a generic
# word-to-int parser would happily read "one" out of "one of the two halves", where the article is
# not a count. Measured, the prose reaches "THIRTEEN"; the list runs to twenty for headroom.
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

# The collection nouns a number may be paired with. Closed on purpose — see the module docstring.
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
    """Longest-first, so ``ten`` cannot win over ``tenth``-like longer members."""
    return "|".join(sorted(words, key=len, reverse=True))


_NUMBER = rf"(?:\d[\d_]*|{_alternation(_WORD_VALUES)})"

# A number, at most two intervening words, then one of the closed nouns. The noun alternation is
# INSIDE the pattern (see the module docstring) so backtracking can find a gap size that works.
_PAIRED = re.compile(
    rf"(?<![-\w])(?:{_NUMBER})\b(?:\W+[\w|/\[\]'-]+){{0,2}}\W+(?:{_alternation(COLLECTION_NOUNS)})\b",
    re.IGNORECASE,
)

# "TEN of the twelve", "13 of the 20", "Five of the 16" — the noun is elided, so _PAIRED is blind
# to these. They are among the most drift-prone claims here, so they get their own shape.
_OF_THE = re.compile(
    rf"(?<![-\w])(?:{_NUMBER})\s+of\s+(?:the|those|these)\s+(?:{_NUMBER})\b", re.IGNORECASE
)

_NUMBER_TOKEN = re.compile(rf"(?<![-\w]){_NUMBER}\b", re.IGNORECASE)

# A full stop followed by a capital is a sentence boundary; a full stop followed by anything else
# is an abbreviation ("7 (resp. 9) rows", where BOTH numbers are real claims). Measured: without
# this distinction either the abbreviation is lost or "one module. The functions" is invented.
_SENTENCE_BREAK = re.compile(r"[.;:]\s+[A-Z]")


@dataclass(frozen=True)
class ProseBlock:
    """One comment run or docstring paragraph, flattened to a single line."""

    path: str
    lineno: int
    qualname: str
    text: str

    def where(self) -> str:
        """``path:line`` plus the enclosing scope — what a failure message should print."""
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
    """Runs of own-line comments at one indentation, joined into one paragraph each."""
    out: list[ProseBlock] = []
    run: list[str] = []
    run_start = 0
    previous_row = -2
    previous_col = -1

    def flush() -> None:
        if run:
            out.append(ProseBlock(path, run_start, _qualname_at(spans, run_start), " ".join(run)))

    lines = iter(source.splitlines(keepends=True))
    for token in tokenize.generate_tokens(lambda: next(lines, "")):
        if token.type != tokenize.COMMENT:
            continue
        row, col = token.start
        text = token.string.lstrip("#").strip()
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


def iter_blocks(targets: Iterable[tuple[str, Path]]) -> tuple[ProseBlock, ...]:
    """Every comment run and docstring paragraph in *targets*, in a stable order.

    *targets* pairs the label a failure message should print with the file to read, so the caller
    decides how a path is spelled and this module never depends on where the repository sits.
    """
    blocks: list[ProseBlock] = []
    for label, path in targets:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        spans = _scope_spans(tree)
        blocks.extend(_comment_blocks(label, source, spans))
        blocks.extend(_docstring_blocks(label, tree, spans))
    return tuple(sorted(blocks, key=lambda block: (block.path, block.lineno, block.text)))


def _spans_in(block: ProseBlock) -> list[tuple[int, int]]:
    """Character spans of every size-naming phrase in *block*, sentence breaks excluded."""
    spans: list[tuple[int, int]] = []
    for pattern in (_PAIRED, _OF_THE):
        for match in pattern.finditer(block.text):
            if _SENTENCE_BREAK.search(match.group(0)):
                continue
            spans.append(match.span())
    return spans


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
