# Contributing

Thanks for your interest. This is a pre-1.0, work-in-progress project. Issues and small
PRs are welcome.

## Organization

What a clone contains, with only the entries whose purpose a directory name does not already give
away. (What an INSTALL contains is a different question, answered by the layout in
[docs/deployment.md](docs/deployment.md); the two barely overlap.)

```text
    EPICS-MCP/
         |
         + src/epics_mcp/         the package. Layering is server -> tools -> services -> clients,
         |    |                   described in ARCHITECTURE.md rather than repeated here
         |    - operator_guide.md NOT documentation about the code: it is package DATA, shipped in
         |                        the wheel and served to an assistant as epics-pv://guide, with
         |                        OPERATING.md as its human mirror. One file, three consumers
         + tests/                 the suite, including the drift guards that hold prose against
         |                        code. Most of what surprises a newcomer is enforced from here
         + scripts/               gate code run by pre-commit, not a user-facing surface
         + docs/                  the reference pages the README indexes
         + examples/              a sample display, an mcp.json, a test.db needing EPICS Base
         + .github/               CI, the release workflow, issue and PR templates
         - .env.example           the canonical commented list of every EPICS_MCP_* variable
         - CLAUDE.md              standing policy for an AI assistant working in this repository
```

⚠️ **`sandbox/` is git-ignored and exists in no clone.** It is a local EPICS stack (an IOC and the
REST services) that the opt-in live tests are pointed at through environment variables. Several
places assume you know that, including the live-test checkbox in the pull-request template, so
treat a reference to "the sandbox" as meaning a stack you supply yourself. It is unrelated to the
`sandbox` PRESET, which is a deployment shape that needs no infrastructure at all.

## Development setup

