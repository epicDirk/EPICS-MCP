# CLAUDE.md: EPICS MCP

Guidance for an AI assistant (Claude Code and similar) working **in this repository**. It applies to
every session here, including a fresh one with no other skill or project folder loaded; this repo is
meant to be self-sufficient.

## Role of this file

This server is an **autonomous knowledge carrier**: its operational knowledge ships *with* the code, so
a session that only has this repository can use and extend the server without re-deriving how the
EPICS service landscape behaves. This file is the standing policy that keeps that true.

## Orientation: where things live

- `README.md` is the landing page: what the server is, the planes, install, and an index of
  everything below. Keep it short; reference depth belongs in `docs/`.
- `docs/` holds the reference pages the README indexes: `tools.md`, `configuration.md`, `safety.md`,
  `mcp-clients.md`, **`deployment.md`** (bringing the server up in a new facility, the first thing
  an outside adopter needs) and **`write-gate-contract.md`** (the specification every in-server
  write gate must meet).
- `ARCHITECTURE.md`: the `server → tools → services → clients` layering and the plane model.
- `CONTRIBUTING.md`: dev setup, the gate chain, Definition of Done, commit style.
- `SECURITY.md`: reporting channel, security posture, and an explicit statement of what the write
  gates are **not**.
- `CHANGELOG.md`: **release history for a USER, in English.** A new, changed or removed tool; a
  breaking change; an error code or field that changed on the wire; a new `EPICS_MCP_*` var; a bug a
  user could hit. **Not** audit runs, test methodology, red-proof notes, `tools/list` byte budgets,
  work-item ids, or internal refactors with no user-visible effect: those go to the tiers below, and
  the work itself is narrated in the commit body. It drifted into a 763-line work journal once,
  because no rule claimed it.
- `.env.example`: the canonical, commented configuration template (every `EPICS_MCP_*` var).
- `examples/` holds a runnable `test.db`, a sample `.bob` and an `mcp.json`: the path for someone with
  no facility at all.
- **`src/epics_mcp/operator_guide.md`**: the operational cookbook (service planes, recipes, error
  signatures). Shipped as package data, served as the `epics-pv://guide` MCP resource, mirrored for
  humans by `OPERATING.md`. **One source file, three consumers: never copy it, link it.**
- `sandbox/`: a LOCAL EPICS test stack (IOC + REST services) for the opt-in live tests. It is
  git-ignored, so it is **absent from every clone**; the live tests are pointed at it through
  `EPICS_MCP_*` env vars and skip when those are unset.

## Knowledge Persistence Policy

Any insight worth keeping must land in a **durable, co-distributed** carrier, not just a session
transcript or a commit body, which future sessions won't find. Three tiers, by the nature of the fact:

1. **Code + tests**: when the insight is a concrete behaviour. Put it in the tool/service code, its
   docstring / tool-description (the always-on surface an assistant sees without pulling a resource),
   and a regression test. This is the most reliable tier: it runs in CI and cannot be forgotten.
2. **Operational cookbook**: when the insight is a service/operational/IOC recipe, an endpoint, or an
   error signature. Its home is **`src/epics_mcp/operator_guide.md`** (→ `epics-pv://guide`). This is
   the default tier for the kind of facts learned by actually operating against a live facility.
3. **Convention**: when the insight is a standing rule for how we work here. It goes in **this file**.

**Standing rule:** every new service / operational / IOC insight goes into the knowledge base (the
guide + docstrings), not only the session transcript. Because this policy lives *in the repo*, it holds
in any session working here, and that is the growth mechanism.

Two of these tiers are **drift-guarded**, not just prose (prose conventions are the category that rots):
`test_guide_matches_code.py` fails if the guide names a tool that is not registered or an `EPICS_MCP_*`
var that is not in `EpicsConfig`, and it fails if a registered tool is **missing** from the guide, so
adding a tool without documenting it is a red test, not a silent omission. The same file holds the
shipped **status legend** against the code (QA-47): a new `PlaneStatus` is red until it is sorted into
one of three declared guide-location buckets, and every glyph paired with a status name in one of the
two written forms `docs/known-limits.md` names, in the guide
or on any tracked `docs/*.md` page, must be the one `cli_doctor` renders. The page list is derived
from `git ls-files` rather than declared, so a new documentation page is covered the day it is
**tracked**: a page still unstaged in the working tree is not read at all, and a wrong glyph in it
passes locally until the `git add`. What that half deliberately does not check is dated in
`docs/known-limits.md`.

