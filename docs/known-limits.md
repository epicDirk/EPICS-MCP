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
Entries 1 to 8 were measured 2026-07-26 unless the entry itself states a later date, entries 9 and 10
on 2026-07-28, entry 11 on 2026-07-29 and entries 13 and 14 on 2026-07-30; entry 12 states a
property of this page and measures nothing.

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
mis-transcribed it on the way in. A page of limits that sends a reader to the wrong function costs
exactly what the limit was meant to save.

## 8 · The prose detector's reading behaviour is pinned on constructed input only

**Re-measured 2026-07-30, and this entry changed as a result (S37).** It used to say that
`tests/prose_numbers.py` had no test module at all, so both of its recorded repairs could be reverted
with nothing going red. It now has one, `tests/test_prose_numbers.py`.

That module is written over SYNTHETIC prose, and that is forced rather than preferred: on the watched
tree the three spellings of the boundary class find an identical set of sites and keys, so a test
against the real files cannot separate them, and would report the behaviour as guarded while every
mutation of it stayed green. Constructed input is the only place the difference exists.

Five behaviours are pinned, and each was proven able to go red by mutating the module and naming the
test that fell: the sentence boundary (mutant `(?-i:[A-Z])`, three cases fell), the rule that a gap
word may not end in a terminator (two fell), the blank line that ends a docstring paragraph, the bare
`#` that ends a comment run, and the tokenizer being fed through `io.StringIO` rather than
`str.splitlines`. The module was restored to a sha256-identical state afterwards.

What remains true, and is why the heading says "on constructed input": the watched tree exercises none
of it. These tests establish what the module DOES with prose somebody wrote for them, never that the
watched prose is read correctly, which is the comparing half's job.

⛔ One specific tempting repair is measured to be **wrong**: making the boundary class case-sensitive.
Counting a boundary as `[.;:]` + whitespace + a letter, inside the comment runs and docstring
paragraphs the detector actually reads across the five watched files, **just over half** are followed
by a lower-case word: an identifier, a quoted term, a continued clause. A capital-only rule stops
recognising those and re-introduces the cross-sentence pairing the look-ahead exists to prevent. The
class in the code now says "any letter", which is what it always did under `re.IGNORECASE`, and since
S37 the decision is a TEST rather than this paragraph:
`test_a_lower_case_word_after_a_terminator_still_ends_the_sentence` fails on the capital-only rule.
⚠️ Only on the SCOPED mutant, though. A bare `[A-Z]` is no mutation at all under `re.IGNORECASE`, so
an author checking their own repair against it measures nothing and concludes it is safe.

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

## 14 · The shipped status legend is guarded by name, glyph and set, not by what it says

Measured 2026-07-31. This entry has now been rewritten three times, once per QA round: the first
round found six places where the guard checked less than it promised, the second found four more,
and the third found that three of the second round's own pins held a helper rather than the guard
that calls it, plus two behaviours in the guard itself. Each rewrite is dated against the state that
followed it, because a guard's honest limits are the one thing that cannot be written in advance.
`tests/test_guide_matches_code.py` holds the operator guide's status legend against `PlaneStatus`
and `cli_doctor._STATUS_MARK`. Every status is sorted into one of three declared buckets (named in
the legend, named in the prose paragraph above it, named in neither), and the buckets are compared
with `PlaneStatus` as SETS, with a double-listing count beside the comparison because set equality
alone cannot see a repeated member. The legend's Status and Mark cells are held against the code in
both directions, its rows are counted before they are reduced, and its delimiter row, its
indentation and its row widths are held against the rules a renderer applies rather than against a
character class. Every glyph and status pairing on
the guide and on every tracked `docs/` page has to agree with the mark the CLI prints. Every
lower-case identifier written as a code span in the guide's status paragraph has to be a plane
status. The buckets are typed `frozenset[PlaneStatus]`, so a name that no longer exists is a
`mypy --strict` error naming the offending literal as well as a red test. Every pairing on the tree
today agrees, and the guard re-checks that on every run.

What holds a behaviour here, and where that stops. A pin on constructed input holds a FUNCTION; it
does not hold that a guard still calls it, and the difference was measured rather than reasoned.
The second round pinned the set comparison, the reverse direction and the duplicate count by
pinning the helper each had been extracted into. Reverting any of the three AT THE GUARD, with the
helper and its pin untouched, then left all 1702 tests green: the helper went on being correct and
nobody asked it anything. Only the optional leading pipe reddened, because that behaviour lives
inside the reader the guard itself calls. Each of the three therefore has a second pin that runs
the GUARD on a world doctored for one line, and with those in place each revert reddens exactly one
pin.

