# Known limits — what this repository deliberately does NOT guard

**Measured 2026-07-26.** Every figure below is a measurement of the tree at that date, not an
invariant: this file is markdown, and markdown is unguarded here (that is the first entry). Treat a
number as "was true when written" and re-measure before building on it.

Why the file exists: the two drift guards this repository runs — the prose-counter guard
(`tests/test_prose_counters.py`, S32) and the sham-guard audit (`scripts/guard_audit.py`, S33) — both
attract an obvious next step that turns out, on measurement, to be the wrong one. Without this page
those measurements live only in commit bodies, which nobody finds again, and the next author pays for
them a second time. Each entry states the limit, what was measured, and why the tempting repair was
rejected rather than merely postponed.

---

## 1 · Markdown prose is unguarded, and a guard over it was rejected

The prose-counter guard reads Python comments and docstrings. It does not read `.md` files.

Measured: **38** size-naming phrases across **10** tracked markdown files (the detector's own
vocabulary — a number paired with one of `prose_numbers.COLLECTION_NOUNS`, or the `N of the M`
shape). **12 of those sit in `CHANGELOG.md` and `CLAUDE.md`**, and that is where the exemptions
would live: a changelog entry that said "22 schemas" in a past release must keep saying it. This page
is in its own census, and is one of the ten files.

Read the twelve rather than trusting the label, because they are not uniform: the `CHANGELOG.md`
release lines and `CLAUDE.md`'s dated coverage measurement are deliberately historical, but at least
two are neither historical nor claims — a numbered-list ordinal the detector paired with a following
noun, and a live epistemic maxim in the evidence-discipline section. Two of the twelve are therefore
detector reach rather than exemption candidates, which nudges the "roughly twenty" estimate below
DOWN and is itself an argument for the release-checklist route.

⚠️ Those three figures were first written as 32 / 9 / 11: the count of the tree BEFORE the last two
commits of the batch that added this page, and before the page itself existed. Corrected. They are
the clearest illustration of the header above — nothing re-runs a number in a markdown file, so
re-measure before quoting it.

