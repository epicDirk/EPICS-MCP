# CLAUDE.md — EPICS PV MCP Server

Guidance for an AI assistant (Claude Code and similar) working **in this repository**. It applies to
every session here, including a fresh one with no other skill or project folder loaded — this repo is
meant to be self-sufficient.

## Role of this file

This server is an **autonomous knowledge carrier**: its operational knowledge ships *with* the code, so
a session that only has this repository can use and extend the server without re-deriving how the
EPICS service landscape behaves. This file is the standing policy that keeps that true.

## Orientation — where things live

- `README.md` — what the server is, the planes it sees, safety/network posture, tools, configuration.
- `ARCHITECTURE.md` — the `server → tools → services → clients` layering contract.
- `CONTRIBUTING.md` — dev setup, the gate chain, Definition of Done, commit style.
- `.env.example` — the canonical, commented configuration template (every `EPICS_MCP_*` var).
- **`src/epics_pv_mcp/operator_guide.md`** — the operational cookbook (service planes, recipes, error
  signatures). Shipped as package data, served as the `epics-pv://guide` MCP resource, mirrored for
  humans by `OPERATING.md`. **One source file, three consumers — never copy it, link it.**
- `sandbox/` — a LOCAL EPICS test stack (IOC + REST services) for the opt-in live tests. It is
  git-ignored, so it is **absent from every clone**; the live tests are pointed at it through
  `EPICS_MCP_*` env vars and skip when those are unset.

## Knowledge Persistence Policy

Any insight worth keeping must land in a **durable, co-distributed** carrier — not just a session
transcript or a commit body, which future sessions won't find. Three tiers, by the nature of the fact:

1. **Code + tests** — when the insight is a concrete behaviour. Put it in the tool/service code, its
   docstring / tool-description (the always-on surface an assistant sees without pulling a resource),
   and a regression test. This is the most reliable tier: it runs in CI and cannot be forgotten.
2. **Operational cookbook** — when the insight is a service/operational/IOC recipe, an endpoint, or an
   error signature. Its home is **`src/epics_pv_mcp/operator_guide.md`** (→ `epics-pv://guide`). This is
   the default tier for the kind of facts learned by actually operating against a live facility.
3. **Convention** — when the insight is a standing rule for how we work here. It goes in **this file**.

**Standing rule:** every new service / operational / IOC insight goes into the knowledge base (the
guide + docstrings), not only the session transcript. Because this policy lives *in the repo*, it holds
in any session working here — that is the growth mechanism.

Two of these tiers are **drift-guarded**, not just prose (prose conventions are the category that rots):
`test_guide_matches_code.py` fails if the guide names a tool that is not registered or an `EPICS_MCP_*`
var that is not in `EpicsConfig`, and it fails if a registered tool is **missing** from the guide — so
adding a tool without documenting it is a red test, not a silent omission.

## Facility-agnostic guardrail (hard)

The guide, this file, `OPERATING.md` and the rest of the repository must stay **facility-agnostic** so
they can ship to a public fork: describe *patterns*, never a specific site. Do **not** commit any
facility domain, internal or cluster-member host name, live device/PV name, personal name, or a local
filesystem path carrying a username. Use synthetic placeholders instead (`SIM:PS-01:Cur-RB`,
`sim://ramp`, `http://archiver:17665`, `http://channelfinder:8080/ChannelFinder`).

Transcribe operationally-learned facts **by hand into agnostic form** — never paste raw output, error
strings (they embed the full request URL), or notes from a live session into a committed file. This is
enforced by `test_guide.py`'s `test_knowledge_files_are_facility_agnostic` over the committed knowledge
files — a site/username/person **denylist** repo-wide, plus a **structural device-PV-name detector**
over the prose/knowledge surface (docs + the operator guide) that flags anything ESS-PV-shaped which
is not a declared synthetic placeholder. Do not rely on the local commit hook alone: its site patterns
come from a git-ignored file and are **absent on a fresh CI / public-fork checkout**, so the committed
test is the CI-effective check — best-effort, not a proof, so the hand-transcription rule above is what
actually keeps it out.