What is held on constructed input today: the declared mark class, the whitespace-run folding, the
code-span pass, the consuming span reader, the allowlist floor, the delimiter rule, the blank-line
rule, the indentation limit, the row width, the odd-backtick filter, the order of the two
operations in the span pass, and the three guard wirings above.

Three things are NOT held, named rather than implied. The page population is derived from
`git ls-files` and is exercised on the tree only. The two-token filter in the span pass cannot be
reddened by any input, and the paragraph on it below says why. And no gate in this repository sees
a pin being DELETED: pytest reports an emptied test body as passed and the lint rules here carry no
flake8-pytest checks, so every pin above protects a revert of the code and none protects itself.

What follows is what the guard does NOT do, and one thing it records.

**The Meaning column is not read.** A row may describe its own status wrongly and stay green,
measured by giving one row another row's meaning and by replacing a meaning with nonsense: all four
guards stay green. Rejected rather than postponed, for the reason entry 13 gives about the remedy
texts: a guard over the wording pins prose that has to stay free to improve. What is compared is a
pair of code tokens.

**The location buckets are checked inside two marked regions, not across the file.** The whole-file
search was written first and then rejected on measurement, and the argument rests on TWO of the
three declared-absent names rather than three. `Disabled` (guide line 647) is an alarm configuration
command value and `Info` (line 153) is an Olog level, both capitalised, so only a case-INSENSITIVE
whole-file search reddens on them. The third name carries none of that weight: it is the `State` of
`diagnose_connection` in the code, but the guide writes it only as plain prose about PV connections
and never as a code span, in either case mode, so no whole-file search over spans sees it. Two are
enough. A whole-file search reddens on an edit that documented something else, and the obvious
repair for that red, moving the status into a "named" bucket, is fully GREEN while retiring the gap
the bucket exists to record. The price paid instead is a false negative: a status documented in some
third place goes unnoticed. The match is case-sensitive so that the behaviour is stated rather than
left to the implementer, and NOT because the region-scoped guard needs it: case-insensitively, still
confined to the two regions, it stays green, because the capitalised words carrying those foreign
senses both stand outside the regions.

**A bucket says where a status NAME appears, not whether the concept is explained, and not that it
appears only there.** The guide describes a disabled plane, an empty URL variable reported honestly
and never a failure, without ever using the status name, so that status counts as unnamed here. And
two statuses that have a legend row are named in the prose region as well, which no bucket denies
and nothing requires. They ARE read, by the reverse direction below, which is why removing either
from `PlaneStatus` reddens; what no guard states is that the prose has to keep naming them.

**A pairing needs both halves side by side.** A sentence naming glyphs alone, as the guide does
where it tells a reader which lines to read, carries no status name and is outside the scan. Side by
side means one of exactly two written forms: two backticked tokens in the same block, separated by
at most three characters none of which is a word character, or exactly two whitespace-separated
tokens inside ONE backticked span. ⚠️ Those three characters are counted AFTER every run of
whitespace collapses to one, so a line break with any indentation behind it counts as one character
and only a blank line separates two blocks. Counting the raw text gives the wrong answer in both
directions. Blocks are separated by a blank line on purpose, because folding the whole file into one
line would let a paragraph ending on a status name pair with the next one beginning on a mark;
measured, both variants read the same pairings on the tree today, so the stricter one is free. Two
forms it does not read: a status and a mark further apart than that, and either half written without
backticks.

**Marks are recognised through a DECLARED character class, not through the current mark mapping.**
That looks weaker and is stronger. A mark the CLI has RETIRED is no longer a member of the mapping,
so a scan keyed on its values stops seeing the stale documentation copies at the exact moment they
become stale, which is the one drift direction this scan exists for. Measured by retiring one mark:
the value-keyed rule read fewer pairings than exist and reported NONE of the stale ones, the
declared class reported every one. What it costs is one shape of false red that the value-keyed rule
already produced, a status name written TIGHTLY against a mark it is being contrasted with rather
than paired to: the name, a slash, and the mark, with nothing else between them. Only that tight
spelling, and that was measured rather than assumed. The sentence this entry used to cite, the one
about an "ok versus question-mark distinction", reads as nothing at all, its gap being eight
characters against a limit of three, and the same holds for the "vs" and comma spellings. Two more
open rules were built and rejected because each adds false reds the declared class does not: "a
single character that is not a digit" reads a yes/no support column, a placeholder letter and the
dash of an empty table cell as marks, and "not alphanumeric or a current mark" still reads that
dash.