Why no guard: it would need roughly twenty "historical, not derivable" rows — precisely the
construction `tests/test_prose_counters.py` already rejected, in writing, for its own two files ("a
blanket exemption wearing a table's clothes"). The one load-bearing markdown figure that has
demonstrably drifted is the README's test count, and a test cannot count the passes of the run it is
part of. **That figure belongs on a release checklist, not in the gate.** Tracked as work item S41.

## 2 · A number is guarded only where a sentence states it

Two sentences may state one figure. A pattern over one of them says nothing about the other, because
`test_the_recorded_figures_match_the_prose_that_states_them` uses `re.search` — one pattern per
WORDING, and only the first occurrence of each.

This has bitten three times, and the third was found by the QA of the commit that fixed the second.
A "17" restated as "those 17 keys sit on 28 targets" went unwatched, which is why the red-proof recipe
recorded for exactly that defect ran green again on shipped code. Then four MORE restatements in the
same docstring — the 28 twice, the "eight", and the vocabulary count — turned out to be settable to
absurd values with the whole suite green. All are patterned now, which is why that table looks
repetitive: the rows are not duplicates, they are the other sentences.

What is **still** unguarded, and this is the honest residue rather than a hypothetical: there is no
COMPLETENESS pin over that docstring. A figure added to it next month ships unwatched, exactly as
these four did. Work item S42. Separately, three sentences in that same file stated a superseded
`PINNED_COVERAGE` pair in the present tense for a day; the repair was to stop repeating a pinned
figure at all, rather than to correct the copies — a second copy of a guarded number is an unguarded
number.

## 3 · The anti-hard-coding meta-guard is a spelling check, one frame deep

`test_no_claim_hard_codes_its_expectation` searches a claim's pattern and its derivation for the
ANSWER, spelled in digits and in words. Measured on 79 claims:

- **51** derivations call a module helper, whose body lies outside the single inspected frame.
- **17** contain arithmetic, so `lambda: 20 + 2` for an expected 22 never spells its answer.
- A pattern can split the literal (`(1[1])` captures an 11 the skeleton search cannot see).

Widening the regex does not close this; a different mechanism does, and `_Claim.reads` (entry 4) is
the first half of it. Deciding whether `reads` is enough, or whether the remaining three holes need
their own construct, is work item S38.

## 4 · `_Claim.reads` proves provenance, not meaning and not dependence

The provenance tracer establishes that a measure TOUCHED the object its claim declares. Three things
it does not establish, all stated in the `_Claim` docstring as well so they cannot be read past:

1. **That the sentence MEANS that object.** A human decides which set a sentence is about; no
   construct replaces that reading.
2. **That the ANSWER depends on what was touched.** A measure can read a constant and ignore it.
3. **The file half is one-directional and, for three claims, nearly free.** `nullable arrays`,
   `nullable arrays (schema-test note)` and `ChannelInfo fields` reach their file through
   `_typed_dict_fields`, which parses the whole package — so "this file was parsed" cannot be told
   apart from "some file under `src` was parsed". Tightening it needs a third recorder over the
   index's keys. The module-attribute half IS checked for equality in both directions.

The stand-in measures the tracer now makes VISIBLE rather than fixed are work item S36 — visible
because each one's declaration says what it actually reads.

## 5 · The origin of a coverage map cannot be verified

`guard_audit.py sham --check --coverage-db <db>` cannot tell a map recorded with
`COVERAGE_CORE=ctrace` from one recorded with the Python 3.12+ default core, which disables a
location after its first observation and so yields a quarter of the context rows. Measured: the
sqlite `tracer` table is **empty in a genuine ctrace map too**, so there is no marker to read.

Consequence, and it is deliberate: the tool's mismatch message is a **disjunction** ("either the code
changed or the map is impoverished — check the core before re-pinning") rather than a check. A guard
asserting "I verified the map" would itself be the sham form this audit exists to find.

## 6 · The sham audit does not see helper- or fixture-installed doubles

The audited population is tests that install a client class double **in their own body**, read from
the syntax tree. A double installed by a helper or a fixture is invisible. Measured over the syntax
tree: **21** calls to `_install_fake` in `tests/test_olog_update.py` alone, in 21 distinct tests —
which is what `scripts/guard_audit.py`'s own note beside the helper has said all along.

⚠️ This entry first said **22**, from `grep -c "_install_fake("`, which counts the `def` line as a
call site. A raw token count standing in for the set the sentence names is the defect this whole
page documents, committed on the page itself; it is the reason the figure above says how it was
measured.

This is a declared blind spot, not an unknown one, and widening the population is a feature rather
than a repair: it forces a fresh `COVERAGE_CORE=ctrace` recording, because the coverage-dependent
pins are stated about the current population. Work item S40.

## 7 · That a human read the candidate list cannot be proven

The sham audit's verdict "these candidates were read and none is a sham guard" rests on a person
reading them. No construct can prove that happened. What IS mechanised since the audit's own repair:
the candidate **members** are frozen, not merely their count, so a new name is named and the reader
is sent back to it rather than silently inheriting the old verdict.

Two smaller decisions of the same kind, recorded here so they do not become work items that never
get ticked: the `unknown` fallback in **`_compare`** is unreachable by construction (everything it
could describe is in `PINNED`), and exit code 2 is shared by two distinct outcomes — an absent
coverage map, and an absent map *together with* a pin mismatch. Both are accepted as they are.

⚠️ This entry named `classify` instead of `_compare` when it was written. `classify` has no such
branch in any revision; the workspace roadmap's own note had it right, and the page mis-transcribed
it on the way in. A page of limits that sends a reader to the wrong function costs exactly what the
limit was meant to save.

## 8 · The prose detector's own reading behaviour is barely pinned

`tests/prose_numbers.py` has no test module of its own. Its sentence-boundary look-ahead is, measured
three times running, without effect on the currently watched prose — so both of its recorded repairs
could be reverted with nothing going red. Work item S37.

⛔ One specific tempting repair is measured to be **wrong**: making the boundary class case-sensitive.
Counting a boundary as `[.;:]` + whitespace + a letter, inside the comment runs and docstring
paragraphs the detector actually reads across the five watched files, **669 of 1300** are followed by
a lower-case word — just over half. A capital-only rule stops recognising those and re-introduces the
cross-sentence pairing the look-ahead exists to prevent. The class in the code now says "any letter",
which is what it always did under `re.IGNORECASE`.

⚠️ This pair of figures was first written here as "666 of about 1440" — the previous commit's tree,
stale on arrival, in two files, under the header above that promises a measurement of the tree at the
stated date. It is corrected and the method is now stated so it can be re-derived rather than
trusted. Nothing re-runs it: `prose_numbers.py` is deliberately unwatched and "boundaries" is not a
`COLLECTION_NOUNS` member, so no guard can reach either number. The **share** is what carries the ⛔
decision, and it is not close.

## 9 · Prose rules rot, and that includes this page

`CLAUDE.md` says it about its own conventions and it is true here: nothing runs a markdown file. The
guard on this page is the review, plus the fact that every entry names the measurement that produced
it, so a reader can re-run it instead of trusting it. Where an entry has a work item, the work item
is the durable half; where it does not, the entry IS the decision.