**Accepted carve-out — the bare facility acronym.** "Facility-agnostic" here means *no specific
infrastructure or identity leaks* — it does **not** mean the facility's short name never appears. The bare
acronym and its product proper-nouns (the naming service, the site gateway, and similar named services)
are the actual names of the software this server talks to and collide with generic technical vocabulary,
so `_SITE_RE` **deliberately does not scrub them** (see its inline comment: *no generic facility
abbreviations — they collide with real system names*). What the guard *does* keep out is unchanged and
load-bearing: facility domains, cluster / host names, live device/PV names, person names, and username
filesystem paths. So a reviewer who sees the bare facility name in a tool description or the guide should
read it as an accepted product name, **not** a leak; a reviewer who sees a domain, host, live PV or person
name must still reject it. A genuinely site-neutral public fork would additionally template even the bare
acronym / product names out — that is a separate, larger job (tracked on the roadmap), not a silent hole
in this guard.

## Server-decided parameters: no promise before a differential live probe (hard)

A parameter whose semantics live on the **server** must not carry a **documented promise** until a
**differential live probe** has confirmed it: the same target expressed several ways, plus a
**positive control** (something that must match) **and a negative control** (something that must
not). Until then it is **unverified**, and it says so **where the promise is made — the tool
description** — not only in a client docstring.

Scope: parameters the server interprets (a time window, a name filter, a glob, a sort order, a
config-tree name). **Not** parameters we enforce ourselves at the boundary (`timeout`, `size`,
`offset` — Pydantic rejects those before a request exists).

Why both controls, and why live:

- A mock cannot reach this class at all. It only ever knows what the client **sent**, never what
  the server **honoured** — and it is equally blind to a **loud** rejection (13 tests passed a
  literal `"a"` as a time; the real Archiver answers 500).
- One control is not enough. "It returns something" would also pass if the filter were dropped
  entirely (→ negative control); "every filter returns 0" looks the same as a filter that blocks
  everything (→ positive control).
