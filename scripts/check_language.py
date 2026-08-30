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
     -heit, -schaft, -lich, -ieren, -isch, -los and their inflections). Every candidate suffix was
     probed against the whole English tree before being admitted, because a suffix that fires on
     English is worse than no suffix at all. This signal earned its place immediately: it found
     "ehrlichen" in a comment the other two signals and the original sweep had all walked past.

     The ``los`` member arrived later, and from a sibling repository, where a measurement earned
     it: the German ``wirkungslos`` sat in an otherwise English doc comment and NO signal caught
     it, because it carries no umlaut, no function word, and ends in ``los`` rather than in
     ``ung``. Probed against THIS tree before being admitted, exactly as the paragraph above
     demands: zero hits over every tracked file, with AND without the stoplist below. So here it
     is preventive rather than a repair, and it is recorded as preventive instead of being sold
     as a fix for something. The three-word-character floor already covers the short English
     plurals ``kilos``/``silos``/``solos``/``halos`` (two characters ahead of the suffix, so the
     floor rejects them); ``logos`` does not end in ``los`` at all and never was at risk. The
     English words that DO collide are enumerated below.

An ALL-CAPS match is not a hit. Without that rule the guard reports the licence name, which is
spelled like the German function word ``mit``, on every line that carries it. German prose does not
write its function words in capitals, so the rule costs nothing and removes the only false-positive
class the measurement turned up.

⚠️ No COUNT stands here any more, and the site acronym is no longer claimed as a second class.
Both were re-measured on 2026-08-29 and neither held. The licence figure had drifted, which is what
a number nothing re-runs does; and the acronym cannot be reported at all, because ``bis`` is not in
the word list above, so that false positive has no mechanism with or without this rule. Re-derive
the first rather than trusting a figure here: run ``german_signals`` over the tracked tree with the
``isupper`` discard removed.

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
    ieren iert ierte ierten ierung ierungen isch ische ischen ischer los
""".split()  # noqa: SIM905 (prose block, see above)

# English words the suffix rule would otherwise flag (QA-5). The tree probe above answers only
# "does it collide TODAY"; this list answers "which English words CAN collide": enumerated per
# suffix over English vocabulary and then verified against ``_SUFFIX_RE`` one by one (a guard test
# pins that every entry still matches, so a stale entry goes red instead of silently widening).
# The 'ung' family needs three word characters before the suffix, which is why 'flung'/'swung'
# pass on their own; 'fahrenheit' is 'fahren' + 'heit' and the likeliest hit in this domain.
# The 'los' family is the second block below: the same floor already rejects the short plurals
# ('kilos', 'silos', 'solos', 'halos' each carry only two characters ahead of the suffix), so what
# remains is loanwords and proper nouns with a longer stem. None of them occurs in this tree today,
# measured; they are listed because this list answers "which English words CAN collide", not
# "which do right now".
_ENGLISH_SUFFIX_LOOKALIKES = frozenset(
    """
    fahrenheit hamstrung overstrung restrung sprung strung underslung unstrung unsung
    amarillos armadillos buffalos carlos diabolos gigolos marcelos pianolos piccolos
    pomelos pueblos tremolos
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
#
# Keyed on the RESOLVED PATH of each of those three files, not on its bare name. A name key exempts
# every file that happens to share the name: a second ``test_language_guard.py`` anywhere in the
# tree would have been entirely free of this guard, and nothing would have said so. Resolving both
# sides also makes the key indifferent to how the path arrives, relative from pre-commit or
# absolute from a manual run, which a repo-relative string comparison would not be.
_SELF_EXEMPT: frozenset[Path] = frozenset(
    {
        Path(__file__).resolve(),
        _EXCEPTIONS_FILE.resolve(),
        (Path(__file__).resolve().parents[1] / "tests" / "test_language_guard.py"),
    }
)


