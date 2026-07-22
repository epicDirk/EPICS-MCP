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
- `sandbox/README.md` — the local EPICS test stack (IOC + REST services) the tests exercise against.

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

Honest limit: these are prose rules — the category that rots (see above). No CI guard can prove
they were followed; the guard is the adversarial counter-probe itself.

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