- Reading the source is not a substitute. On exactly this question it was wrong five times in one
  week (two judges on Olog, an assessment on Alarm, a suspicion on Archiver, O1's premise).

Prefer making the promise **structurally true** over documenting a caveat: a `Literal`/enum at the
tool boundary is Tier 1 and cannot rot. Document the caveat only for what the boundary cannot
enforce. And a refusal is only correct while its premise holds — pin the server behaviour that
justifies it in a live test, so it goes **red** when the server improves
(`tests/test_olog_live.py::test_unreadable_sort_silently_reverses_on_the_server`).

Honest limit: this one is **prose**, and prose is the category that rots (see above). No CI guard
can prove a probe *ran* — CI runs with the live env unset, so live tests skip. The guard is the
review, plus the live tests themselves once someone points the env at a service.

## Evidence discipline (hard)

How claims are made here. Measured on this repository's own history: one working day produced
seven instances of the same error class — a plausible claim standing in for a measurement — and
every one was caught by an adversarial counter-probe, never by the author re-reading their own
work.

1. **Reading code is not a measurement.** A claim about behaviour is made only after executing a
   probe that could have falsified it. (The server-decided-parameter rule above is the special
   case of this for documented promises.)
2. **A non-finding carries the same burden of proof as a finding.** "There is no X" is a claim
   about the system, not about the search that failed to find it — say what was probed, and how.
3. **Universal claims ("none", "never", "structurally impossible") need more than a handful of
   samples.** Three probed paths do not support an all-quantifier.
4. **A rationale written into a docstring binds beyond its function.** Deviating a few functions
   later from a justification the same file states — e.g. an exact-match rule with its reason,
   followed by a substring check — is a defect, not a style choice.
5. **A fix is a change like any other.** It gets the same adversarial counter-probe as the code
   it repairs; both defects the re-audit found in this repo's own hardening commit had been
   introduced BY the fix — and the adversarial review of the follow-up fix found two more in it.
   Every new guard must be proven able to go red (on the pre-fix code or via a mutant, by test
   node id) — a guard that cannot go red is the defect it was meant to remove.
6. **Whoever commissions a review is liable for the premises handed to it.** A reviewer builds on
   what the prompt asserts and cannot re-check it — measure premises before writing them into a
   prompt. And a verdict returned without findings (file:line, repro) is demanded back before
   acting on it.
7. **A probe that WRITES owns its data space, and the target is named — never derived.** A live test
   against a shared service needs a boundary on *what it may touch*, not just on which files it may
   edit. Deriving the target from the server's own answer looks self-sufficient and is the opposite:
   the server cannot know which of its resources somebody else has frozen, so the "obvious" pick is
   an unowned one. Measured here: `tests/test_write_gate_live.py` derived its deny logbook as
   `sorted(server_logbooks - allowlist)[0]`, which resolved by plain alphabetical order onto a
   parallel window's frozen fixture logbook and left six entries in it — on a service with **no
   delete**. An unnamed target is a refusal (through the live gate, so a demanded run goes red), not
   a guess; and where the medium cannot forget, every artifact a probe leaves is labelled as one.

8. **A double placed at the CLIENT-CLASS level cannot test anything that client does at its
   edge.** The convenient fake here is `monkeypatch.setattr(<module>, "<X>Client", _Fake)` — it
   is used widely and is right for most purposes. But it removes, silently, every validation the
   real client runs on a service's answer: the `isinstance` guards that turn an unreadable
   payload into a loud error instead of a fabricated empty result. A test written that way can
   *appear* to protect such a guard while never executing it — measured with a spy on
   `olog_client._hit_count`: **not reached** through the tool path under a class-level double,
   so mutating the guard away left the test green. Where the assertion is *about* a client-edge
   guard, fake the **transport** instead (`get_shared_session`, so the real client runs and only
   the socket is replaced), and prove the guard red-provable through the layer the caller
   actually uses. Corollary for reviews: "there is a test for that guard" is a claim about which
   *seam* the test fakes, not about the guard's name appearing in it — and, one step further,
   faking at the right seam still does not say the right ADDRESS was requested. One faked
   response serves every endpoint, so where two routes return the same shape the result slot is
   no evidence: the requested URL has to be asserted on its own (measured: pointing `list_tags`
   at the properties URL left the whole suite green, with the payload-path test already faking
   at the transport seam).

   Auditing this is `scripts/guard_audit.py`, and its findings are pinned in
   `tests/test_client_edge_guards.py` rather than written up somewhere. Two directions, because
   a mutation sweep alone answers the wrong question: it asks whether ANY test notices a guard
   disappearing, so a guard covered by a real test AND a sham one reads as "guarded" and the sham
   is acquitted. ⚠️ That double-cover risk is argued, not observed — in the one proven case
   nothing else covered `_hit_count` (its own commit records every other test staying green), so
   a sweep WOULD have caught it. The sham list comes from the coverage map read BACKWARDS (which
   tests execute the guard, versus which claim it), and costs no run at all.

   ⚠️ Record the map with `COVERAGE_CORE=ctrace`. On Python 3.12+ coverage defaults to the
   `sys.monitoring` core, which disables a location after its FIRST observation, so every later
   test covering the same line leaves no context row. Measured here: 72 covering tests under the
   default core versus 291 under ctrace (median 2 versus 12, 56 of 61 lines affected). An audit
   driven by the default map runs a quarter of the relevant tests and reports false survivors —
   it becomes the sham guard it was built to find.

Honest limit: these are prose rules — the category that rots (see above). No CI guard can prove
they were followed; the guard is the adversarial counter-probe itself.

## Write-gate contract: what any in-server write gate must provide (hard)

This server exposes tools that **mutate** state on a downstream service — a PV setpoint, a logbook entry, an
archive/config record. Every such tool sits behind an **in-server write gate**. Today there are two (the PV
gate and the logbook gate); a third — an archiver, an alarm-acknowledge, or a channel-registry mutator —
repeats every question below. So the requirements are written once, here, as the standing contract. A new
gate that does not meet all six points is not done.

Read the scope statement (point 6) **first**: it fixes what a gate is *for*, and the other five points only
make sense under it.

**1. An environment on/off gate, default OFF.** Writes for the surface are disabled unless an explicit
`EPICS_MCP_ALLOW_<SURFACE>_WRITE=true` is set. Off is the shipping default and the safe state; a fresh
checkout, a missing env file, or a typo in the var name all resolve to *no writes*. Each surface's env gate
is **independent** — enabling one never enables another.

**2. An allowlist with explicitly-defined empty-semantics — empty is fail-closed, and *which* fail-closed
shape is a deliberate, documented per-surface choice.** The env gate only says *whether* the surface may be
written; an allowlist says *what* target is permitted — a regex over target names (`^SIM:PS-01:.*-SP$`), an
exact set of logbook names, a URL allowlist.

- **An empty allowlist is NEVER a silent allow-all.** On every surface it fails closed. An operator who
  genuinely wants every target writable must say so **explicitly** (a regex allowlist: `.*`; an exact-set
  allowlist has no "all", so an empty set can only mean deny). A silent allow-all on empty is a footgun — a
  forgotten env var reading as "permit everything" — and is a defect, not a valid configuration.
- **The *shape* of the fail-closed is a per-surface decision, made deliberately and documented at the config
  field itself.** Two sanctioned shapes exist:
  - **Refuse-to-start** — writes enabled with an empty allowlist raises a config error at gate construction,
    so the process will not run write-enabled-but-unscoped. Use this where an empty allowlist can *only* be a
    misconfiguration (a forgotten pattern) **and** there is a way to spell "all" explicitly.
  - **Deny-all at runtime** — the gate constructs, logs a loud warning that writes are enabled but nothing is
    allowlisted, and denies every write. Use this where deny-all-with-a-visible-error is the better operator
    experience (a wrong or missing target surfaces as a denied write, not a dead process).
- **Never copy a sibling gate's empty-semantics blindly, and never describe one gate in terms of another's.**
  The choice is load-bearing and must be re-decided for each surface, with its rationale written **at that
  surface's config field**, not inferred from whichever sibling the author read first. In particular: do
  **not** propagate any cross-gate comment of the form *"the inverse of the other gate, where an empty
  allowlist allows all."* Such a description is **inaccurate** — a name-pattern gate that refuses to start on
  an empty pattern does **not** allow all — and copying it forward is exactly how a third gate inherits a
  *fail-open* reading of "empty". Both existing gates are fail-closed on empty; they differ only in shape, and
  each states its own shape and reason at its own field.

**3. A rate limit.** At most N writes per fixed window, enforced in-server. This bounds the blast radius of a
runaway loop — the failure mode where the caller is wrong and issues the same mistaken write repeatedly.
Acquisition of a rate token (purge-expired → count → append) must be **atomic** under a lock if the gate can
run on more than one thread, and a write **denied by the gate** must never consume a token (audit-then-raise
happens before the token is appended). A refusal that happens *after* the gate has admitted the write — a
value-bounds check that runs on data fetched post-admission, say — legitimately *has* consumed one; that is a
different event, and it says so where it is implemented.

**4. A mandatory, metadata-only audit.** Every gate verdict, and every write that reaches the I/O, leaves a
**durable** record, and the record carries **metadata only — never free text**.

- **Mandatory core (every gate):** exactly one **terminal** line per **gate verdict** — **ALLOW** (the write
  completed), **DENY** (a gate precondition rejected it — record the reason code, emitted before the raise),
  or **FAILED** (it passed the gate and failed downstream). A surface may define **further terminal events**
  where its semantics genuinely differ (the PV gate's post-admission bounds refusal is its own event, not a
  DENY, precisely because it already consumed a token) — enumerate those where the gate is described instead
  of filing them under one of the three.
- **Scope this claim honestly.** The promise is *one terminal line per **gate verdict***, **not** "no write
  request is ever un-recorded". A refusal raised **before** the gate is consulted — a precondition in the
  tool or service layer above it — writes **no audit line at all**, and such refusals exist in this server
  today. Which forces the next rule:
- **A refusal raised outside the gate must not carry the gate's error code.** If a caller cannot distinguish
  an un-audited pre-gate refusal from a real, audited gate DENY, the audit's coverage claim becomes
  unfalsifiable from the outside — the exact silent gap the audit exists to close. Give pre-gate refusals
  their own code.
- **Stronger shape, REQUIRED where the mutation can be interrupted mid-flight** — where the code that issues
  the I/O can be abandoned by its caller while the I/O itself proceeds (an async put handed to a worker that
  a cancellation does not stop, so the value may still land). Then: an **ATTEMPT** line emitted **before** the
  I/O carrying a **correlation id**, plus an **UNKNOWN** terminal outcome (cancelled or timed out; may or may
  not have landed — verify by read-back, never blindly retry). Where the gate, the I/O and its terminal audit
  all run to completion inside one unit that a caller's cancellation cannot split, ATTEMPT/UNKNOWN are
  **optional**. **Do not hard-code one surface's audit shape as universal** — decide it per gate, like point
  2, and write down the reason. *(Known limit: "a caller cannot split it" is not "it cannot be interrupted".
  A transport-level timeout inside such a unit can still leave an applied write reported as FAILED. Where
  that is so, say it at the gate.)*
- **Metadata only.** The record may carry identifiers (target name, entry/operation id), the **principal** the
  server records as the writing account, any correlation id, and bounded scalars appropriate to the surface (a
  numeric setpoint's old/new value, a title *length*, an attachment *count* and *byte total*). It must
  **never** carry free text the read path redacts (a title/description body, a filename) — routing write
  content around the read-side redaction is itself the defect the audit exists to prevent.
- **The audit sink is validated at gate construction and fails closed** — a broken or unwritable sink is a
  config error, never a raw exception at the first write. *When* that validation runs follows the gate's own
  construction: at startup for an eagerly-built gate, at first write for a lazily-built one. An audit
  emission must never turn a denial or failure into a crash.
- **Know where the audit goes — and it is not optional.** A default sink (e.g. stderr) is ephemeral and
  un-persisted; an audit nobody can read after the process exits is a promise, not a record. So, separately
  from the per-gate validation above and unconditionally at startup: a write-enabled process **refuses to
  start** unless a durable sink is configured. Document the effective sink.

**5. Where the surface has network reach: a reach / URL boundary, enforced fail-closed.** A gate whose target
can be an arbitrary host must confine *where* a write can physically go, in addition to *what* may be written
(points 1–2):

- **When the target is fixed at construction** (a client search reach): confine it at construction — writes
  enabled while the search reach extends beyond loopback is a config error; refuse to start a write-enabled
  process that could reach a real facility network. The check must be **parser-faithful** (it reads the reach
  exactly as the underlying client will) and **resolution-free** (a hostname is never trusted as loopback).
- **When the target is per-write** (an HTTP URL like `http://logbook:8080`): confine it at each write — permit
  a loopback host, or an **exactly-allowlisted** URL over a confidential scheme with remote writes explicitly
  enabled — and take the host from **the same parser the client will actually connect with**, never a second
  parser and never a substring of the authority (`http://127.0.0.1@logbook-remote/…` has host
  `logbook-remote` and is refused). **Two URL parsers do not agree on hostile input:** a URL that one library
  reads as a loopback host, another reads as a remote one. A boundary validated with a *different* parser
  than the one that opens the connection is a bypass waiting to be found — so parse with the connecting
  library's own parser, not with whatever the standard library offers.
- **Fail-closed on the boundary itself:** an unparseable or unresolvable target is **denied first**, before
  any allowlist check — a bad target is a clean, audited DENY, and the allowlist can never override a target
  that fails to parse.

**6. An honest scope statement — the gate is a guardrail on the sanctioned path, not a security boundary.**
State this plainly where the gate is described, because the word "gate" invites a category error:

- The gate guards writes **through this server**. A caller with a shell, or with the write library the server
  itself depends on to run, can reach the same target **without passing the gate**. That path is **out of the
  gate's reach by construction** — it is the gate's *shape*, not a hole to be patched inside the server. The
  real boundary, if one is wanted, lives outside this process (network reach, account privileges, an external
  reconciliation watchdog); the gate does not claim to be it, and no future reader should mistake it for one.
- Therefore the audit's promise is **"every gate verdict, and every write through the server that reaches the
  I/O,"** not "every write". Scope every completeness claim that way; a claim of total coverage would be the
  very silent gap the audit exists to close.
- **Every deny-path must be verifiable — a guard that cannot be driven red is the defect.** Each denial the
  gate can raise (env off, allowlist miss, boundary reject, rate limit) must be exercised by a test that can
  be shown to **fail on the un-gated code or on a mutant** — the evidence-discipline rule applied to the
  gate. Note that the mutant must actually target the assertion: for a rate-limit denial, deleting the deny
  branch does not exercise "a denial consumes no token"; appending the token anyway does. Prefer a *live*
  deny-path test (one that goes red against a real target when the env points at a service) over an
  in-memory assertion alone: an in-memory-only deny test proves the branch, not that the gate fires against
  the wire. A gate whose refusals are only asserted in prose, never observed going red, has a documented
  promise, not a verified one.

**Honest limit:** points 1–5 are enforceable in code and are drift-guarded per gate by
`tests/test_write_gate_contract.py`; point 6 and the empty-semantics *rationale* of point 2 are **prose**,
the category that rots. No CI check proves a future gate author re-decided the empty-shape deliberately or
scoped the audit claim honestly — the guard there is the review plus the red-provable deny tests of point 6.

Point 6's own *live* preference, stated per surface rather than as one number:

- **PV gate — not applicable, and that is a reason, not an excuse.** Every one of its deny paths raises
  *before* the first network I/O. A live deny test would execute byte-identical code beside an unused
  socket; the in-memory rows are the honest coverage there, not a lesser substitute.
- **Olog gate — one path proven against the wire, the rest reasoned away.** `olog_allowlist_miss` on
  `add_log_attachment` / `update_log_entry` is live-covered by `tests/test_write_gate_live.py`. It is the
  only deny path whose *decision input* comes from the server (the target entry's logbooks, read back over
  HTTP), i.e. the only one a mock cannot falsify — the mock supplies the very payload shape the code
  assumes. The other Olog paths are argued out one by one in that module's docstring (env-off and the URL
  boundary are shadowed by the whole-mode precondition on those tool paths; empty-logbooks is unreachable
  there; the size cap has no server-side input; a rate-limit probe would need N real mutating writes).
- **Not claimed:** that every gate refusal has been observed against a real service. It has not, and the
  list above says which ones and why — recorded rather than papered over.

## Definition of Done (doc-sync)

A new tool / resource / config is not done until: `README.md` is updated (resource URIs are
drift-checked), the server `instructions` / tool descriptions carry the point-of-need semantics —
including, for a server-decided parameter, its **probed** semantics or an explicit "unverified"
(see above) — the operator guide is updated, and `.env.example` documents any new var. See
`CONTRIBUTING.md`.

## Build-once

The display-aware tools depend on `opi_navigation` via the optional `[displays]` extra, pinned by SHA in
`pyproject.toml`. Changes internal to this server do **not** bump that pin; only a change that needs a
newer `opi_navigation` does (then move the pin and the lockfile together).

## What is NOT persistence

Commit bodies (read, rarely found later), plan snapshots, personal assistant memory, and session
handover notes or analysis write-ups: **documented is not discoverable** — a file no future
session systematically reads is a transcript, not a home (measured: a seven-row defect table that
lived only in a handover nearly died with it). Durable knowledge belongs in code+tests, the
operator guide, or this file.

## Gates

`uv run pytest` green (coverage not reduced) + `uv run pre-commit run --all-files` green (ruff,
ruff-format, mypy --strict, and the no-secrets / no-site-internal guards). Conventional commit prefixes,
one logical change per commit.
