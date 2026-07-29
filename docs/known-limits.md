# Known limits: what this repository deliberately does NOT guard

**What an entry IS:** a measured limit of a guard this repository runs, plus the tempting repair that
was probed and REJECTED rather than merely postponed. **What it is NOT:** a work journal, a QA
history, or scheduling. Those live in a maintainer backlog outside this repository, and every entry
here has to read without it. The rule is stated rather than assumed because the sibling case is on
record: `CHANGELOG.md` drifted into a 763-line work journal once, because no rule claimed it. A
bracketed `S` or `QA` mark is therefore provenance and never an instruction; each entry names its own
open work in words.

**Every entry carries its own measurement date**, and every figure in it is a measurement of the
tree on that date, not an invariant: this file is markdown, and markdown is unguarded here (that is
the first entry). Treat a number as "was true when written" and re-measure before building on it.
Entries 1 to 8 were measured 2026-07-26, entries 9 and 10 on 2026-07-28, entry 11 on 2026-07-29 and
entry 13 on 2026-07-30; entry 12 states a property of this page and measures nothing.

One consequence of that is applied here rather than only described: where a figure exists to
establish a SHARE and no guard can re-run it, the share is what this page states, together with the
rule to re-derive it. Of the figures re-measured on 2026-07-30, exactly the two in that class had
drifted (entries 1 and 8); every one that a guard does re-run still agreed, which is the whole of
the argument for stating a share instead.

Why the file exists: the drift guards this repository runs, the prose-counter guard
(`tests/test_prose_counters.py`, S32), the sham-guard audit (`scripts/guard_audit.py`, S33) and the
language and typography hooks (`scripts/check_language.py`, `scripts/check_typography.py`), each
attract an obvious next step that turns out, on measurement, to be the wrong one. Without this page
those measurements live only in commit bodies, which nobody finds again, and the next author pays for
them a second time. Each entry states the limit, what was measured, and why the tempting repair was
rejected rather than merely postponed.

---

## 1 · Markdown prose is unguarded, and a guard over it was rejected

The prose-counter guard reads Python comments and docstrings. It does not read `.md` files.

Measured 2026-07-26 and re-measured 2026-07-30, when it had drifted in both halves. What this entry
states is therefore the shape and not the count: size-naming phrases are spread across the tracked
markdown files (the detector's own vocabulary: a number paired with one of
`prose_numbers.COLLECTION_NOUNS`, or the `N of the M` shape), and **the largest share of them sits in
`CHANGELOG.md` and `CLAUDE.md`**, which is where the exemptions would live: a changelog entry that
said "22 schemas" in a past release must keep saying it. Re-derive it with that vocabulary over
`git ls-files "*.md"`. This page is in its own census and is one of those files, so its own edits
move the answer, which is the second reason not to print one.

Read that share rather than trusting the label, because it is not uniform: the `CHANGELOG.md`
release lines and `CLAUDE.md`'s dated coverage measurement are deliberately historical, but at least
two of them are neither historical nor claims: a numbered-list ordinal the detector paired with a
following noun, and a live epistemic maxim in the evidence-discipline section. Those are detector
reach rather than exemption candidates, which nudges the "roughly twenty" estimate below DOWN and is
itself an argument for the release-checklist route.

⚠️ The figures this entry used to print were first taken from the tree BEFORE the last two commits of
the batch that added this page, and before the page itself existed. They were corrected once, and had
drifted again within four days, measured on a tree where every figure a guard re-runs still agreed.
That is the clearest illustration of the header above, and the reason the rule to re-derive them
outlived them: nothing re-runs a number in a markdown file.

