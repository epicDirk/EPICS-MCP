# Known limits: what this repository does not guard

**What an entry IS:** a measured limit of a guard this repository runs, plus the tempting repair
that was probed and REJECTED rather than merely postponed. **What it is NOT:** a work journal or a
QA history. Each entry names the measurement that produced it, so a reader can re-run it instead of
trusting it.

**Treat every figure as "was true when written."** Markdown is unguarded here, which is the first
entry, so nothing re-runs a number on this page. Where a figure exists to establish a SHARE, the
share is what this page states, together with the rule to re-derive it.

**Numbers are permanent identifiers, not positions.** A retired entry's number is never reused, so
a citation elsewhere in this repository still means what it meant when it was written. What was
retired, and where it went, is listed at the end.

**On this page:** [1 markdown prose](#1--markdown-prose-is-unguarded-and-a-guard-over-it-was-rejected) ·
[9 the language guard](#9--the-language-guard-finds-german-by-vocabulary-not-by-understanding) ·
[10 the typography guard](#10--the-typography-guard-forbids-a-named-set-not-typography-in-general) ·
[11 Naming-client provenance](#11--the-naming-service-clients-provenance-is-a-measurement-not-an-opinion) ·
[13 doctor remedies](#13--a-remedy-is-checked-for-being-present-not-for-being-right) ·
[14 the status legend](#14--the-shipped-status-legend-is-guarded-by-name-glyph-and-set-not-by-what-it-says) ·
[15 the https write path](#15--the-remote-https-write-path-has-no-live-probe-any-more-only-in-memory-coverage) ·
[16 CI and the display tests](#16--ci-cannot-run-the-display-coupled-tests-and-no-switch-inside-this-repository-changes-that) ·
[17 the service-URL redaction](#17--the-service-url-redaction-removes-a-userinfo-not-every-secret-a-url-can-carry) ·
[18 one line is not one field set](#18--the-audit-record-is-one-line-which-is-not-the-same-as-one-set-of-fields) ·
[19 the repository posture is unguarded](#19--the-repository-side-release-protections-are-unguarded-and-a-guard-over-them-was-rejected) ·
[retired](#retired-entries-and-where-they-went)

---

## 1 · Markdown prose is unguarded, and a guard over it was rejected

The prose-counter guard reads Python comments and docstrings. It does not read markdown files, so
no figure written on any page in this repository is re-run by anything.

Why no guard: size-naming phrases are spread across the tracked markdown, and the largest share of
them sits in `CHANGELOG.md` and `CLAUDE.md`, where the exemptions would have to live. A release
entry that said "22 schemas" must keep saying it. That would need roughly twenty "historical, not
derivable" rows, precisely the construction `tests/test_prose_counters.py` already rejected in
writing for its own two files, as "a blanket exemption wearing a table's clothes". Re-derive the
share with the detector's own vocabulary (a number paired with a `prose_numbers.COLLECTION_NOUNS`
member, or the `N of the M` shape) over `git ls-files "*.md"`.

⚠️ The one inventory worth knowing about: `docs/tools.md` lists every tool, and **nothing compares
that list to the registrations.** Only the operator guide's inventory block is checked
(`test_guide_matches_code`). What is checked on `docs/tools.md` is narrower than its tool table and
touches none of it: the resource URIs (`test_readme_resources`), that no page advertises an
`epics-*` command the package does not install (`test_examples_match_entry_points`), that its
"part of the core install" sentence names the same commands as the engine gate
(`test_cli_without_display_engine`), and that every status name it pairs with a glyph uses the
glyph `cli_doctor` renders (`test_guide_matches_code`, which names this page explicitly so a rename
cannot drop it out of the scan). SIX test modules read the file, measured with
`grep -rln 'tools\.md' tests/`, and not one of them reads the tool table. It is a
second inventory of the same thing with one of them guarded.

## 9 · The language guard finds German by vocabulary, not by understanding

**Measured 2026-07-28.** `scripts/check_language.py` decides per line from three independent
signals: a closed list of German function words with no English homograph, umlauts and the eszett,
and German word-formation suffixes.

What it cannot see, stated because a guard's reach invites over-reading:

- **A German sentence built entirely from words none of the three signals know.** The lists are
  closed by design (one hit blocks a commit, so a wrong entry is expensive), which buys a
  false-positive rate of zero on the current tree at the cost of an unknown false-negative rate.
  Nothing measures the latter, and nothing can without a labelled corpus.
- **Whether English prose is GOOD English.** The guard answers "is this German", never "is this
  well written". Neither does anything else here.
- **Any language other than German and English.** The signals are German-specific.
- **A German word inside an ALL-CAPS run.** Discarded on purpose: the licence name is spelled the
  same as a German preposition, so without that rule the guard reports the MIT licence six times.
  German prose does not capitalise its function words, so the trade is cheap, but it is a hole.

And one class it flags although it should not, known and accepted rather than repaired
(QA 2026-07-28, measured with `german_signals` on each example): **an English proper noun that is
spelled like a German function word.** "the von Neumann architecture", "the Weil pairing" and
"Des Moines" each trip the word signal, because the ALL-CAPS discard does not cover Title-Case.
The mechanical repair, discarding a lowercase `von` before a capitalised word, would blind the
guard to most genuine German (German nouns ARE capitalised), so the residual stays and the
per-line exception file is the intended relief; this very paragraph needed those exceptions, which
doubles as the end-to-end proof of the mechanism. Two sibling classes from the same audit ARE

⛔ The tempting repair, a general language-detection library, is rejected rather than postponed: a
statistical detector on a 100-column line of technical English with identifiers in it is noisy in
exactly the direction that makes a commit hook unusable, and the failure would be a false BLOCK,
which is worse here than a false pass. The honest position is that this guard makes the 2026-07
sweep hard to undo, not that it proves the repository is English. The original umlaut grep declared
the tree English and had missed more than half of what the three signals find, which is why a
single-signal guard is not enough.

## 10 · The typography guard forbids a named set, not typography in general

**Measured 2026-07-28, re-measured 2026-08-02.** `scripts/check_typography.py` blocks nine things:
the em dash, the en dash, the ellipsis, five curly quotes and the doubled hyphen. The doubled
hyphen counts at the START and at the END of a line as well as between two spaces, because a line
edge is the same context a space is, and it is where a dash lands when a sentence wraps. It is
deliberately NOT extended to a tab neighbour.

⚠️ Four legitimate forms fall under that widening. None occurs in the tracked tree today, and each
is a case for an exception entry rather than for narrowing the rule again: git's pathspec separator
where it ends a command line, the pair as a comment prefix in column 0 (SQL, Lua, Ada, Haskell, and
the hook has no file-type filter), the RFC 3676 signature delimiter, and a Setext heading underlined
with a pair.

It does **not** know any other typographic character (a minus sign, a non-breaking hyphen, a figure
dash), and it deliberately tolerates the typographic apostrophe, the arrow and the multiplication
sign, which carry meaning no ASCII substitute holds. Nor does it see a run of three or more standing
in for a dash, a tab or a non-breaking space as the neighbour, or the pair glued inside a word. An
exception frees the whole LINE rather than one character on it.

⚠️ Do not assume ruff covers any of this. Probed one at a time through
`ruff --isolated --select RUF001,RUF002,RUF003`, it recognises five of the nine characters this
repository once listed in `allowed-confusables` (the en dash, the typographic apostrophe, the
multiplication sign and both single curly quotes) and nothing at all outside `.py`. That set is not
the nine this guard blocks, which is the list below.
The em dash, the three double curly quotes and the ellipsis produce no finding in a string, a
docstring or a comment alike.

## 11 · The Naming-Service client's provenance is a measurement, not an opinion

Measured 2026-07-29, and recorded here because it is the first question a public fork asks.
`services/naming_client.py` and `services/naming_exceptions.py` used to describe themselves as
"vendored" from pvValidator, an unrelated validation tool that is **GPL-3.0-only** while this
package is MIT. Read on its own, that self-description states a licence problem. It was wrong.

What was measured, on the upstream checkout and on this tree. **No contiguous block of code is
shared:** longest identical run of lines, `difflib` over the whole files, is four lines in
`naming_client.py` of which two are non-blank, and those two are `import requests` and
`from urllib.parse import quote as url_quote`; `naming_exceptions.py` shares two lines of which the
one non-blank is `"""`. The remaining shared lines are class and method signatures the REST endpoint
dictates, which is API shape rather than code. The exceptions module is not slimmed from anything:
upstream derives its errors from its own root, ours from `services/rest_exceptions.py` like the
other four REST planes, and `NamingServiceNotFound` exists only here.

The honest limit: this compares two files at one point in time. It says nothing about what a court
would say and it is not a licence review. It is enough to establish that the modules were
mis-described, which is what the wording now reflects.

## 13 · A remedy is checked for being PRESENT, not for being right

`epics-doctor` appends a remedy to every status that reports a problem, and
`test_every_problem_status_names_a_remedy` proves that each has one, that it is not empty, and that
it opens with an imperative. None of that says the advice is CORRECT, or still correct: a remedy
naming a variable that gets renamed, or pointing at a section of `docs/deployment.md` that moves,
goes stale in silence. Two of the seven texts are backed by that file, the other five by the probe
that produces the status, and nothing re-checks either link.

Measured: the guard survives replacing any remedy with a different, equally imperative sentence, and
no test reads a documentation path out of a Python string. Deliberate. A guard over the WORDING
would pin prose that has to be free to improve, which is the trade entry 1 already describes. The
check is the review.

## 14 · The shipped status legend is guarded by name, glyph and set, not by what it says

`tests/test_guide_matches_code.py` holds the operator guide's status legend against `PlaneStatus`
and `cli_doctor._STATUS_MARK`, in both directions and as sets. Every glyph and status pairing on the
guide and on every tracked page under `docs/` has to agree with the mark the CLI prints. Every
lower-case identifier written as a code span in the guide's status paragraph has to be a plane
status. The plane NAMES the report prints are held separately (a status is what a line SAYS, a plane
is what it is CALLED), by two guards: the guide's `plane-inventory` region against a real
`run_doctor` run, and the plane-name literals in `services/doctor.py` against that same run rather
than against the guide, because a literal nobody calls is a dead branch and reporting it as a
documentation gap would be a false alarm on the wrong surface.

What is NOT held, named rather than implied:

- **The Meaning column is not read.** A row may describe its own status wrongly and stay green,
  measured by giving one row another row's meaning. Rejected rather than postponed, for the reason
  entry 13 gives about the remedy texts: a guard over the wording pins prose that has to stay free.
- **A pairing needs both halves side by side**, in one of exactly two written forms: two backticked
  tokens in the same block separated by at most three characters, none of them a word character, or
  exactly two whitespace-separated tokens inside ONE backticked span. Whitespace collapses first, so
  a line break counts as one character and only a blank line separates two blocks. A status and a
  mark further apart, either half written without backticks, or a span of some other token count, is
  not read. ⚠️ This page is itself a scanned surface, so an illustration here must not put a
  single-character code span within three characters of a backticked status name.
- **The per-surface floor detects an EMPTY read, not an almost-empty one, and it covers three of the
  eight surfaces read.** A page that stops documenting all but one of its pairings passes. The
  tempting repair, a floor of "at least one pairing outside the legend region", is green today and
  was rejected because a legitimate prose rewrite reddens it and the obvious repair for that red is
  to delete the floor, which is green and retires it.
- **A page is covered the day it is TRACKED, not the day it is written.** The population comes from
  `git ls-files`, so a page sitting unstaged is not read at all and a wrong glyph in it passes the
  local run until the `git add`. Tracked markdown outside `docs/` is not read either. Widening to
  every tracked page is green today and is rejected because release history is historical by policy,
  so a retired mark would force a red whose only repair is rewriting a published entry.
- **This is not a markdown parser.** It holds the delimiter rule, the blank-line rule, the
  indentation limit (in COLUMNS, because a tab advances to the next multiple of four), the
  delimiter's leading-whitespace rule and the row width, which is what the measured breakages
  violate; the rest of the GFM table grammar stays outside. A renderer was probed and rejected on
  two grounds: it would make a guard's verdict depend on a third-party version, and the only one in
  the tree arrives transitively rather than declared. Two exotic spellings still read as a table
  here and as none for a reader; both repairs for them cost more false reds than they close.
- **No gate sees a pin being DELETED.** pytest reports an emptied test body as passed and the lint
  rules here carry no flake8-pytest checks. A structural pin holds each guard's asserted NAMES
  against a declaration, but it compares declared against found and has no reverse direction, so a
  guard added without a row is unfloored and nothing says so.

⚠️ **"Every status is explained" is not "every LINE is explained", and that is the open gap.** The
totality guard proves that `PlaneStatus` is a subset of the legend. The report has four other
parts, and measured 2026-08-02 not one of their fixed strings appears in the shipped guide: the
header line, the privacy block with its two allowlists, the verdict in all six of its branches, and
since 2026-08-13 the write-gate block (which the guide describes in prose without quoting a line of
it, the same trade as the other three).
The verdict is the last line an operator reads. It was left open deliberately: a mark is a
CHARACTER and cannot be guessed, which is why it needed a legend, whereas the verdict and the
privacy block are English sentences that state their own meaning, and the semantics behind them are
already in the guide in two places. Documenting them would buy a reader the ability to FIND the
wording, not to understand it.

⚠️ Further plane orderings exist elsewhere and are held against nothing. No count is given, because
the first attempt at one was measured too narrowly. Enumerate instead: `git grep` for `plane` over
the tracked markdown and the package sources. On 2026-08-02 that found orderings in `README.md`,
`ARCHITECTURE.md`, `docs/deployment.md`, `prompts.py` and two more inside `presets.py`. None is
wrong for its own purpose, and unifying them is a separate cut.

## 15 · The remote-https write path has no live probe any more, only in-memory coverage

Measured 2026-08-01. Commit `c05a93f` deleted `tests/test_olog_remote_https_live.py`, and the
removal plan named it "the live test module of the loopback boundary". That name understated it:
the module verified the REMOTE-HTTPS write path against a local self-signed TLS proxy, with both
directions of one claim. An upload through the full gate over an https URL succeeds because the CA
comes from `EPICS_MCP_CA_BUNDLE` via the config, and the SAME upload without that bundle fails TLS
verification. Its bytes were then read back and compared, so "the https upload stored them" was a
measurement rather than a 2xx.

Why the deletion still stands: the module could only prove the round trip by reading the uploaded
bytes back through a second client, and that read leaned on a posture that no longer exists.
Rebuilding it is a rewrite, not a revert.

What covers the path today, and what does not. `tests/test_http.py` holds the pieces in memory:
that a configured CA bundle is applied and `trust_env` pinned off, that an empty bundle falls back
to certifi with `trust_env` left on, and that the write session is built without env influence.
Those are the decisions the code makes. **What nobody checks any more is that the assembled session
then completes a real TLS handshake against a server presenting that CA**, which is the one thing a
mock cannot fake. A `verify` argument that stops reaching requests, or a session that silently falls
back to the system store, would pass every test in this repository.

The honest repair is a rebuilt live module (self-signed proxy, synthetic hostname, throwaway cert,
opt-in behind `pytest -m live`), reading the bytes back through an ordinary client now that reads
are whole. It is not scheduled here.

## 16 · CI cannot run the display-coupled tests, and no switch inside this repository changes that

Measured 2026-08-03, while closing the silent half of the same gap (GB-27).

`.github/workflows/ci.yml` syncs with `uv sync --extra dev --locked` and passes no `--group`, so
the `opi_navigation`-coupled test modules listed in `tests/conftest.py` are not collected there.
That is a decision with a
reason, stated in `pyproject.toml` next to the `displays` group and in `CONTRIBUTING.md`: CI tests
exactly the standalone core a public user gets. What was wrong is a different thing, and it is
fixed: the green report said nothing about the **145 tests** (measured 2026-08-09 with
`pytest --collect-only` over that list) it had not run,
so a reader could not tell a full run from a partial one. Every run that drops those modules AND
prints a header now carries a gap line in it, which is the honest reach: `-q` and `--no-header`
suppress the header block and the line with it. `EPICS_MCP_REQUIRE_DISPLAYS=1` turns the silent
skip into a refusal for anyone who demands the full suite locally.

What that does NOT do is make the display tests run in CI, and the reason is not oversight either.
Measured on 2026-08-03: this repository is PUBLIC (`gh repo view --json visibility`) and carries
**zero** repository secrets (`gh secret list`, exit 0, empty), while `opi_navigation` lives in a
PRIVATE repository that a runner cannot clone without a credential. Adding one is a maintainer
decision about the exposure surface of a public repository, not a technical detail, so it is not
taken here.

The tempting repair, probed and rejected: commit the extra job in commented-out form so it "only
needs the secret". Rejected because commented-out YAML is dead code that nothing lints, runs or
type-checks, and it rots against the next Actions schema change while looking ready.

**The other option is built as of 2026-08-04, and it is not in this repository.** A job in the
PRIVATE repository that owns the engine checks THIS public one out and runs the suite with it. That
inverts which side needs a credential, which is to say it needs none: a public checkout is
anonymous. Measured on its first run, all of it fresh rather than assumed: the checkout succeeded
on nothing but the private repository's own default token; the leg WITHOUT the engine reproduced
this CI's numbers exactly (1776 passed, 72 skipped, the figure both of its Python legs report);
the leg WITH it reached 1884 passed, 64 skipped. **Those are the figures of 2026-08-04 and they
have moved; the latest measured run is 2026-08-09, 1817/72 against 1973/64, a difference of 156
over 148 collected display tests, none skipped.** Both readings are kept rather than one
overwritten, because the pair is the point: a total is a fact about a day, which is exactly why
the job asserts the DIFFERENCE and a per-module floor instead of a remembered total. A noted
expectation is not a guard and ages silently, as these two lines demonstrate on themselves. Its
sharper assertion is per
module: each module of the list it derives from `tests/conftest.py` has to appear in the run
carrying tests, and not one of those tests may be
skipped. That is the case a green report otherwise renders indistinguishable from a full one, and
the guard has been driven red against it deliberately, not merely reasoned about.

Two things that do not change, and they are why this entry stays open rather than retired. This
CI still cannot execute those tests, so the corollary below stands unaltered for anyone reading a
green run here. And the coverage now sits where a reader of this page can neither open nor trigger
it: a change made HERE cannot start that job, so the display tools can break in this repository and
stay green here until someone on the other side runs it.

Corollary for a reader of a green CI run: it means "the standalone core passes", never "the display
tools pass". The mutation proofs of the display tools are sharp only on a checkout that installed
the `displays` group:

```bash
uv sync --extra dev --group displays
EPICS_MCP_REQUIRE_DISPLAYS=1 uv run pytest tests/
```

## 17 · The service-URL redaction removes a userinfo, not every secret a URL can carry

Measured 2026-08-13, when the redaction was built (BG-DURL).

`epics-pv://config` reports `channelfinder_url`, `archiver_url` and `alarm_url`, and those fields
are unvalidated strings, so `https://user:password@host/path` is a spelling an operator can and
does use. `url_without_userinfo` removes the userinfo from them and leaves every other character
alone, because the documented use of that resource is to compare the running server's configuration
with the block in a client's configuration file, and a rebuilt address would make that comparison
false-negative.

**What it does not remove is a token in a QUERY string.** `http://archiver:17665/mgmt?apikey=SECRET`
is printed as configured. That is not an oversight, it is the price of the character-for-character
promise: the sibling redaction on the `epics-doctor` write block drops the query for exactly this
reason, and it can, because there the question is which ADDRESS a write would reach rather than
what an operator typed. The two functions therefore answer the same question differently on
purpose, and each says so in its docstring.

**It also does not recognise a credential written without a literal `@`.**
`https://svc:pw%40host/path` percent-encodes the separator; measured, urllib3 refuses that URL
outright, and since the parser refuses it the address is withheld rather than printed. A Unicode
lookalike (`＠`) is a different character again and is not a userinfo to any parser. None of these
spellings can connect, so the plane is dead either way.

**The error route is closed, and it redacts on a different rule than this resource.** Measured
2026-08-13 it carried the whole request URL, including into a `note` of a SUCCESSFUL payload;
measured 2026-08-14 after the fix, across both failure kinds and every REST-backed tool, it
carries none. The rule differs on purpose: a message names an ADDRESS, so it drops the userinfo
and the query and prints `(unparseable)` where it cannot prove the result names the same address,
while this resource is meant to be COMPARED against a configured value character for character and
therefore keeps the query. Do not read "the query is gone from error messages" as a statement
about this resource.

**What still carries a configured credential, measured the same day.** The server log, by decision
rather than oversight (`docs/safety.md`, `SECURITY.md`): the shared REST layer logs the request URL
at DEBUG and an unexpected internal error logs its cause at ERROR. `epics-doctor`, whose
pattern-based redaction cuts at the FIRST `@` and does not recognise a bare user name. And the
Naming plane, which has no `*_AUTH` variable, so a credential it needs has nowhere else to live.

Three things follow for an adopter. Put credentials in `EPICS_MCP_*_AUTH`, which four of the
planes have (ChannelFinder, Archiver, Alarm, Olog; the Naming plane has none, and its URL is not
part of this payload at all). Read `null` in one of those fields as "configured, but its spelling
could not be shown to be a removable userinfo", never as "not configured", which is `"(disabled)"`.
And do not read the withheld case as a short list: it covers a URL the parser refuses, a URL with
no scheme or no host, an `@` urllib3 does not read as a userinfo, an `@` that survives the removal,
and a cut whose result no longer names the same address.

## 18 · The audit record is one LINE, which is not the same as one set of FIELDS

**Measured 2026-08-14**, when the record separator was closed.

`epics_mcp.audit_record` escapes every character a reader might treat as the end of a line, so one
gate verdict is one record for a byte-oriented reader and for a Unicode-aware one alike. It
deliberately does NOT escape the space, because the space separates the `key=value` pairs INSIDE a
record, and escaping it would rewrite every legitimate line.

**What that leaves open:** a caller-chosen field that contains a space can add further `key=value`
pairs to a record it is part of. Measured on the Olog gate, whose `in_reply_to` reaches the audit
unvalidated (a logbook is checked against the allowlist and a level against the server's vocabulary,
but a reply target is a plain string): a `FAILED` record, which the gate's own comment pins as
metadata-only and ownerless, was made to carry `owner=` and a second `event=` on the same line. The
PV gate has the same shape through the PV name in its `DENY` record.

**Why this is a limit rather than a defect to fix here.** The forgery a separator buys is a whole
RECORD, indistinguishable from a genuine one; the forgery a space buys is extra fields inside a
record that is still attributable to the verdict that emitted it, so a parser reading the FIRST
occurrence of each key is unaffected and a naive scan is not. Closing it properly means quoting
identifier fields, which changes the shape of every audit line and therefore every consumer of one.
That is a wire change with no urgency behind it, so it is written down rather than rushed into a
release.

**What to do with it as an operator:** read an audit line's fields left to right and take the first
occurrence of each key, which is what the gate wrote. And note that neither gate promises anything
about a field a caller chose beyond recording it faithfully.


## 19 · The repository-side release protections are unguarded, and a guard over them was rejected

**Measured 2026-08-14**, when the protections were created.

Three of the four things that now stand in front of a release live on GitHub, not in this tree: the
`release-tags` ruleset, the required reviewer and tag-only deployment policy on the `pypi`
environment, and the allowlist of third-party actions. `SECURITY.md` describes them and
`CONTRIBUTING.md` tells a releaser to expect the approval. **Nothing in this repository notices if
any of them is deleted.** A single API call removes any one of them, and every test stays green.

**What IS guarded, and it is only the tree-side half:**
`test_the_upload_runs_inside_the_pypi_environment` pins the `environment: name: pypi` block into the
`publish` job, so the reference the approval hangs on cannot be removed or moved without a red test.
The rules behind that reference are not covered.

**The tempting repair, probed and rejected.** A test calling `gh api .../rulesets` would need a
token and network access. CI has neither: the workflow token is read-only by repository policy
(`default_workflow_permissions: read`, measured), and a fork checkout has less than that. Such a
test would therefore skip on every run that matters, which is the silent non-check this repository
rejects elsewhere (`tests/test_release_workflow.py` states the same argument for a guard that only
runs when an optional dependency happens to be installed). A guard that cannot go red in CI is not
a guard.

**What to do with it instead:** re-measure by hand when a release is prepared. One command each:

```bash
gh api repos/<owner>/<repo>/rulesets --jq '.[].id' | while read id; do \
  gh api "repos/<owner>/<repo>/rulesets/$id" --jq '{name,rules:[.rules[].type],me:.current_user_can_bypass}'; done
gh api repos/<owner>/<repo>/environments/pypi --jq '.protection_rules'
gh api repos/<owner>/<repo>/actions/permissions/selected-actions
```

A cheaper signal costs nothing: if a `v*` tag push starts the upload **without** stopping for an
approval, one of the two protections is gone.


## Retired entries, and where they went

An entry is retired when the limit it records belongs beside the guard it describes, where the next
author of that guard will meet it. The numbers are not reused.

| Retired | Where the limit lives now |
|---------|---------------------------|
| 2, 3 | `tests/test_prose_counters.py`, in the docstrings of the two guards concerned |
| 4 | `tests/test_prose_counters.py`, the `_Claim` docstring, which already stated both limits |
| 5, 6, 7 | `scripts/guard_audit.py`, beside the audit and the message each one qualifies |
| 8 | `tests/test_prose_numbers.py`, the module docstring |
| 12 | folded into the header of this page |
