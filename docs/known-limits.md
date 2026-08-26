# Known limits: what this repository does not guard

**What an entry IS:** a measured limit of a guard this repository runs, plus the tempting repair
that was probed and REJECTED rather than merely postponed. **What it is NOT:** a work journal or a
QA history. Each entry names the measurement that produced it, so a reader can re-run it instead of
trusting it.

**Treat every figure as "was true when written."** This page is not one of the markdown files the
prose-counter guard reads (entry 1), so nothing re-runs a number on it. Where a figure exists to establish a SHARE, the
share is what this page states, together with the rule to re-derive it.

**Numbers are permanent identifiers, not positions.** A retired entry's number is never reused, so
a citation elsewhere in this repository still means what it meant when it was written. What was
retired, and where it went, is listed at the end.

**On this page:** [1 markdown figures](#1--markdown-figures-are-unguarded-on-every-page-but-one) ·
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
[20 the commit-message guard](#20--the-commit-message-guard-is-per-clone-state-and-its-site-pattern-half-needs-a-git-ignored-file) ·
[21 error codes outside errors.py](#21--the-documented-error-code-population-is-errorspy-and-ten-more-codes-are-raised-elsewhere) ·
[22 the cross-plane block](#22--the-cross-plane-block-sees-three-patterns-and-each-is-blind-in-a-stated-way) ·
[23 the archiver-pair sentence](#23--the-archiver-pair-finding-says-both-webapps-answered-for-two-statuses-where-nothing-answered) ·
[24 Dependabot](#24--dependabot-does-not-update-the-python-dependencies-here-and-never-has) ·
[retired](#retired-entries-and-where-they-went)

---

## 1 · Markdown FIGURES are unguarded on every page but one

The prose-counter guard reads Python comments and docstrings, and markdown too. It is pointed at
exactly ONE page: `src/epics_mcp/operator_guide.md`, the guide `get_guide`
ships to a model. Seven of its seventeen size-naming figures are compared to the code they are
about, ten are inventoried with the reason they cannot be. Every OTHER page in this repository is
still unguarded for figures.

⚠️ **What is rejected, and stays rejected, is a guard over the WHOLE tracked markdown**, not the
reading of markdown at all. The reader exists and is pointed at one page; the cost of widening it
to the rest of the tree is re-measured below, and that cost still holds.

⚠️ **Markdown is not unguarded, it is unguarded FOR FIGURES.** Several guards read it, they just
do not read figures:
`test_doc_links` resolves every relative link against `git ls-files`, `test_guide_matches_code`
checks tool names, `EPICS_MCP_*` variables and status glyphs across the tracked pages,
`test_write_posture_sites` holds the write-posture claim and its pointers, and
`test_changelog_discipline` caps the size of a new `[Unreleased]` entry. The claim below is about
SIZE-NAMING FIGURES in markdown, and only about those.

Why not the rest of the tree, re-measured 2026-08-21 with the detector's own reader over
`git ls-files "*.md"`. **Two reasons, and they are different reasons, which is why one page could
come in while the others stayed out.**

- **`CHANGELOG.md` cannot come in at all**, and not for cost: a release entry that said
  "22 schemas" MUST keep saying it, so every one of its figures would need a "historical, not
  derivable" row, precisely the construction `tests/test_prose_counters.py` rejects in writing as
  "a blanket exemption wearing a table's clothes". It carries the largest share of the tree's
  markdown figures on its own, and it is also the only tracked page whose section titles repeat,
  which the markdown key rests on them not doing
  (`prose_numbers.ambiguous_headings`). `CLAUDE.md` is the same argument at a smaller size.
- **`docs/known-limits.md`, this page, is kept out on COST.** Its size-naming phrases move on
  better than one commit in two, measured over its commits since 2026-07-01, and the highest of any
  tracked markdown page (`CHANGELOG.md` 29%, `CLAUDE.md` 26%, the shipped guide 16%). The first
  wording said "of any page that would be worth watching", which cannot be falsified because
  nothing defines that set. Every one of those commits would demand a table
  edit whether or not a figure was wrong, and a guard that cries wolf on half of its subject's
  commits stops being read.

Re-derive both with the detector's own vocabulary (a number paired with a
`prose_numbers.COLLECTION_NOUNS` member, or the `N of the M` shape); the workspace keeps the
script that produced the churn shares next to the GQ-123 report.

⚠️ The one inventory worth knowing about: `docs/tools.md` lists every tool, and **nothing compares
that list to the registrations.** Only the operator guide's inventory block is checked
(`test_guide_matches_code`). What is checked on `docs/tools.md` is narrower than its tool table and
touches none of it: the resource URIs (`test_readme_resources`), that no page advertises an
`epics-*` command the package does not install (`test_examples_match_entry_points`), that its
"part of the core install" sentence names the same commands as the engine gate
(`test_cli_without_display_engine`), that every status name it pairs with a glyph uses the
glyph `cli_doctor` renders (`test_guide_matches_code`, which names this page explicitly so a rename
cannot drop it out of the scan), that its relative links resolve (`test_doc_links`), and that it
uses no retired name for the display context cap (`test_diagnostics_tail`). Six test modules read
the file, and **not one of them reads the tool table.** It is a second inventory of the same thing
with one of them guarded.

⚠️ **How that six is counted matters, and the obvious way to count it is wrong in both
directions.** Deriving it from `grep -rln 'tools\.md' tests/` also yields six `.py` modules and a
DIFFERENT six: it finds `test_resources`, which names the page in a comment and reads
`operator_guide.md` instead, and it misses `test_examples_match_entry_points`,
which really does read the page but reaches it through `(_ROOT / "docs").glob("*.md")` and
therefore never spells its name. `test_diagnostics_tail` is in both sets for the same reason in
reverse: it names the page in a comment AND reads it, through a `docs/` directory prefix. So the
grep answers "which modules mention the string", the number above answers "which modules open the
file", and the two agreeing on a total was a coincidence. Re-derive it by looking for a `docs`
directory walk as well as the name, not by the grep alone. This mattering on the page whose whole
purpose is honesty about measurement methods is the reason it is written out rather than fixed
silently.

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
repaired mechanically instead, because their fix costs no German recall: URL-shaped tokens are
blanked before any signal runs, and a word ending in a watched suffix is checked against an
explicit English lookalike stoplist first. Both carry their own reasoning in
`scripts/check_language.py`, beside the code that applies them, which is where the retirement rule
at the end of this page says such a manual belongs.

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
`services/naming_client.py` and `services/naming_exceptions.py` name pvValidator in their own
docstrings, an unrelated validation tool that is **GPL-3.0-only** while this package is MIT.
Following another project's API shape and error names is not taking its code, and which of the two
happened here is measurable rather than arguable.

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
  nine surfaces read.** (The population is derived from `git ls-files` at run time, so the nine is
  a reading rather than a constant, and it grows without this line noticing.)
  A page that stops documenting all but one of its pairings passes. The
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

`epics://config` reports `channelfinder_url`, `archiver_url` and `alarm_url`, and those fields
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
at DEBUG and an unexpected internal error logs its cause at ERROR. And the Naming plane, which has
no `*_AUTH` variable, so a credential it needs has nowhere else to live.

⚠️ **`epics-doctor` is deliberately not a third entry, and that is the result of a search rather
than a proof.** Its cause texts cross the same barrier as every other message, and **no unredacted
exception was found that could reach them**. The search has a known hole: of the six probe sites,
four re-raise through the shared barrier, but the archiver and archiver_retrieval planes go through
`rest_get_json`, which catches `RequestException` only, so a bare `OSError` reaches the verdict
unwrapped. The one instance anybody has produced, an unreadable `EPICS_MCP_CA_BUNDLE`, is now
recognised by name and reported as `ca_error`; the hole it came through is still open for any other
bare `OSError`. Both catch sites are bare `except Exception`, so the population is whatever those
call trees can raise. The one place where a credential really did print is that command's
write-gate block, which rebuilt its target address and printed a fragment of the password in the
path for `https://svc:p@ss/w0rd@host/Olog` on every run with an armed gate. That is FIXED, and it
is the only real credential disclosure this section carries.

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


## 20 · The commit-message guard is per-clone state, and its site-pattern half needs a git-ignored file

**Measured 2026-08-15**, when the guard was built.

The commit MESSAGE is a leak surface no file guard reaches: `test_guide.py` scans what
`git ls-files` reports, and commit metadata is outside that population by construction. It is not a
theoretical surface. `git log --format='%ae' | sort | uniq -c` counts **614** commits carrying the
facility address, out of the **670** that predate the identity switch, and running the shared
detector over `git log --format=%B` flags **2 of 722** bodies for a real device name. None of it can
be corrected afterwards: tags and published releases pin the commit ids.

`scripts/check_commit_message.py` closes it going forward, at the `commit-msg` stage. **Two halves
of that guard are outside what any test here can hold.**

**(a) The installation.** `.pre-commit-config.yaml` carries the wiring, and
`tests/test_commit_message_guard.py` pins it: the stage entry, `pass_filenames`, `always_run`, and
`default_stages`, so the hook cannot go inert or silently widen onto the file hooks. But
`.git/hooks/commit-msg` is per-clone state outside the tree. **A fresh clone has no message guard
until somebody runs it:**

```bash
uv run pre-commit install --hook-type commit-msg
ls .git/hooks/commit-msg          # the only honest check
```

⚠️ `uv run pre-commit run --all-files` drives the **pre-commit stage only**, so a green gate run is
no evidence at all about this hook. That is why its decision function is pure and pinned by
ordinary tests, which is the half CI can check.

**(b) The site patterns.** `check_no_ess_internal.load_patterns()` reads
`scripts/.pii_patterns.local`, which is **git-ignored**. On the FILE surface that gap has a
fallback, the committed repo-wide test CI runs (see the facility-agnostic guardrail in
`CLAUDE.md`). Here there is none, because CI never reaches this stage. A clone without that file
runs the message guard with the built-in secret formats and the structural device-name detector
only. The structural detector is the half that depends on no local file, and it is the half the
measured leak needed.

**The tempting repair, probed and rejected.** A test asserting `.git/hooks/commit-msg` exists would
be red on every fresh clone and every CI run, which trains people to ignore it, and green the moment
someone writes any file with that name. Neither direction says anything about whether the hook runs.
The check is the `ls` above, at the moment it matters.

## 21 · The documented error-code population is `errors.py`, and ten more codes are raised elsewhere

`tests/test_error_codes_documented.py` holds that every class in `src/epics_mcp/errors.py` setting
its own `error_code` is explained in the operator guide's "Error signatures" section. It reads that
population out of the syntax tree, so a new class is red the day it is written. It says nothing
about a code raised anywhere else, and codes ARE raised elsewhere.

**Three counting rules, three numbers, and none of them is "the number of error codes."** State the
rule with the figure or the figure means nothing:

| Counting rule | Measured 2026-08-15 |
|---|---|
| a string literal directly in `error_code="..."`, anywhere under `src/` | **21** fixed codes |
| plus the literals RETURNED by the four `*_error_code` helpers | **22** fixed plus **4** dynamic families |
| plus each helper's `*_RESPONSE_ERROR` fallback | **at least 26** fixed plus the same 4 families |

The dynamic families are `ALARM_HTTP_{status}` (`services/checkers.py`),
`CHANNELFINDER_HTTP_{status}`, `ARCHIVER_HTTP_{status}` and `OLOG_HTTP_{status}`
(`services/checkers_olog.py`): the served HTTP status becomes part of the code, so their members
cannot be enumerated at all. The third row is a LOWER BOUND on purpose. Re-derive any of them by
naming the rule first, and do not compare a figure produced by one rule with a figure produced by
another.

**The ten codes outside `errors.py` under the first rule split in half, and the halves are not the
same kind of thing.**

- **Five are input validation, and they are out of the GUARD, which is not the same as out of the
  section.** `INVALID_INPUT`, `INVALID_TIME_WINDOW`, `INVALID_ARGUMENT`, `BATCH_TOO_LARGE`,
  `PATH_OUTSIDE_WORKSPACE`. ⚠️ Measured rather than argued: **three of them already stand in that
  section** (`INVALID_INPUT`, `INVALID_TIME_WINDOW`, `INVALID_ARGUMENT`), and only
  `BATCH_TOO_LARGE` and `PATH_OUTSIDE_WORKSPACE` do not. So the claim here is NOT that a
  validation code does not belong there; the document already disagrees with that. The claim is
  narrower and is about the guard: these codes
  tell a caller their ARGUMENTS were wrong, before any service was contacted, so the tool's own
  schema and message are the primary channel and a missing entry is not a hole a running operator
  falls into. Requiring all of them would put a wall of argument validation into a section a
  reader opens to diagnose a control system.
- **Five are operational signatures, and they are measurably missing.** `UPSTREAM_CONTRACT_ERROR`
  (`services/epics_client.py`), `OLOG_HTTP_404`, `OLOG_ATTACH_TOO_LARGE_AT_READ`, `FILE_EXISTS`,
  `INTERNAL`. Each says something happened out in a service or on disk, which is exactly what that
  section is for. They are open, recorded here rather than papered over, and carried as a work item
  of their own rather than folded into the guard above, so that guard's effect stays measurable.

**The tempting repair, probed and rejected.** Widening the population to "every code-shaped literal
under `src/`" also demands the reverse direction (nothing documented that cannot be raised), and
that direction is unbuildable here: the section legitimately names members of the dynamic families,
whose literal spellings exist nowhere in the source, so every one would be reported as an
invention. A guard whose noise outnumbers its findings gets suppressed, and then it guards nothing.

## 22 · The cross-plane block sees three patterns, and each is blind in a stated way

`epics-doctor`'s `Installation` block compares the planes with one another. What it cannot see is
recorded here rather than in the block itself, because a report that listed its own blind spots on
every run would train the reader to skip it. Measured 2026-08-16.

**It reports a SIGNATURE, not a cause.** Both the archiver-pair and the one-host findings match on
statuses and configuration alone. A gateway answering 5xx for both archiver webapps, and two URLs
that are each one path segment too deep, produce the same evidence as an exchanged pair; nothing
in the block separates them, and its text says so and names a check rather than a change. The
`evidence` field on the wire carries `signature` for exactly this reason, so a future confirming
probe is an additive change rather than a rewrite of the contract.

**The one-host finding is deliberately conservative in three directions.** It needs two distinct
host-and-port authorities on the host, so a single-JVM appliance (two planes on one address) and a
path-based reverse proxy never trigger it, even when they really are down. It stays silent when
EVERY configured plane on EVERY host has failed: that shape was measured twice here with the cause
on the caller's side rather than on any host (an unreadable CA bundle, and an `HTTP_PROXY`), so
naming hosts would point at the wrong end. And it counts only `unreachable`, because the sentence
it prints is "none on it answered": `api_error` and `identity_probe_failed` both mean the host DID
answer, and a `ca_error` from an unreadable bundle is raised before a socket is opened. A
`throttled` plane is outside every pattern for a related but distinct reason: nothing was sent, so
it is evidence about this command's read budget rather than about any host, and both this finding
and the archiver-pair one would otherwise fire on a healthy deployment under a tight limit
(BG-DTHR). Keying on
all four was measured to contradict the report's own plane lines twice, once by calling a host
that answered every request silent, once by naming a host nothing had ever contacted.

**The TLS comparison is invisible with `EPICS_MCP_TLS_VERIFY=false`.** No `ca_error` is raised
then, so a host presenting a certificate this process would otherwise reject produces no finding
at all. It also withholds both findings when fewer than two HTTPS planes are configured (with one,
the host and the bundle are indistinguishable) and when no HTTPS plane completed a handshake at
all, because "the others are fine" would then be a claim about handshakes that never ran.
It names an observation and never a cause: `ca_error` is raised for any SSL failure, so an expired
certificate, a hostname mismatch and an interception proxy all look the same from here.

## 23 · The archiver-pair finding says "both webapps answered" for two statuses where nothing answered

`_archiver_pair` in `src/epics_mcp/services/doctor_crosscut.py` triggers on membership in
`failing` alone, and `failing` is built from all four `_TRIGGERS`. Its text opens **"Both archiver webapps answered with an error while pointing at different
URLs"**, and for two of those four triggers nothing answered: on `unreachable`, which
`_classify_failure` defines as "a transport failure, no chained HTTP response", and on the
`is_ca_bundle_error` half of `ca_error`, which is raised before a socket is opened. Measured
2026-08-19 by driving `installation_findings` with each trigger in turn: all four produce that
sentence. With two genuinely unreachable webapps on one host, the same report prints it directly
beside `host_down`'s "none on it answered", so the block contradicts itself in two adjacent lines.

⚠️ **It exists WITHOUT any read throttle and is untouched by BG-DTHR.** It was found while that
ticket was measured, and it is recorded here rather than fixed there because the two are
independent: BG-DTHR put `throttled` into `_NEVER_TRIGGERS`, so a plane this command never asked
cannot reach `_archiver_pair` at all, which removes one WAY to reach the wrong sentence and not
the sentence. The four other paths to it are unchanged.

**Nothing pins today's width.** `tests/test_doctor_crosscut.py` drives this finding with
`api_error` only, so the two statuses that make the sentence false are exercised by no test. The
repair is the one its neighbour `_host_down` carries: earn the claim from what the plane MEASURED
rather than from which bucket its status is in, which for this sentence means `reachable is True`.
It is written down rather than made here because it belongs to a different ticket, and a change
nobody asked for is how a review loses its boundary.

⚠️ **What makes `_host_down` the model is that it earns "this host answered" from an answer, not
from "did not fail".** Earned the other way, a plane nobody contacted certifies a host, and one
sitting on the dead host withdraws a correct finding. No line numbers are cited on this page
either, for the reason at the top: they move.

## 24 · Dependabot does not update the Python dependencies here, and never has

**Measured 2026-08-24**, at the Actions run logs rather than at a report.

`.github/dependabot.yml` configures two ecosystems and only one of them works. All three
`github-actions` update runs this repository has recorded succeeded. All NINE `uv` update runs
failed, from the first one on: the configuration landed on 2026-07-25 with `33855fd`, and the first
three runs failed the same day. The newest is run `32540700189` of 2026-08-22. Six of the nine were
read in full and the cause is identical in all six: the git fetch answers `404`, the proxy's scope
request is refused with a `403` whose detail reads `Either the repo doesn't exist, or Dependabot
doesn't have access to it`, and the updater then logs `git_dependencies_not_reachable` once per
dependency, each time naming `https://github.com/epicDirk/opi-foundry.git`. That URL is the display
engine, pinned in `pyproject.toml` as a git dependency on a PRIVATE repository. The dependency set
is resolved as a whole, so one unreachable git entry takes all SEVEN of `pytest`, `ruff`, `mypy`,
`pre-commit`, `pydantic-settings`, `mcp` and `fastmcp` down with it. It is the same unreachability
`CLAUDE.md` already records for CI, which cannot clone the engine either.

**What it costs.** No Python dependency of this repository is watched for a published advisory.
The four Dependabot alerts that exist are all in state `fixed`, so nothing open is being ignored
today, and that is the trap rather than the reassurance: the mechanism that would report the NEXT
one is off, and it is off silently. No pull request, test run or release says so. The only signal
is a red run in the Actions tab that nothing asks anyone to open.

**The tempting repair, probed and rejected.** Giving Dependabot access to the private engine
repository would work, and it is refused: it hands a read credential on a PRIVATE repository to a
service in order to keep a lock file current on a PUBLIC one. That repository carries more than
the engine, and the trade is not proportionate to what it buys. Decided 2026-08-24, and this entry
is the end state until further notice rather than a note pending a fix.

**What is NOT rejected**, so that this entry does not read as a closed door: configuring the `uv`
ecosystem so it never has to resolve the git entry is untried, and nothing here blocks it. If it
is ever built, the proof is a green `uv` update run with its number, and this entry is retired
rather than edited.

**What to do with it instead**, since no guard in this tree can see any of it: re-measure by hand
when a release is prepared, and upgrade deliberately rather than waiting for a pull request that
will not arrive.

```bash
gh api "repos/<owner>/<repo>/actions/runs?event=dynamic&per_page=100" --paginate --jq '.workflow_runs[] | select(.name | contains("- Update")) | .created_at + " " + .conclusion + " " + .name'
uv lock --upgrade   # then let the gate chain judge the result
```

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