Why no guard: it would need roughly twenty "historical, not derivable" rows, precisely the
construction `tests/test_prose_counters.py` already rejected, in writing, for its own two files ("a
blanket exemption wearing a table's clothes"). The one load-bearing markdown figure that had
demonstrably drifted was the README's test count, and a test cannot count the passes of the run it
is part of.

**Resolved in 0.4.0 by removing the figure rather than guarding it (S41, QA-20).** It went stale
twice in a single day, and the honest reason is that it is not one number: CI runs the standalone
core on Linux, a local checkout with the display engine runs more, and Windows adds path tests, so
any single figure printed in the README is wrong in at least one of those environments. A guard
would have had to ask which environment it was running in, which is green locally and red in CI.
The README now states the shape of the claim and points at the CI badge for the live answer.

The tool count in the README and on `docs/tools.md` went the same way, for a weaker but sufficient
reason: both pages LIST the tools a few lines further down, so the count added nothing a reader
could not see, while being the half nothing re-measures. ⚠️ Do not read that as "the list is
guarded": measured, only the operator guide's inventory block is compared to the registrations
(`test_guide_matches_code`), and on `docs/tools.md` only the RESOURCE URIs are
(`test_readme_resources`). Its tool table is unchecked, which is a second inventory of the same
thing with only one of them guarded, and is recorded as such rather than implied away.

## 2 · A number is guarded only where a sentence states it

Two sentences may state one figure. A pattern over one of them says nothing about the other, because
`test_the_recorded_figures_match_the_prose_that_states_them` uses `re.search`: one pattern per
WORDING, and only the first occurrence of each.

This has bitten three times, and the third was found by the QA of the commit that fixed the second.
A "17" restated as "those 17 keys sit on 28 targets" went unwatched, which is why the red-proof recipe
recorded for exactly that defect ran green again on shipped code. Then four MORE restatements in the
same docstring (the 28 twice, the "eight", and the vocabulary count) turned out to be settable to
absurd values with the whole suite green. All are patterned now, which is why that table looks
repetitive: the rows are not duplicates, they are the other sentences.

What is **still** unguarded, and this is the honest residue rather than a hypothetical: there is no
COMPLETENESS pin over that docstring. A figure added to it next month ships unwatched, exactly as
these four did. Open: that docstring needs a completeness pin over its FINDINGS, the shape
`test_prose_counters` already has, so a figure added to it cannot ship unwatched (S42). Separately,
three sentences in that same file stated a superseded
`PINNED_COVERAGE` pair in the present tense for a day; the repair was to stop repeating a pinned
figure at all, rather than to correct the copies: a second copy of a guarded number is an unguarded
number.

## 3 · The anti-hard-coding meta-guard is a spelling check, one frame deep

`test_no_claim_hard_codes_its_expectation` searches a claim's pattern and its derivation for the
ANSWER, spelled in digits and in words. Measured on 79 claims:

- **51** derivations call a module helper, whose body lies outside the single inspected frame.
- **17** contain arithmetic, so `lambda: 20 + 2` for an expected 22 never spells its answer.
- A pattern can split the literal (`(1[1])` captures an 11 the skeleton search cannot see).

Widening the regex does not close this; a different mechanism does, and `_Claim.reads` (entry 4) is
the first half of it. Deciding whether `reads` is enough, or whether the remaining three holes need
their own construct, is open (S38).

## 4 · `_Claim.reads` proves provenance, not meaning and not dependence

The provenance tracer establishes that a measure TOUCHED the object its claim declares. Three things
it does not establish, all stated in the `_Claim` docstring as well so they cannot be read past:

1. **That the sentence MEANS that object.** A human decides which set a sentence is about; no
   construct replaces that reading.
2. **That the ANSWER depends on what was touched.** A measure can read a constant and ignore it.
3. **The file half is one-directional and, for three claims, nearly free.** `nullable arrays`,
   `nullable arrays (schema-test note)` and `ChannelInfo fields` reach their file through
   `_typed_dict_fields`, which parses the whole package, so "this file was parsed" cannot be told
   apart from "some file under `src` was parsed". Tightening it needs a third recorder over the
   index's keys. The module-attribute half IS checked for equality in both directions.

The stand-in measures the tracer now makes VISIBLE rather than fixed are still open, and bindable
one by one (S36): visible
because each one's declaration says what it actually reads.

## 5 · The origin of a coverage map cannot be verified

`guard_audit.py sham --check --coverage-db <db>` cannot tell a map recorded with
`COVERAGE_CORE=ctrace` from one recorded with the Python 3.12+ default core, which disables a
location after its first observation and so yields a quarter of the context rows. Measured: the
sqlite `tracer` table is **empty in a genuine ctrace map too**, so there is no marker to read.

Consequence, and it is deliberate: the tool's mismatch message is a **disjunction** ("either the code
changed or the map is impoverished; check the core before re-pinning") rather than a check. A guard
asserting "I verified the map" would itself be the sham form this audit exists to find.

## 6 · The sham audit does not see helper- or fixture-installed doubles

The audited population is tests that install a client class double **in their own body**, read from
the syntax tree. A double installed by a helper or a fixture is invisible. Measured over the syntax
tree: **21** calls to `_install_fake` in `tests/test_olog_update.py` alone, in 21 distinct tests,
which is what `scripts/guard_audit.py`'s own note beside the helper has said all along.

⚠️ This entry first said **22**, from `grep -c "_install_fake("`, which counts the `def` line as a
call site. A raw token count standing in for the set the sentence names is the defect this whole
page documents, committed on the page itself; it is the reason the figure above says how it was
measured.

This is a declared blind spot, not an unknown one, and widening the population is a feature rather
than a repair: it forces a fresh `COVERAGE_CORE=ctrace` recording, because the coverage-dependent
pins are stated about the current population. Open: widen the audited population to helper- and
fixture-installed doubles, which forces that re-recording with it (S40).

## 7 · That a human read the candidate list cannot be proven

The sham audit's verdict "these candidates were read and none is a sham guard" rests on a person
reading them. No construct can prove that happened. What IS mechanised since the audit's own repair:
the candidate **members** are frozen, not merely their count, so a new name is named and the reader
is sent back to it rather than silently inheriting the old verdict.

Two smaller decisions of the same kind, recorded here so they do not become work items that never
get ticked: the `unknown` fallback in **`_compare`** is unreachable by construction (everything it
could describe is in `PINNED`), and exit code 2 is shared by two distinct outcomes: an absent
coverage map, and an absent map *together with* a pin mismatch. Both are accepted as they are.

⚠️ This entry named `classify` instead of `_compare` when it was written. `classify` has no such
branch in any revision; the note this entry was transcribed from had it right, and the page
mis-transcribed it on the way in. A page of limits that sends a reader to the wrong function costs exactly what the
limit was meant to save.

## 8 · The prose detector's own reading behaviour is barely pinned

`tests/prose_numbers.py` has no test module of its own. Its sentence-boundary look-ahead is, measured
three times running, without effect on the currently watched prose, so both of its recorded repairs
could be reverted with nothing going red. Open: `prose_numbers.py` needs a test module of its own,
written over SYNTHETIC prose, because the watched tree cannot tell the behaviours apart (S37).

⛔ One specific tempting repair is measured to be **wrong**: making the boundary class case-sensitive.
Counting a boundary as `[.;:]` + whitespace + a letter, inside the comment runs and docstring
paragraphs the detector actually reads across the five watched files, **just over half** are followed
by a lower-case word: an identifier, a quoted term, a continued clause. A capital-only rule stops
recognising those and re-introduces the cross-sentence pairing the look-ahead exists to prevent. The
class in the code now says "any letter", which is what it always did under `re.IGNORECASE`.

⚠️ The **share** is what carries that decision, and the share is all this entry states, because the
absolute pair it used to print could not survive: written first from the previous commit's tree, in
two files, under a header that promises a measurement of the tree at the stated date; corrected; and
drifted again inside four days, while the share moved by around a point. Nothing re-runs it, by
construction: `prose_numbers.py` is deliberately unwatched and "boundaries" is not a
`COLLECTION_NOUNS` member, so no guard can reach such a number. Re-derive the share with the rule
above over the blocks `iter_blocks` returns for the watched files. ⚠️ Count the LETTER, not any
non-space: a terminator plus whitespace plus any non-space is a different, larger denominator, and
the retired pair had silently mixed the two rules.

## 9 · The language guard finds German by vocabulary, not by understanding

**Measured 2026-07-28.** `scripts/check_language.py` decides per line, from three independent
signals: a closed list of German function words with no English homograph, umlauts and the eszett,
and German word-formation suffixes. Run over the pre-translation tree (`db5a83c`) it reports **156**
German lines in **21** files.

What it cannot see, stated because a guard's reach invites over-reading:

- **A German sentence built entirely from words none of the three signals know.** The lists are
  closed by design (one hit blocks a commit, so a wrong entry is expensive), which buys a false-
  positive rate of zero on the current tree at the cost of an unknown false-negative rate. Nothing
  measures the latter, and nothing can without a labelled corpus.
- **Whether English prose is GOOD English.** The guard answers "is this German", never "is this
  well written". Neither does anything else here.
- **Any language other than German and English.** The signals are German-specific.
- **A German word inside an ALL-CAPS run.** Discarded on purpose: the licence name is spelled the
  same as a German preposition, so without that rule the guard reports the MIT licence six times
  and a site acronym once, measured tree-wide. German prose does not capitalise its function words,
  so the trade is cheap, but it is a hole.

And one class it flags although it should not, known and accepted rather than repaired
(QA 2026-07-28, measured with `german_signals` on each example): **an English proper noun that is
spelled like a German function word.** "the von Neumann architecture", "the Weil pairing" and
"Des Moines" each trip the word signal, because the ALL-CAPS discard does not cover Title-Case.
The mechanical repair, discarding a lowercase `von` before a capitalised word, would blind the
guard to most genuine German (German nouns ARE capitalised), so the residual stays and the
per-line exception file is the intended relief; this very paragraph needed those exceptions, which
doubles as the end-to-end proof of the mechanism. Two sibling classes from the same audit ARE
repaired mechanically, because their fix costs no German recall: URL-shaped tokens are blanked
before any signal (`https://web.mit.edu` carries the German preposition as a host label; a BARE
hostname without a scheme or `www.` prefix is not URL-shaped and stays a residual), and nine
English words ending in a watched suffix (`unsung`, `sprung`, `strung`, `hamstrung`,
`Fahrenheit`, ...) sit in an explicit lookalike stoplist whose entries are pinned to still match
the suffix rule.

⛔ The tempting repair, a general language-detection library, is rejected rather than postponed: a
statistical detector on a 100-column line of technical English with identifiers in it is noisy in
exactly the direction that makes a commit hook unusable, and the failure would be a false BLOCK,
which is worse here than a false pass. The honest position is that this guard makes the 2026-07
sweep hard to undo, not that it proves the repository is English.

Why this entry is not the sweep's own history: the original umlaut grep declared the tree English
and had missed **87 of those 156 lines**, which carry no umlaut at all. That is the measurement the
three-signal design exists to answer, and it is the reason a single-signal guard is not enough.

## 10 · The typography guard forbids a named set, not typography in general

**Measured 2026-07-28.** `scripts/check_typography.py` blocks nine things: the em dash, the en dash,
the ellipsis, four curly quotes and the doubled hyphen. Against `db5a83c` it reports **3056** lines.

It does **not** know any other typographic character (a minus sign, a non-breaking hyphen, a
figure dash), and it deliberately tolerates the typographic apostrophe, the arrow and the
multiplication sign, which carry meaning no ASCII substitute holds. An exception frees the whole
LINE rather than one character on it; with one exception in the file, a narrower key would be more
machinery than the problem deserves.

⚠️ Worth knowing before assuming ruff covers any of this: of the nine characters `pyproject.toml`
once listed in `allowed-confusables`, ruff recognises **five** (en dash, apostrophe, multiplication
sign, and both single curly quotes U+2018/U+201A), probed one at a time through
`ruff --isolated --select RUF001,RUF002,RUF003` and re-measured 2026-07-28 after the first
write-up undercounted them as three. The em dash and the three double curly quotes produce no
finding in a string, a docstring or a comment alike (the ellipsis, never on that list, is equally
silent), so that list carried four entries that never did anything. Ruff now guards the en dash
and the two single quotes inside `.py` as a second, independent pair of eyes; everything else is
this guard's job.

## 11 · The Naming-Service client's provenance is a measurement, not an opinion

Measured 2026-07-29. `services/naming_client.py` and `services/naming_exceptions.py` used to
describe themselves as "vendored" from pvValidator, an unrelated validation tool that is
**GPL-3.0-only** while this package is MIT. Read on its own, that self-description states a licence
problem. It was wrong, and the correction is recorded here because a future reader will otherwise
have to re-derive it from scratch, or worse, act on the wording.

What was measured, on the upstream checkout and on this tree:

- **No contiguous block of code is shared.** Longest identical run of lines, `difflib` over the
  whole files: `naming_client.py` **four lines, two of them non-blank**, and those two are
  `import requests` and `from urllib.parse import quote as url_quote`. `naming_exceptions.py`
  **two lines, one non-blank**, and that one is `"""`.
- The remaining shared lines are class and method signatures the REST endpoint dictates
  (`class NamingServiceClient:`, `def names_url(self) -> str:`), which is API shape, not code.
- The exceptions module is not "slimmed from" anything: upstream derives its errors from its own
  root, ours derive from `services/rest_exceptions.py` like the other three REST planes, and
  `NamingServiceNotFound` exists only here (added for S13).

The honest limit: this measurement compares two files at one point in time. It says nothing about
what a court would say, and it is not a licence review. It is enough to establish that the modules
were mis-described, which is what the wording now reflects: they follow the API shape of that
client and carry none of its code. Attribution lives in the README credits, where this repository
already records origin.

## 12 · Prose rules rot, and that includes this page

`CLAUDE.md` says it about its own conventions and it is true here: nothing runs a markdown file. The
guard on this page is the review, plus the fact that every entry names the measurement that produced
it, so a reader can re-run it instead of trusting it. The MEASUREMENT is the durable half of an entry,
and a backlog mark beside it is only scheduling: where an entry has open work, the words name it, and
where it has none, the entry IS the decision.

## 13 · A remedy is checked for being PRESENT, not for being right

`epics-doctor` appends a remedy to every status that reports a problem, and
`test_every_problem_status_names_a_remedy` proves that each of them has one, that it is not empty, and
that it opens with an imperative. None of that says the advice is CORRECT, or still correct: a remedy
naming a variable that gets renamed, or pointing at a section of `docs/deployment.md` that moves, goes
stale in silence. Two of the seven texts are backed by that file (the CA bundle and the per-plane
variables), the other five by the probe that produces the status, and nothing re-checks either link.

Measured: the guard survives replacing any remedy with a different, equally imperative sentence, and
no test reads a documentation path out of a Python string (`tests/test_doc_links.py` scans tracked
markdown only). Deliberate: a guard over the WORDING would pin prose that has to be free to improve,
which is the trade this page's first entry already describes. The check is the review.
