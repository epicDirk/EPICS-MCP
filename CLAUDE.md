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
files. Do not rely on the local commit hook alone: its site patterns come from a git-ignored file and
are **absent on a fresh CI / public-fork checkout**, so the committed test is the CI-effective check —
a best-effort denylist, not a proof, so the hand-transcription rule above is what actually keeps it out.

## Definition of Done (doc-sync)

A new tool / resource / config is not done until: `README.md` is updated (resource URIs are
drift-checked), the server `instructions` / tool descriptions carry the point-of-need semantics, the
operator guide is updated, and `.env.example` documents any new var. See `CONTRIBUTING.md`.

## Build-once

The display-aware tools depend on `opi_navigation` via the optional `[displays]` extra, pinned by SHA in
`pyproject.toml`. Changes internal to this server do **not** bump that pin; only a change that needs a
newer `opi_navigation` does (then move the pin and the lockfile together).

## What is NOT persistence

Commit bodies (read, rarely found later), plan snapshots, and personal assistant memory. Durable
knowledge belongs in code+tests, the operator guide, or this file.

## Gates

`uv run pytest` green (coverage not reduced) + `uv run pre-commit run --all-files` green (ruff,
ruff-format, mypy --strict, and the no-secrets / no-site-internal guards). Conventional commit prefixes,
one logical change per commit.
