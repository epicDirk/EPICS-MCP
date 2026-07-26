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

Measured: **32** size-naming phrases across **9** tracked markdown files (the detector's own
vocabulary — a number paired with one of `prose_numbers.COLLECTION_NOUNS`, or the `N of the M`
shape). **11 of those 32 sit in `CHANGELOG.md` and `CLAUDE.md` and are deliberately historical** —
a changelog entry that said "22 schemas" in a past release must keep saying it.

Why no guard: it would need roughly twenty "historical, not derivable" rows — precisely the
construction `tests/test_prose_counters.py` already rejected, in writing, for its own two files ("a
blanket exemption wearing a table's clothes"). The one load-bearing markdown figure that has
demonstrably drifted is the README's test count, and a test cannot count the passes of the run it is
part of. **That figure belongs on a release checklist, not in the gate.** Tracked as work item S41.

## 2 · A number is guarded only where a sentence states it

Two sentences may state one figure. A pattern over one of them says nothing about the other, and
this has bitten twice: a "17" restated as "those 17 keys sit on 28 targets" went unwatched, which is
why the red-proof recipe recorded for exactly that defect ran green again on shipped code. Both are
patterned now. There is no completeness pin over `tests/test_client_edge_guards.py`'s own docstring,
so a figure added to it ships unwatched — work item S42.

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
the syntax tree. A double installed by a helper or a fixture is invisible. Measured: **22**
`_install_fake(` call sites in `tests/test_olog_update.py` alone.

This is a declared blind spot, not an unknown one, and widening the population is a feature rather
than a repair: it forces a fresh `COVERAGE_CORE=ctrace` recording, because the coverage-dependent
pins are stated about the current population. Work item S40.

## 7 · That a human read the candidate list cannot be proven

The sham audit's verdict "these candidates were read and none is a sham guard" rests on a person
reading them. No construct can prove that happened. What IS mechanised since the audit's own repair:
the candidate **members** are frozen, not merely their count, so a new name is named and the reader
is sent back to it rather than silently inheriting the old verdict.

Two smaller decisions of the same kind, recorded here so they do not become work items that never
get ticked: the `unknown` branch of `classify` is unreachable by construction, and exit code 2 is
shared by two distinct outcomes (no map, and a pin mismatch). Both are accepted as they are.

## 8 · The prose detector's own reading behaviour is barely pinned

`tests/prose_numbers.py` has no test module of its own. Its sentence-boundary look-ahead is, measured
three times running, without effect on the currently watched prose — so both of its recorded repairs
could be reverted with nothing going red. Work item S37.

⛔ One specific tempting repair is measured to be **wrong**: making the boundary class case-sensitive.
**666 of about 1440** sentence boundaries in the watched prose begin lower case, so a capital-only
rule stops recognising them and re-introduces the cross-sentence pairing the look-ahead exists to
prevent. The class in the code now says "any letter", which is what it always did under
`re.IGNORECASE`.

## 9 · Prose rules rot, and that includes this page

`CLAUDE.md` says it about its own conventions and it is true here: nothing runs a markdown file. The
guard on this page is the review, plus the fact that every entry names the measurement that produced
it, so a reader can re-run it instead of trusting it. Where an entry has a work item, the work item
is the durable half; where it does not, the entry IS the decision.