class Unreadable(Exception):
    """A path this guard could not judge, RAISED rather than returned as an empty hit list.

    An empty list means "no German found" to every caller of :func:`scan`, so a path the guard
    never opened reads exactly like a clean file. Inside the hook that costs nothing, because
    pre-commit hands over paths it has just staged and they are readable by construction. Run BY
    HAND over a tree, this guard is the acceptance net of a translation tranche, and there a
    silent skip lowers the reported number with nothing said about it.

    Raised rather than reported as one more finding, and that is the load-bearing half: several
    callers use :func:`scan` directly and take the LENGTH of what comes back. A finding would be
    counted as one more German line; an exception cannot be mistaken for anything.
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}:0: {reason}")
        self.path = path
        self.reason = reason


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


def path_matches(path: str, suffix: str) -> bool:
    """True when *path* ends with *suffix* at a PATH-COMPONENT boundary (QA-11).

    A bare ``endswith`` compares characters, not path components, so the key
    ``scripts/guard_audit.py`` also matched ``myscripts/guard_audit.py``: an exception granted to
    one file silently covered a different one, in a directory nobody had decided about. Anchoring
    on the separator makes the key mean what a reader takes it to mean. The whole-path case is kept
    so a key that spells the complete relative path still matches itself.

    Spelled identically in check_typography.py; ``tests/test_typography_guard.py`` pins the two
    against each other so the pair cannot drift apart unnoticed.
    """
    posix = Path(path).as_posix()
    return posix == suffix or posix.endswith("/" + suffix)


def is_excepted(path: str, line: str, exceptions: list[tuple[str, str]]) -> bool:
    """True when *line* in *path* is covered by an entry: BOTH path suffix and fragment must hit."""
    return any(path_matches(path, suffix) and fragment in line for suffix, fragment in exceptions)


def scan(path: str, exceptions: list[tuple[str, str]]) -> list[str]:
    """Return ``file:line: signals`` for every line of *path* that reads as German.

    Raises :exc:`Unreadable` when the file cannot be judged at all, rather than returning nothing.
    The two failures are kept apart because they say different things, and only one of them is
    about this guard's own population: a decode failure is NOT proof of a binary, and its ordinary
    shape on Windows is a cp1252-encoded GERMAN file, which is exactly what this guard exists for.
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except UnicodeError as problem:
        raise Unreadable(
            path, "not valid UTF-8, so it was NOT checked (a cp1252 German file lands here too)"
        ) from problem
    except OSError as problem:
        raise Unreadable(
            path, f"could not be read, so it was NOT checked ({problem.strerror})"
        ) from problem
    hits: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        signals = german_signals(line)
        if signals and not is_excepted(path, line, exceptions):
            hits.append(f"{path}:{lineno}: {', '.join(signals)}")
    return hits


def main(argv: list[str]) -> int:
    """Scan the staged files in *argv*; return 1 (block the commit) on any German line.

    THREE exit codes rather than two, and the third one is the point of the whole branch. 0 and 1
    keep their meaning exactly. 2 says the run was not a MEASUREMENT at all: either no paths
    arrived, or a path could not be read. Whoever counts this output has to be able to tell that
    apart from a clean tree, because an empty report and an unread tree look identical otherwise.

    The two reports are kept on separate lines for the same reason. A German finding is indented
    by two spaces, an unread path by an exclamation mark, so a count of the German findings does
    not silently gain the paths nobody managed to read.
    """
    if len(argv) < 2:
        sys.stderr.write(
            "check_language.py: no paths were given, so NOTHING was checked. An empty run is not "
            "a clean run, and returning 0 here would say the opposite. Pass the files to scan, "
            "for example the output of git ls-files.\n"
        )
        return 2
    exceptions = load_exceptions()
    all_hits: list[str] = []
    not_judged: list[str] = []
    attempted = 0
    for path in argv[1:]:
        if Path(path).resolve() in _SELF_EXEMPT:
            continue
        attempted += 1
        try:
            all_hits.extend(scan(path, exceptions))
        except Unreadable as problem:
            not_judged.append(str(problem))
    if all_hits:
        sys.stderr.write(
            "Blocked: this repository is written in English.\n"
            "Translate the line (do not delete the reasoning), or, if the German is the POINT "
            f"of the line, add a justified exception to {_EXCEPTIONS_FILE.name}:\n"
        )
        for hit in all_hits:
            sys.stderr.write(f"  {hit}\n")
    if attempted == 0:
        # Der letzte Schlitz derselben Klasse, und er ist im Haken erreichbar: ein Commit, der nur
        # den Waechter und seinen Test anfasst, uebergibt ausschliesslich selbstausgenommene Pfade.
        # Der Code bleibt 0, denn das Ueberspringen ist gewollt und kein Fehlschlag; still bleibt
        # es trotzdem nicht, weil ein Aufrufer sonst "nichts gefunden" liest, wo "nichts gelesen"
        # steht.
        sys.stderr.write(
            f"NOTHING TO CHECK: all {len(argv) - 1} given paths are self-exempt, so this run read "
            "no file at all. That is the absence of a result, not a clean one. The exit code "
            "stays 0 because skipping these paths is intended, not a failure.\n"
        )
        return 0
    if not_judged:
        sys.stderr.write(
            f"NOT CHECKED: {len(not_judged)} of the {attempted} paths this run really opened "
            f"could not be read, out of {len(argv) - 1} given. This says NOTHING about them; it "
            "is not a finding about their content, it is the absence of one:\n"
        )
        for entry in not_judged:
            sys.stderr.write(f"! {entry}\n")
        return 2
    return 1 if all_hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