Naming the forbidden repair protects the spelling that was named, and there were two. Putting the
membership test back into the scan is caught by a property pin. Writing the CLASS as a view of the
mapping buys the identical blindness, was green in every guard here, and no runtime check can see
it: a derived constant is built at import time from a mapping that still holds every current mark,
so the two are equal until a mark is retired, which is the one moment nobody is looking. That one is
therefore held on the SOURCE, by an assertion that the assigned expression reads no name at all. A
floor over the FORM of the rule sees neither: it stays vacuously green under both, because the class
still covers every current mark.

**The per-surface floor detects an EMPTY read, not an almost-empty one, and it covers three of the
eight surfaces read.** A file that stops documenting all but one of its pairings passes. On the
guide the floor is carried by the six legend rows that the legend guard already compares, so the
derived scan adds no floor of its own there. Measured: rewriting the guide's prose pairings into a
form the scan does not read leaves every guard green. The tempting repair, a floor of "at least one
pairing OUTSIDE the legend region", is green today and was rejected on the test this page applies
elsewhere: a legitimate prose rewrite reddens it, and the obvious repair for that red is to delete
the floor, which is green and retires it. The other tracked `docs/` pages are read without a floor,
deliberately: most of them document no status at all, so requiring one of each would be red on the
day it was written. A global extraction break is therefore caught, a page-specific one is not.

⚠️ The tuple naming those three surfaces is read TWICE, as a floor on the input and as a floor on
each page's output, so removing a name from it is never the small edit it looks like. When a page
legitimately stops documenting statuses its output floor reddens, and deleting its name is the
repair that message invites; measured, that also retires the input floor, so a later rename of the
same page drops it out of the scan silently. The entry for the guide itself can only ever satisfy
the input floor, never fail it, the guide being put into the population unconditionally.

**A page is covered the day it is TRACKED, not the day it is written.** The population comes from
`git ls-files`, which reads the index, so a documentation page sitting unstaged in the working tree
is not read at all and a wrong glyph in it passes the local run until the `git add`. Measured both
ways in a throwaway repository. CI runs on the committed tree, so what this costs is the run before
staging. A page tracked but absent from the working tree, or one that is not decodable as UTF-8,
fails loudly with the interpreter's own error rather than with this guard's message.

**What the status paragraph's reverse direction does not see.** Every lower-case identifier written
as a code span in that paragraph has to be a plane status, which is what makes the marker printed
above it true: before that, a status removed from `PlaneStatus` and from its bucket while the
sentence naming it stayed in the guide was green in all four guards, and the tiling guard FORCES
that bucket removal in the same edit. It does not see a status name outside both marked regions, one
written without backticks, one written with a capital, or one written as a code span that runs
across a LINE BREAK, which the odd-backtick filter below drops before this direction ever sees it.
The allowlist for tokens in that paragraph
that are not statuses is EMPTY today, and that emptiness is the promise; a legitimate new non-status
word there reddens and has to be DECLARED, which the failure message says in as many words. That
instruction costs something only because every entry has to earn its place: an entry the paragraph
does not write is reported, or declaring and deleting would be the same move at different prices.

⚠️ PADDING inside a code span used to be a fourth blind spot and is not one any more, which is
worth recording because the repair is easy to mistake for tidiness. This direction compares against
what a READER sees, and CommonMark removes one leading and one trailing space from a span when both
are present, so a padded span and its unpadded twin render as the same word. Measured before the
repair, against the real prose region: the unpadded spelling was reported and all four padded ones
were green, which made two typed spaces a cheaper way out of this check than the allowlist entry
its own failure message asks for. The span is stripped at the comparison point now. It is NOT
normalised in the shared span reader, deliberately: the pairing pass splits its tokens anyway and
never had the defect, so the reader goes on saying what the markdown literally holds.

**Both readers of markdown agree on what a code span is, and that was not always so.** The pairing
scan consumed whole spans while the reverse direction matched a pattern carrying its own backticks,
and a pattern cannot tell an opening backtick from a closing one. Measured, it read the prose
BETWEEN two adjacent spans as if it were one, and missed the real span behind it. Both faces are
pinned now. Neither could fire on the tree: the guide's two marked regions carry no adjacent spans
at all.

**A line carrying an odd number of backticks is skipped BY THE SPAN READER.** Markdown pairing
cannot be read from it, and these documents carry code spans that run across a line break. The
qualifier is load-bearing and was lost from this entry for one round: the filter lives in the shared
span reader, so it covers the code-span pass and the reverse direction, and NOT the neighbour pass,
which folds whole blocks and has no notion of a line. Measured, an odd-parity line carrying two
backticked tokens three characters apart is read as a pairing by that pass today. Measured too,
twenty such lines across the surfaces read, none of them carrying a pairing and none inside either
marked region, so the filter changes no result on this tree: 22 pairings with it and 22 without,
which is why it is held on constructed input instead. Its reach is per line: a line of even parity
that begins inside an unclosed run is read out of phase, and can be read as a pairing that a reader
sees only as literal backticks inside a longer span.

