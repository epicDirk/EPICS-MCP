# Contributing

Thanks for your interest. This is a pre-1.0, work-in-progress project. Issues and small
PRs are welcome.

## Development setup

The gate chain is [uv](https://docs.astral.sh/uv/)-based:

```bash
uv sync --extra dev --group displays --frozen  # full local install (toolchain + display engine)
uv run pytest                                  # run the test suite
uv run pytest --cov=src --cov-branch           # with coverage
uv run pre-commit run --all-files              # ruff + format + mypy --strict + guards
```

**Extra vs group.** `dev` is the only published extra and carries the toolchain. The
`opi_navigation` PV engine, which the display-aware tools need, is a **dependency group**
(`--group displays`), not an extra, and the distinction is load-bearing: a group never reaches
the published metadata. The engine lives in a private repository, so an outside user cannot
install it by any route; as an extra it both advertised a promise nobody could keep and, being
a direct git reference, made the package unpublishable, which is the single thing PyPI refuses
outright.

CI runs `uv sync --extra dev --frozen` and passes no `--group`, so it tests exactly the
standalone core a public user gets. The `opi_navigation`-coupled test modules are dropped at
collection when the package is absent, so the core suite stays green.

**That drop is no longer silent (GB-27).** A green report over a hundred tests that never ran is
indistinguishable from a full one, so a run that drops those six modules now says so in its report
header, and a run that DEMANDS them refuses instead:

```bash
EPICS_MCP_REQUIRE_DISPLAYS=1 uv run pytest        # engine missing -> refusal, not a silent skip
```

Use it whenever a change touches the display-aware tools, so a half-installed checkout cannot hand
you a green run. Why CI itself still cannot execute them, and where their coverage comes from
instead since 2026-08-04: `docs/known-limits.md`, entry 16.

## Definition of done

- `uv run pytest` green and coverage not reduced.
- `uv run pre-commit run --all-files` green (ruff, ruff-format, **mypy --strict**, the no-secrets
  guard, and the two prose guards below).
- **English, and plain punctuation.** This repository is written in English, with ASCII
  punctuation; `scripts/check_language.py` and `scripts/check_typography.py` enforce both on every
  text file. If a hook blocks you: translate the line rather than deleting the reasoning, and
  replace the character rather than reaching for the exception file. Exceptions exist for text
  where the German or the character IS the subject (a casefold fixture, git's pathspec separator),
  are keyed on a line fragment rather than a line number, and go stale loudly. What the two guards
  cannot see is written down in [docs/known-limits.md](docs/known-limits.md) sections 9 and 10.
- New behaviour has a test; new tools/config are documented in `README.md` (the resource
  URIs are drift-checked against the server by a test).
- **Changed** behaviour of an existing tool carries the same documentation duty as a new one, and
  it is the easier one to miss: a stale sentence about a tool that still exists reads as current.
  Enumerate the surfaces with `git grep` over the tracked tree, do not recall them, and include the ones that TEACH a
  call (`src/epics_mcp/prompts.py`) rather than describe one. Rationale and the measured miss:
  `CLAUDE.md`, "Definition of Done (doc-sync)".
- New operational knowledge (a service/IOC recipe, an endpoint, an error signature) lands in the
  operator guide (`src/epics_mcp/operator_guide.md`), not just a commit body. See the Knowledge
  Persistence Policy in `CLAUDE.md`. A new tool must appear in the guide's tool inventory, any new
  `EPICS_MCP_*` var must be mentioned there, and a new `epics-doctor` plane status must be **given a
  row in the shipped legend**, with the glyph the CLI prints, and sorted into the matching
  guide-location bucket. ⚠️ Leaving it undocumented is not an option the buckets offer any more:
  "named in neither" is empty since QA-49 and putting a status there is a red test, not a decision.
  A status the code treats as a hard failure must additionally stay named in the prose paragraph
  above the legend. A new `epics-doctor` **plane** must be named in the guide's `plane-inventory`
  region, in the spelling the report prints, which is a separate obligation from the status one: a
  status is what a line SAYS, a plane is what it is CALLED, and only the first was guarded until
  QA-73. All of it is drift-checked by `test_guide_matches_code.py`.
- A new **write** surface additionally satisfies all six points of the
  [write-gate contract](docs/write-gate-contract.md)
  and registers every one of its deny paths in `tests/test_write_gate_contract.py::DENY_PATHS`, each
  one red-provable on a mutant. The drift guard there discovers gate modules by scanning, so a new gate
  fails that test until it is registered; registering it is the work, not a formality.
- **Two guards can fail on a change that looks unrelated. Both are deliberate, and neither is
  satisfied by editing the number they report.**
  - *A number written next to a collection noun* ("the 26 projection fields", "all 22 schemas",
    "four paths") in `tests/test_server.py`, `server.py`, `services/checkers.py`,
    `services/checkers_olog.py` or `tools/archiver.py`. **Which nouns count is a closed list,
    `prose_numbers.COLLECTION_NOUNS`**. Read it, do not guess, and note that this obligation is
    NOT the same on both sides of it:
    - Noun **in** the list: the detector finds the phrase, so all three routes are open: derive it
      in `tests/test_prose_counters.py::_CLAIMS`, or list it in `_FROZEN` with a written reason, and
      re-pin that file's phrase count either way.
    - Noun **outside** the list ("modes", "halves", "planes"): the detector never sees the phrase,
      so only a `_CLAIMS` entry works: a claim is matched against the text directly. An `_FROZEN`
      row for it goes **red** (`test_every_frozen_entry_still_exists` finds no such site), and so
      does re-pinning the count (nothing was added to count). Both were probed. Adding the noun to
      `COLLECTION_NOUNS` is the other legitimate answer, and it is a bigger change: it re-opens the
      whole watched estate and will surface phrases that then need rows.

    Rewording a sentence means updating its pattern, not its figure. A new claim must also declare
    what it READS (`_Claim.reads`, keyword-only and required); that declaration is traced, so it
    has to be true rather than plausible.
  - *A new test that installs a client class double in its own body* moves the audited population
    and reddens `tests/test_client_edge_guards.py::test_the_ast_derivable_audit_figures_are_pinned`.
    Re-measure with `uv run python scripts/guard_audit.py sham --check`. That verdict was reached
    by a person reading a list, so a longer list has not been read: re-read it, then re-record.
- **Periodically, and after any change to a client module or a client-double test:** re-record the
  coverage-dependent half of the sham audit. It is not in the gate because it costs a full
  `COVERAGE_CORE=ctrace` suite run, so it only happens if someone does it deliberately:
  `uv run python scripts/guard_audit.py sham --check --coverage-db <db>`, recipe in
  `scripts/guard_audit.py`. Without it, two of that audit's four figures are recorded and unverified.
  ⚠️ Point `COVERAGE_FILE` at a path **outside the repository** while recording, or the run
  overwrites the working `.coverage`. And record with `COVERAGE_CORE=ctrace`: on Python 3.12+ the
  default core disables a location after its first observation, so a map recorded without it makes
  the audit report false survivors; the reasoning is in `CLAUDE.md`'s evidence section.

- **What this repository deliberately does NOT guard** is written down and dated in
  [docs/known-limits.md](docs/known-limits.md), which carries the limits a reader of this project
  needs. Read it before building a guard for something: several of the obvious candidates were
  measured and rejected for a reason, and the reason is there rather than in a commit body.
- **A limit of ONE guard belongs in that guard's docstring, not on that page.** The distinction is
  who has to read it. A limit an adopter or a fork contributor can act on goes on the page; a limit
  only the next author of a particular guard can act on goes beside the assertion it qualifies,
  where that author will meet it. The page grew to 841 lines before this rule existed, and half of
  it was a maintenance manual for `tests/test_guide_matches_code.py`. Retired entries keep their
  numbers, which are never reused, so a citation by section number does not rot.

## Releasing

Publishing fails expensively and exactly once: a package index never lets a version number be
reused, so a wrong upload cannot be withdrawn, only superseded. Hence the order below, and hence
the gates.

**Already done, kept because the reasoning still binds:**

1. The display question. The four display-aware tools depend on `opi_navigation`, which is in a
   private repository. The current answer is **not yet**, with a named condition: revisit once the
   Java live plugin and the CS-Studio MCP have been tested in practice. Until then the engine stays
   a dependency group and the published package does not offer it. The same note sits in
   `pyproject.toml` next to the group, so neither copy is the only one.
2. The **Trusted Publisher** for `epics-mcp` is registered: repository `epicDirk/EPICS-MCP`,
   workflow `publish.yml`, environment `pypi`. Trusted Publishing uses OIDC, so no API token is
   ever stored in this repository. It is proven end to end: the `v0.3.0` tag published 0.3.0
   through it (run 30402266430).
3. The rehearsal path works too. `workflow_dispatch` with `dry_run` at its default `true` installs,
   tests, clears `dist/`, builds and gates, and stops before uploading; it is the only way to
   exercise a release workflow without releasing. ⚠️ **One step has still never executed**: the
   rehearsal skips the publish job, so `download-artifact` has never run. The upload path is
   therefore proven by the real 0.3.0 release, not by a rehearsal.

**Every release:**

1. Bump `[project].version` in `pyproject.toml` **and** the hardcoded fallback in
   `src/epics_mcp/__init__.py`. This is not optional bookkeeping: with no installed metadata
   (a source checkout) the stale literal becomes a silent version lie, and
   `tests/test_packaging.py::test_version_fallback_matches_pyproject` goes red if they drift.
2. Close the `[Unreleased]` section in `CHANGELOG.md` under the new version.
3. Run the gates one more time, then rehearse: `rm -rf dist && uv build && uv run python
   scripts/check_release_ready.py dist/*`. The gate reads the BUILT metadata, never
   `pyproject.toml`, because the two can differ; it refuses a direct reference, a pre-release
   version, and a description that would not render.
4. Tag `vX.Y.Z` and push the tag. That is what triggers the upload, and it is the irreversible
   step: from here the version number is spent whatever happens next.
5. Afterwards: check that the index page renders and that a clean install of the new version
   imports and runs its commands. The README install command and the index badge are already
   pointing at the published package since 0.3.0, so there is nothing to swap. The **GitHub
   release** needs no hand step: the workflow's third job creates it after a successful upload,
   with the body sliced out of `CHANGELOG.md` by `scripts/changelog_section.py`. Check it appeared;
   if the section was missing the job fails loudly rather than publishing an empty release.

**Three traps, all measured here rather than imagined:**

- `uv build` does **not** clear `dist/`. A three-week-old artifact was still sitting there during
  this work, carrying a dependency reference the current build no longer has, and `dist/*` would
  have uploaded it alongside the new files. The workflow clears the directory; a manual release
  must too.
- A published package is not the same thing as a working one. Installing the built wheel into a
  clean environment and running every console script found two that died on their import chain
  with a bare traceback. Do that check by hand whenever an entry point or an optional dependency
  changes; no unit test replaces it.
- **The `v` in the tag is load-bearing, and this repository's own history proves it can be
  forgotten.** `publish.yml` triggers on `v*`; the 0.2.0 tag is named `0.2.0`, without one. A tag
  pushed as `0.4.0` would therefore start nothing at all: no build, no upload, no release, and no
  error either, because nothing ran to fail. Tag `vX.Y.Z`.

## Live / sandbox tests

Tests that need a running EPICS stack are opt-in and skip by default, so the standard run
and CI stay green without any infrastructure.

## Code style

Small, focused, deterministic functions; speaking names; comments explain *why*. The
layering contract is `server → tools → services → clients` (see `ARCHITECTURE.md`); keep
dependencies pointing downward.

## Commits

Conventional-style prefixes (`fix:`, `feat:`, `refactor:`, `docs:`, `test:`), one logical
change per commit.

House style, written down so a newcomer does not break it by accident:

- **English**, both subject and body.
- **A scope** in the prefix where one applies: `feat(channelfinder):`, `test(schemas):`,
  `fix(audit):`, `docs(changelog):`.
- **Plain ASCII in the subject line**, so it survives any terminal encoding.
- The **work-item id** in parentheses at the end of the subject when there is one: `(S29)`.
- A body that carries the **measured** facts (gate counts, red-proof node ids, what was *not*
  done and why) rather than a restatement of the diff.