The gate chain is [uv](https://docs.astral.sh/uv/)-based:

```bash
uv sync --extra dev --group displays --locked  # full local install (toolchain + display engine)
uv run pytest                                  # run the test suite
uv run pytest --cov=src --cov-branch           # with coverage
uv run pre-commit run --all-files              # ruff + format + mypy --strict + guards
uv run pre-commit install --hook-type commit-msg   # ONE-OFF, see below: the message guard
```

⚠️ **The last line is a one-off per clone, and nothing can do it for you.** Six hooks run over
FILES at commit time; a seventh scans the commit MESSAGE, which is a leak surface no file guard can
reach (real device names have reached commit bodies on this public repository before). Its wiring
lives in `.pre-commit-config.yaml` and a test holds that wiring, but `.git/hooks/commit-msg` is
per-clone state outside the tree, so **a fresh clone has no message guard until this command is
run**. Verify with `ls .git/hooks/commit-msg`. What it refuses, and what it deliberately does not
look at, is documented at `scripts/check_commit_message.py`.

**Extra vs group.** `dev` is the only published extra and carries the toolchain. The
`opi_navigation` PV engine, which the display-aware tools need, is a **dependency group**
(`--group displays`), not an extra, and the distinction is load-bearing: a group never reaches
the published metadata. The engine lives in a private repository, so an outside user cannot
install it by any route; as an extra it both advertised a promise nobody could keep and, being
a direct git reference, made the package unpublishable, which is the single thing PyPI refuses
outright.

⚠️ **Install that group with uv, or name the source yourself.** What ties `opi-navigation` to the
private repository is `[tool.uv.sources]`, and that table is uv-only, resolution-time information:
pip does not read it, and it is not published metadata. So `pip install --group displays` resolves
the bare name from the public index, where it is **not reserved** (measured 2026-08-15: both
spellings answer 404). Today that fails honestly; the day somebody claims the name, it succeeds
with a stranger's code. The reasoning, and the guard that keeps a second group member from
repeating it, sit at `[dependency-groups]` in `pyproject.toml`.

CI runs `uv sync --extra dev --locked` and passes no `--group`, so it tests exactly the
standalone core a public user gets. The `opi_navigation`-coupled test modules are dropped at
collection when the package is absent, so the core suite stays green.

**What CI covers, since the README used to say it and a landing page is the wrong place for it.**
The suite runs on every push against Python 3.12, 3.13 and 3.14, which is every version
`requires-python` permits, on an install with no EPICS infrastructure at all. `mypy --strict`
covers `src`, `tests` and `scripts`, and the package ships `py.typed`.

⚠️ **The sentence that moved here carried a number, and the number was wrong**: it said seven
display-coupled test modules where `ENGINE_COUPLED_MODULES` in `tests/conftest.py` lists eight. It
is not repaired to eight, it is gone. `tests/engine_gate.py` had already written down why ("a
number repeated beside a list it does not read is how this paragraph drifted in the first place"),
and `docs/known-limits.md` entry 16 names the same set deliberately without one.

**`--locked`, not `--frozen`, and the local line above says so too.** Both install from the
lockfile without re-resolving; only `--locked` also verifies that the lockfile still MATCHES
`pyproject.toml`. Under `--frozen` an edit committed without its `uv lock` installs the old set in
silence. The two commands were deliberately made the same so that a divergence fails on your
machine rather than on a runner, and, since the release build uses `--locked` as well, rather than
on the tag.

**That drop is no longer silent (GB-27).** A green report over a hundred tests that never ran is
indistinguishable from a full one, so a run that drops those modules now says so in its report
header, and a run that DEMANDS them refuses instead:

```bash
EPICS_MCP_REQUIRE_DISPLAYS=1 uv run pytest        # engine missing -> refusal, not a silent skip
```

Use it whenever a change touches the display-aware tools, so a half-installed checkout cannot hand
you a green run. Why CI itself still cannot execute them, and where their coverage comes from
instead since 2026-08-04: `docs/known-limits.md`, entry 16.

## Definition of done

- `uv run pytest` green. **"Coverage not reduced" is enforced since GB-34, not remembered:** CI
  fails below a floor, `--cov-fail-under` in `.github/workflows/ci.yml`, on each of the three matrix
  legs. Until then this line asked for something no mechanism checked.
  ⚠️ **That floor is the CI number and is therefore LOWER than a local one.** CI syncs without
  `--group displays`, so the eight engine-coupled test modules never run there and their lines count
  as missed (`docs/known-limits.md`, entry 16). Measured 2026-08-20 on the same tree: 87% in CI
  against 94% locally with the displays group installed. Do not derive the floor from your own run.
  Locally: `uv run pytest --cov=src --cov-branch` and read the TOTAL line. The floor is a floor, not
  a target, and it moves up only.
- `uv run pre-commit run --all-files` green (ruff, ruff-format, **mypy --strict**, the no-secrets
  guard, and the two prose guards below). ⚠️ That command drives the pre-commit stage only, so it
  never exercises the **commit-message** guard; that one runs when you commit, and only in a clone
  where `pre-commit install --hook-type commit-msg` has been run. Its behaviour is pinned by
  `tests/test_commit_message_guard.py` instead, which is what keeps CI able to check a hook CI does
  not run.
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
    `services/checkers_olog.py`, `tools/archiver.py` or, since GQ-123, the SHIPPED
    `src/epics_mcp/operator_guide.md`. The criterion that decides that list, and the arithmetic
    that keeps two large test modules out of it, is written above `_WATCHED`.
    **Which nouns count is a closed list,
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
    and reddens
    `tests/test_guard_audit_cli.py::test_check_without_a_database_agrees_and_names_what_it_could_not_reach`,
    which drives the real CLI and therefore fails on any `PINNED_AST` figure moving, and on any
    figure being measured that nothing pins. Re-measure with
    `uv run python scripts/guard_audit.py sham --check`. That verdict was reached
    by a person reading a list, so a longer list has not been read: re-read it, then re-record.
- **After any change to a client module or a client-double test:** the coverage-dependent half of
  the sham audit is re-checked for you. Since GB-34 the `sham-audit` job of
  `.github/workflows/ci.yml` records a `COVERAGE_CORE=ctrace` map on every push and runs
  `guard_audit.py sham --check --coverage-db`, so all six figures are verified rather than four.
  It still costs a full extra suite run, which is why it is a parallel JOB and not a step in the
  gate, and why the local chain does not run it.
  ⚠️ **It reports, it does not block:** main's ruleset holds `non_fast_forward` and `deletion` only,
  so no required status check stands in front of a commit. Read the run.
  Locally, when you want the same answer before pushing:
  `uv run python scripts/guard_audit.py sham --check --coverage-db <db>`, recipe in
  `scripts/guard_audit.py`. Run it without a database and it says so itself: four pins checked, two
  named as needing one.
  ⚠️ Point `COVERAGE_FILE` at a path **outside the repository** while recording, or the run
  overwrites the working `.coverage`. And record with `COVERAGE_CORE=ctrace`: where the default core
  is `sys.monitoring` it disables a location after its first observation, so a map recorded without
  it makes the audit report false survivors; the reasoning is in `CLAUDE.md`'s evidence section.
  ⚠️ **"On Python 3.12+" is what this line used to say, and it is wrong by two minor versions.**
  Measured against the locked coverage 7.14.1: `coverage.env.SYSMON_DEFAULT` is true only from 3.14,
  so on 3.12 and 3.13 the map is a C-tracer map with or without the variable. Setting it is still
  right, and the CI job sets it, because the job's interpreter is a configuration choice that can
  change under the recipe.

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
   exercise a release workflow without releasing. ⚠️ **What a rehearsal cannot cover, stated
   precisely because this paragraph used to overstate it.** It stops before the publish job, so
   `download-artifact` and the upload never run under it, and it takes the LENIENT gate branch
   (`--allow-prerelease`); the strict `--expect-version` branch runs on a tag alone. So a green
   rehearsal says nothing about either. What covers them is the real releases, and that coverage is
   measured rather than assumed: run `31765446620` (the `v0.6.0` tag, 2026-08-14) is the most recent
   one whose `publish` job ran `download-artifact` and uploaded, and whose `github-release` job then
   sliced the changelog successfully. Older tags did the same; the earlier claim that
   `download-artifact` "has still never executed" was already false when it was written, and had
   been since `v0.3.0`. ⚠️ A superlative like "the most recent" goes stale on its own, without
   anyone editing it: this line named `v0.5.0` for twelve hours after `v0.6.0` had shipped. Re-read
   it whenever a release goes out, or measure it with
   `gh run list --workflow Publish --json databaseId,headBranch,event`.

**Every release:**

1. Bump `[project].version` in `pyproject.toml`, the hardcoded fallback in
   `src/epics_mcp/__init__.py`, **and `uv.lock`** (run `uv lock`, do not hand-edit). The first two
   are not optional bookkeeping: with no installed metadata (a source checkout) the stale literal
   becomes a silent version lie, and
   `tests/test_packaging.py::test_version_fallback_matches_pyproject` goes red if they drift.
   ⚠️ **The third is newly load-bearing and this step did not name it before.** `uv.lock` carries
   the project's own version (`[[package]] name = "epics-mcp"`), so a bump leaves it stale, and
   both workflows now sync with `--locked`, which REFUSES a lockfile that disagrees with
   `pyproject.toml`. Under the previous `--frozen` this passed unnoticed. The trap is that the
   local gates hide it: `uv run` refreshes `uv.lock` on disk without committing it, so step 3 goes
   green on your machine and the tag build fails, after the tag is already pushed. `git status`
   after `uv lock` is the cheap check.
2. Close the `[Unreleased]` section in `CHANGELOG.md` under the new version.
3. Run the gates one more time, then rehearse: `rm -rf dist && uv build && uv run python
   scripts/check_release_ready.py dist/*`. The gate reads the BUILT metadata, never
   `pyproject.toml`, because the two can differ; it refuses a direct reference, a pre-release
   version, and a description that would not render.
4. Tag `vX.Y.Z` and push the tag. That starts the build. ⚠️ **Since 2026-08-14 the tag is no longer
   the irreversible step, and this line used to say it was.** Two repository settings sit in front
   of the upload now, both outside this tree, so neither shows up in a diff:
   - Only a repository **administrator** can create a `v*` tag at all (ruleset `release-tags`).
   - The `publish` job then **waits for an approval** on the `pypi` environment. Approve it under
     the run's own page, "Review deployments". The version is spent from that click onwards, not
     from the tag push.

   If the approval is never given, the run expires after GitHub's pending-deployment limit and the
   `github-release` job is skipped: the tag exists, the index has nothing, and the Releases page
   disagrees with the tag. The way back is to re-run the whole workflow from the same tag and
   approve it, not to push a second tag.
5. Afterwards: check that the index page renders and that a clean install of the new version
   imports and runs its commands. The README install command and the index badge are already
   pointing at the published package since 0.3.0, so there is nothing to swap. The **GitHub
   release** needs no hand step *after the approval in step 4*: the workflow's third job creates it
   once the upload succeeded, with the body sliced out of `CHANGELOG.md` by
   `scripts/changelog_section.py`. Check it appeared; if the section was missing the job fails
   loudly rather than publishing an empty release.

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

**Without a stack**, which is the normal case and the one CI is in, there is nothing to do: they
skip, `uv run pytest` is the whole contract, and no contribution is blocked by their absence. The
one thing to know is that a green run therefore says nothing about the code paths that only a real
service exercises, which is why the repository asks for a live probe before a behaviour is
DOCUMENTED as certain (see `CLAUDE.md`, "no promise before a differential live probe").

**With a stack of your own**, point them at it and they run. Selection is `-m live`, and the
targets come from `EPICS_MCP_LIVE_*` variables, one per thing a test has to be told rather than
discover: which PV to read (`..._ARCHIVER_PV`, `..._ALARM_PV`, `..._ALARM_CONFIGURED_PV`), which
name pattern is safe to search (`..._CF_GLOB`, `..._ARCHIVER_GLOB`), which alarm tree and naming
device to ask for (`..._ALARM_TREE`, `..._NAMING_DEVICE`), whether the archiver is expected to be
ingesting (`..._DOCTOR_INGEST`), and, for the write gate, the PV to write, the value, an
out-of-range value and a logbook that must be REFUSED (`..._WRITE_PV`, `..._WRITE_VALUE`,
`..._OUT_OF_RANGE_VALUE`, `..._OLOG_DENY_LOGBOOK`). They are deliberately absent from
`.env.example`, which documents the SERVER, not the suite; the test module that reads each one is
the place its meaning is written down.

⚠️ A live test that writes owns its target and never derives it. The deny logbook above is named
explicitly for that reason: a test that picked one from the server's own answer once resolved onto
a colleague's fixture logbook and left entries in a service with no delete.

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
