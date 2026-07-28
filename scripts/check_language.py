#!/usr/bin/env python3
"""Pre-commit guard: this repository is written in English.

It was not always. The public fork grew out of a German-speaking project and carried German
comments and docstrings for months; removing them took 155 lines across 22 files, translated
rather than deleted. This guard exists so that is a one-off.

WHY THREE SIGNALS, and why the obvious one alone is a trap. The first sweep searched with
``grep [aeoeueAEOEUEss]`` (the umlauts and the eszett) and declared the job done. It had missed 21
German lines that happen to carry no umlaut, and the miss was invisible: a search that finds
nothing looks exactly like a tree that contains nothing. The three signals are deliberately
independent, each catching what the previous one cannot:

  1. FUNCTION WORDS: a closed list of German words that are not also English words. One hit is
     enough, which is why the list has to be exact. English homographs are excluded BY NAME
     (``was``, ``die``, ``man``, ``war``, ``in``, ``so``, ``also``, ``hat``, ``an``, ``am``,
     ``den``); each of them is a common English word and would fire on ordinary prose.
  2. UMLAUTS: the original search, kept as a cheap backstop for German that uses no function word.
  3. GERMAN MORPHOLOGY: word-formation suffixes that English does not produce (-ung, -keit,
     -heit, -schaft, -lich, -ieren, -isch and their inflections). Every candidate suffix was
     probed against the whole English tree before being admitted, because a suffix that fires on
     English is worse than no suffix at all. This signal earned its place immediately: it found
     "ehrlichen" in a comment the other two signals and the original sweep had all walked past.

An ALL-CAPS match is not a hit. Measured: without that rule the guard reports the MIT licence six
times (the ``mit`` function word) and the site acronym BIS once. German prose does not write its
function words in capitals, so the rule costs nothing and removes the only false-positive class
the measurement turned up.

Scope, stated so nobody over-reads it: this finds German by VOCABULARY and MORPHOLOGY, per line.
It cannot see a German sentence built entirely from words the lists do not know, and it does not
judge whether English prose is good English. See docs/known-limits.md.

Exceptions live in ``scripts/.language-exceptions``, keyed on a literal FRAGMENT of the line
rather than a line number, for the same reason check_typography.py is: a number moves with any
edit above it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# German function words with no English homograph. Deliberately closed and deliberately
# SHORT: one hit blocks a commit, so a wrong entry is expensive, while a missing one is still
# covered by signals 2 and 3.
#
# Held as a text block rather than a list literal (SIM905): a word list is read and edited as
# prose, and one element per line would turn a 90-word list into 90 lines of diff noise.
_FUNCTION_WORDS = """
    aber auch aus beim bleibt damit dann das dass dem denn der des diese diesem diesen dieser
    dieses durch ein eine einem einen einer eines etwas fuer gegen gibt hier ihre ihrer im immer
    ist jede jeder jedes kann kein keine keinen mehr mit nach nicht nichts noch nur ohne oder
    schon sehr seine seiner seit sich sind sondern sonst sowie statt steht und unter vom von vor
    weil werden wieder wenn wie wird wurde wurden zu zum zur zwischen
""".split()  # noqa: SIM905 (prose block, see above)

# Suffixes English does not produce. Each was probed against the entire English tree first; a
# suffix that fires there is not a signal, it is noise, and was dropped rather than special-cased.
_GERMAN_SUFFIXES = """
    ung ungen keit keiten heit heiten schaft schaften lich liche lichen licher liches
    ieren iert ierte ierten ierung ierungen isch ische ischen ischer
""".split()  # noqa: SIM905 (prose block, see above)

# English words the suffix rule would otherwise flag (QA-5). The tree probe above answers only
# "does it collide TODAY"; this list answers "which English words CAN collide": enumerated per
# suffix over English vocabulary and then verified against ``_SUFFIX_RE`` one by one (a guard test
# pins that every entry still matches, so a stale entry goes red instead of silently widening).
# The 'ung' family needs three word characters before the suffix, which is why 'flung'/'swung'
# pass on their own; 'fahrenheit' is 'fahren' + 'heit' and the likeliest hit in this domain.
_ENGLISH_SUFFIX_LOOKALIKES = frozenset(
    """
    fahrenheit hamstrung overstrung restrung sprung strung underslung unstrung unsung