## Facility-agnostic guardrail (hard)

The guide, this file, `OPERATING.md` and the rest of the repository must stay **facility-agnostic** so
they can ship to a public fork: describe *patterns*, never a specific site. Do **not** commit any
facility domain, internal or cluster-member host name, live device/PV name, personal name, or a local
filesystem path carrying a username. Use synthetic placeholders instead (`SIM:PS-01:Cur-RB`,
`sim://ramp`, `http://archiver:17665`, `http://channelfinder:8080/ChannelFinder`).

Transcribe operationally-learned facts **by hand into agnostic form**: never paste raw output, error
strings (they embed the full request URL), or notes from a live session into a committed file. This is
enforced by `test_guide.py`'s `test_knowledge_files_are_facility_agnostic` over the committed knowledge
files: a site/username/person/local-drive-path **denylist** repo-wide, plus a **structural
device-PV-name detector** over the **same repo-wide population**, flagging anything ESS-PV-shaped
which is not a declared synthetic placeholder. That detector used to read only doc-suffixed files
outside `tests/` and `src/`, which was 20 of 180 tracked files; measured, the path half of that
filter excluded nothing and the **suffix** half hid every `.py`, which is where the one real leak
this repo has had (QA-27) actually sat. Widening it cost zero hits, because the test fixtures pass
on their declared synthetic markers rather than on an exemption. Do not rely on the local commit hook alone: its site patterns
come from a git-ignored file and are **absent on a fresh CI / public-fork checkout**, so the committed
test is the CI-effective check. It is best-effort rather than a proof, so the hand-transcription
rule above is what actually keeps it out.

**Accepted carve-out: the bare facility acronym.** "Facility-agnostic" means *no specific
infrastructure or identity leaks*, not that the facility's short name never appears. The bare acronym
and its product proper-nouns are the actual names of the software this server talks to, and they
collide with generic technical vocabulary, so `_SITE_RE` **deliberately does not scrub them**. What
the guard keeps out is unchanged and load-bearing: facility domains, cluster and host names, live
device/PV names, person names, and username filesystem paths. A reviewer seeing the bare name in a
tool description reads it as a product name; a domain, host, live PV or person name is still a
reject. Templating the acronym out as well is a separate, larger job, not a hole in this guard.

## Server-decided parameters: no promise before a differential live probe (hard)

A parameter whose semantics live on the **server** must not carry a **documented promise** until a
**differential live probe** has confirmed it: the same target expressed several ways, plus a
**positive control** (something that must match) **and a negative control** (something that must
not). Until then it is **unverified**, and it says so **where the promise is made, in the tool
description** and not only in a client docstring.

Scope: parameters the server interprets (a time window, a name filter, a glob, a sort order, a
config-tree name). **Not** parameters we enforce ourselves at the boundary (`timeout`, `size`,
`offset`; Pydantic rejects those before a request exists).

Why both controls, and why live:

- A mock cannot reach this class at all. It only ever knows what the client **sent**, never what
  the server **honoured**, and it is equally blind to a **loud** rejection (13 tests passed a
  literal `"a"` as a time; the real Archiver answers 500).
- One control is not enough. "It returns something" would also pass if the filter were dropped
  entirely (→ negative control); "every filter returns 0" looks the same as a filter that blocks
  everything (→ positive control).