**The legend is read positionally, and its delimiter row is held against the renderer's rule.**
Inside the markers the first non-blank line is the header, the second the delimiter, and every line
after that has to parse at the header's width. That is what lets a legal edit through (a row without
its leading pipe, alignment colons, a renamed header column) while a row that lost its backticks
still fails loudly. The delimiter is checked as GFM checks it, same cell count as the header and
every cell dashes with optional colons, because a character class over the whole line accepts five
spellings under which the shipped legend renders as NO TABLE AT ALL, a wall of literal pipes for a
reader who has no repository around it, while the parser here went on reading every row and every
guard stayed green. A blank line between two rows does the same and is rejected for the same reason.

⚠️ INDENTATION was a sixth such spelling and the entry above missed it for a round, while claiming
to hold the rule a renderer applies. Four spaces or a tab make GFM read a line as an indented code
block, so the table ends there or never starts. Measured with a renderer on the shipped shape:
indenting the HEADER by four spaces gives a reader zero table rows, and so does indenting the
DELIMITER, or using a tab for either, while this parser read every row and every guard stayed
green. Four false greens, and the mirror case was a false RED: a table indented by one to three
spaces renders perfectly and was rejected, and only in the spelling that kept its leading pipe.
Both directions are held now, by a line check over every line of the block, header and delimiter
included, because those two never reach the row pattern and because a pattern whose optional pipe
is followed by a whitespace class cannot reject indentation in the pipe-less spelling at all.
Cross-checked against a renderer over 56 cases, two spellings by four placements by seven
indentations: no divergence. Named and rejected, because it is the cheapest repair and turns the
guard around: DEDENTING the region before parsing would make a legend that renders as no table
read happily, turning a correct red into a false green.

⚠️ This is not a markdown parser. It holds the delimiter rule, the blank-line rule, the indentation
limit and the row width, which is what the measured breakages violate; the rest of the GFM table
grammar stays outside. A renderer was probed and rejected on two grounds: it would make a guard's
verdict depend on a third-party version, and the only one in the tree today arrives transitively
rather than declared. The cost of reading positionally is unchanged: a prose note written between
the markers reddens the parse floor, and the repair is to put the note outside them.

**The ORDER of the two operations in the code-span pass is pinned, the two-token filter is not.**
Every span is matched first and the token filter applied afterwards; the shorter spelling, one regex
requiring whitespace inside the span, skips a whitespace-free span and re-anchors on its closing
backtick, so prose between two spans is read as a span. Measured on a documentation page, that
variant invents dozens of such fragments, several of them exactly two tokens long. It produces no
false red today, and that was measured rather than assumed: both spellings yield an identical
pairing list on this tree.

⚠️ This entry got its own subject wrong twice in three rounds, which is why it now names the pin.
It first said the order was held by no test, which was true. It then said the order was pinned,
because both markdown readers had been put on one span extractor and THAT extractor was pinned; but
the shorter spelling simply stops calling the extractor, so the pin could not see it, and the
revert left all 1702 tests green. The order is held now by a pin on an input where the two spellings
DISAGREE, with the reverted spelling asserted first so the emptiness is a statement about the rule
rather than about an extractor that has stopped reading.

What keeps those invented fragments harmless is the two-token filter, not the mark class, and THAT
is the part no test can redden: it is a conservative narrowing whose motivating separator forms are
false reds only under an open mark rule, and relaxing it changes no result here. It is written down
for the reader who would simplify it away.

**This page is itself a scanned surface.** Since the scan reads every tracked `docs/` page, an
illustration here must not put a single-character code span within three characters of a backticked
status name, counting a whole run of whitespace as one and remembering that only a blank line ends
the block, and must not put a status name and a single-character token inside one code span. For a
value borrowed from another namespace, prefer prose to code formatting. This is not theoretical: the
first draft of the rewrite above put a contrasted pair into a live code span and the scan went red
on it, one paragraph below this one.

**The gap those buckets record, written out here because a backlog is not a home for it.**
`disabled`, `info` and `disconnected` are never named by their status name anywhere in the shipped
guide, and two of the marks, the middle dot and the letter i, have no legend in it at all. That is a
measurement and not a decision: the legend deliberately carries the identity-near statuses, but
nobody decided that these three should be invisible to a reader who has only the shipped document.
Re-derive it by searching the guide for each `PlaneStatus` value as a backticked token. The
declaration in the test file states today's answer, and it goes red the moment one of them is
documented inside a marked region.