""".split()  # noqa: SIM905 (prose block, see above)
)

# A URL is an address, not prose: ``web.mit.edu`` carries the German preposition ``mit`` as a
# host label and would block the commit of a legitimate citation (QA-5, measured). URL-shaped
# tokens are blanked before any signal runs; the prose AROUND a URL is still judged.
_URL_RE = re.compile(r"\S+://\S+|www\.\S+")

_UMLAUTS = "".join(chr(code) for code in (0xE4, 0xF6, 0xFC, 0xC4, 0xD6, 0xDC, 0xDF))

_WORD_RE = re.compile(
    r"\b(?:" + "|".join(sorted(set(_FUNCTION_WORDS), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(
    r"\b\w{3,}(?:" + "|".join(sorted(set(_GERMAN_SUFFIXES), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_UMLAUT_RE = re.compile(f"[{_UMLAUTS}]")

_EXCEPTIONS_FILE = Path(__file__).with_name(".language-exceptions")

# Three files spell German on purpose and are skipped whole, rather than by fragment: this script
# (the word lists ARE German), the exceptions file (it quotes the fragments it exempts) and the
# guard's test (its probes are German sentences). A fragment key would have to enumerate every
# probe and would rot on the next one.
_SELF_EXEMPT = frozenset({Path(__file__).name, _EXCEPTIONS_FILE.name, "test_language_guard.py"})


def german_signals(line: str) -> list[str]:
    """Return the signals *line* trips, empty when it reads as English.

    An ALL-CAPS match is discarded: ``MIT`` (the licence) and site acronyms are not the German
    ``mit``, and that is the only false-positive class the tree-wide measurement produced.
    Two more discards from the QA's dictionary audit (the tree probe only proves the absence of
    a collision TODAY): URL-shaped tokens are blanked first, and a suffix match on a word in
    ``_ENGLISH_SUFFIX_LOOKALIKES`` is not German morphology.
    """
    prose = _URL_RE.sub(" ", line)
    signals: list[str] = []
    if any(not match.group(0).isupper() for match in _WORD_RE.finditer(prose)):
        signals.append("German function word")
    if _UMLAUT_RE.search(prose):
        signals.append("umlaut or eszett")
    if any(
        not match.group(0).isupper() and match.group(0).lower() not in _ENGLISH_SUFFIX_LOOKALIKES
        for match in _SUFFIX_RE.finditer(prose)
    ):
        signals.append("German word formation")
    return signals


def load_exceptions() -> list[tuple[str, str]]:
    """Return ``(path_suffix, line_fragment)`` pairs; a line matching both is not reported."""
    if not _EXCEPTIONS_FILE.exists():
        return []
    pairs: list[tuple[str, str]] = []
    for raw in _EXCEPTIONS_FILE.read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        path_part, separator, fragment = entry.partition("::")
        if not separator or not fragment:
            sys.stderr.write(f"warning: bad exception line in {_EXCEPTIONS_FILE.name}: {entry!r}\n")
            continue
        pairs.append((path_part.strip(), fragment))
    return pairs


def is_excepted(path: str, line: str, exceptions: list[tuple[str, str]]) -> bool:
    """True when *line* in *path* is covered by an entry: BOTH path suffix and fragment must hit."""
    posix = Path(path).as_posix()
    return any(posix.endswith(suffix) and fragment in line for suffix, fragment in exceptions)


def scan(path: str, exceptions: list[tuple[str, str]]) -> list[str]:
    """Return ``file:line: signals`` for every line of *path* that reads as German."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []  # binary or unreadable, no prose to judge
    hits: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        signals = german_signals(line)
        if signals and not is_excepted(path, line, exceptions):
            hits.append(f"{path}:{lineno}: {', '.join(signals)}")
    return hits


def main(argv: list[str]) -> int:
    """Scan the staged files in *argv*; return 1 (block the commit) on any German line."""
    exceptions = load_exceptions()
    all_hits: list[str] = []
    for path in argv[1:]:
        if Path(path).name in _SELF_EXEMPT:
            continue
        all_hits.extend(scan(path, exceptions))
    if all_hits:
        sys.stderr.write(
            "Blocked: this repository is written in English.\n"
            "Translate the line (do not delete the reasoning), or, if the German is the POINT "
            f"of the line, add a justified exception to {_EXCEPTIONS_FILE.name}:\n"
        )
        for hit in all_hits:
            sys.stderr.write(f"  {hit}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