- Reading the source is not a substitute. On exactly this question it was wrong five times in one
  week (two judges on Olog, an assessment on Alarm, a suspicion on Archiver, O1's premise).

Prefer making the promise **structurally true** over documenting a caveat: a `Literal`/enum at the
tool boundary is Tier 1 and cannot rot. Document the caveat only for what the boundary cannot
enforce. And a refusal is only correct while its premise holds: pin the server behaviour that
justifies it in a live test, so it goes **red** when the server improves
(`tests/test_olog_live.py::test_unreadable_sort_silently_reverses_on_the_server`).

Honest limit: this one is **prose**, and prose is the category that rots (see above). No CI guard
can prove a probe *ran*: CI runs with the live env unset, so live tests skip. The guard is the
review, plus the live tests themselves once someone points the env at a service.

## Evidence discipline (hard)

How claims are made here. Measured on this repository's own history: one working day produced
seven instances of the same error class, a plausible claim standing in for a measurement, and
every one was caught by an adversarial counter-probe, never by the author re-reading their own
work.

1. **Reading code is not a measurement.** A claim about behaviour is made only after executing a
   probe that could have falsified it. (The server-decided-parameter rule above is the special
   case of this for documented promises.)
2. **A non-finding carries the same burden of proof as a finding.** "There is no X" is a claim
   about the system, not about the search that failed to find it: say what was probed, and how.
3. **Universal claims ("none", "never", "structurally impossible") need more than a handful of
   samples.** Three probed paths do not support an all-quantifier.
4. **A rationale written into a docstring binds beyond its function.** Deviating a few functions
   later from a justification the same file states (for instance an exact-match rule with its
   reason, followed by a substring check) is a defect, not a style choice.
5. **A fix is a change like any other.** It gets the same adversarial counter-probe as the code
   it repairs; both defects the re-audit found in this repo's own hardening commit had been
   introduced BY the fix, and the adversarial review of the follow-up fix found two more in it.
   Every new guard must be proven able to go red (on the pre-fix code or via a mutant, by test
   node id): a guard that cannot go red is the defect it was meant to remove.
6. **Whoever commissions a review is liable for the premises handed to it.** A reviewer builds on
   what the prompt asserts and cannot re-check it, so measure premises before writing them into a
   prompt. And a verdict returned without findings (file:line, repro) is demanded back before
   acting on it.
7. **A probe that WRITES owns its data space, and the target is named, never derived.** A live test
   against a shared service needs a boundary on *what it may touch*, not just on which files it may
   edit. Deriving the target from the server's own answer looks self-sufficient and is the opposite:
   the server cannot know which of its resources somebody else has frozen, so the "obvious" pick is
   an unowned one. Measured here: `tests/test_write_gate_live.py` derived its deny logbook as
   `sorted(server_logbooks - allowlist)[0]`, which resolved by plain alphabetical order onto a
   parallel window's frozen fixture logbook and left six entries in it, on a service with **no
   delete**. An unnamed target is a refusal (through the live gate, so a demanded run goes red), not
   a guess; and where the medium cannot forget, every artifact a probe leaves is labelled as one.

8. **A double placed at the CLIENT-CLASS level cannot test anything that client does at its
   edge.** The convenient fake here is `monkeypatch.setattr(<module>, "<X>Client", _Fake)`, and it
   is used widely and is right for most purposes. But it removes, silently, every validation the
   real client runs on a service's answer: the `isinstance` guards that turn an unreadable
   payload into a loud error instead of a fabricated empty result. A test written that way can
   *appear* to protect such a guard while never executing it, measured with a spy on
   `olog_client._hit_count`: **not reached** through the tool path under a class-level double,
   so mutating the guard away left the test green. Where the assertion is *about* a client-edge
   guard, fake the **transport** instead (`get_shared_session`, so the real client runs and only
   the socket is replaced), and prove the guard red-provable through the layer the caller
   actually uses. Corollary for reviews: "there is a test for that guard" is a claim about which
   *seam* the test fakes, not about the guard's name appearing in it; and, one step further,
   faking at the right seam still does not say the right ADDRESS was requested. One faked
   response serves every endpoint, so where two routes return the same shape the result slot is
   no evidence: the requested URL has to be asserted on its own (measured: pointing `list_tags`
   at the properties URL left the whole suite green, with the payload-path test already faking
   at the transport seam).

   Auditing this is `scripts/guard_audit.py`, and its findings are pinned in code rather than
   written up somewhere, split by what checking them costs. The figures that follow from the
   test suite's own AST live in `guard_audit.PINNED_AST` and are checked by the ordinary gate
   (`tests/test_client_edge_guards.py`), together with the population and every recorded finding's
   line. The figures that depend on which tests EXECUTED a guard line live in
   `guard_audit.PINNED_COVERAGE` and are checked only by `guard_audit.py sham --check
   --coverage-db <db>`, deliberately: reaching them costs a full ctrace run, and a check that
   claimed to have verified them without one would be the sham guard this audit exists to find.
   Run without a database it verifies the cheap pair and NAMES the pins it could not reach, on the
   clean run as well as the failing one. Two directions, because
   a mutation sweep alone answers the wrong question: it asks whether ANY test notices a guard
   disappearing, so a guard covered by a real test AND a sham one reads as "guarded" and the sham
   is acquitted. ⚠️ That double-cover risk is argued, not observed: in the one proven case
   nothing else covered `_hit_count` (its own commit records every other test staying green), so
   a sweep WOULD have caught it. The sham list comes from the coverage map read BACKWARDS (which
   tests execute the guard, versus which claim it), and costs no run at all.

   ⚠️ Record the map with `COVERAGE_CORE=ctrace`. On Python 3.12+ coverage defaults to the
   `sys.monitoring` core, which disables a location after its FIRST observation, so every later
   test covering the same line leaves no context row. Measured here, both maps on the same
   1472-test tree (2026-07-26): 72 tests touch a guard line under the default core versus 292 under
   ctrace (median 2 versus 13, and the default map is poorer on 58 of the 61 covered lines). An
   audit driven by the default map runs a quarter of the relevant tests and reports false
   survivors, and it becomes the sham guard it was built to find.

Honest limit: these are prose rules, the category that rots (see above). No CI guard can prove
they were followed; the guard is the adversarial counter-probe itself.

## Write-gate contract (hard)

Every tool that **mutates** a downstream service sits behind an in-server write gate. Two exist
today (PV and logbook); a third repeats every question, so the requirements are written once, as a
specification, in **[docs/write-gate-contract.md](docs/write-gate-contract.md)**. A gate that does
not meet all six points is not done.

The six in one line each: **(1)** an env on/off gate, default OFF, independent per surface;
**(2)** an allowlist whose empty case is fail-closed, in a shape decided and justified per surface,
never copied from a sibling; **(3)** a rate limit, where a denied write consumes no token;
**(4)** a mandatory metadata-only audit, one terminal line per gate verdict, and a pre-gate refusal
must not wear the gate's error code; **(5)** a fail-closed reach/URL boundary parsed by the
connecting library's own parser; **(6)** an honest scope statement, because the gate is a
**guardrail on the sanctioned path, not a security boundary**.

Read point 6 first: it fixes what a gate is *for*, and the other five only make sense under it.
Points 1 to 5 are drift-guarded per gate by `tests/test_write_gate_contract.py`; point 6 and the
empty-semantics *rationale* of point 2 are prose, the category that rots.

## Definition of Done (doc-sync)

A new tool / resource / config is not done until: `README.md` is updated (resource URIs are
drift-checked), the server `instructions` / tool descriptions carry the point-of-need semantics,
including, for a server-decided parameter, its **probed** semantics or an explicit "unverified"
(see above); the operator guide is updated, and `.env.example` documents any new var. See
`CONTRIBUTING.md`.

## Build-once

The display-aware tools depend on `opi_navigation` via the `displays` **dependency group** (PEP
735), whose git rev is pinned in `[tool.uv.sources]`. A group is deliberate: unlike an extra it
never reaches the published metadata, so the private engine neither blocks a PyPI upload nor is
advertised to users who cannot install it. Changes internal to this server do **not** bump that
pin; only a change that needs a newer `opi_navigation` does (then move the pin and the lockfile
together).

## What is NOT persistence

Commit bodies (read, rarely found later), plan snapshots, personal assistant memory, and session
handover notes or analysis write-ups: **documented is not discoverable**: a file no future
session systematically reads is a transcript, not a home (measured: a seven-row defect table that
lived only in a handover nearly died with it). Durable knowledge belongs in code+tests, the
operator guide, or this file.

## Gates

`uv run pytest` green (coverage not reduced) + `uv run pre-commit run --all-files` green (ruff,
ruff-format, mypy --strict, the no-secrets / no-site-internal guards, and the two prose guards
below). Conventional commit prefixes, one logical change per commit.

**English, and ASCII punctuation, both mechanised.** This repository grew out of a German-speaking
project and carried German comments and an em dash as its default connector for months; removing
both took a multi-day sweep. `scripts/check_language.py` (three independent signals: function
words, umlauts, German word formation) and `scripts/check_typography.py` (em/en dash, ellipsis,
curly quotes, doubled hyphen) make that sweep hard to undo. Translate rather than delete, replace
the character rather than exempt it; the exception files are for text where the German or the
character IS the subject. Their honest reach is `docs/known-limits.md` sections 9 and 10. ⚠️ Do not
assume ruff covers the characters: probed one at a time, it recognises **three** of the nine
(en dash, apostrophe, multiplication sign) and nothing at all outside `.py`.
