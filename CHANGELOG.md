# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
carry breaking changes).

## [Unreleased]

(nothing yet)

## [0.4.0] - 2026-07-29

### Changed

- **BREAKING: the import package is now `epics_mcp`** (was `epics_pv_mcp`). `import epics_pv_mcp`
  stops working; `import epics_mcp` replaces it, one for one, with no other change to the API. This
  completes the rename begun in 0.3.0, where the distribution, the repository, the server command
  and the server's MCP identity already became `epics-mcp` and only the import package did not. It
  is done now rather than later because every release under the old import name grows the set of
  installations a rename breaks.
- **BREAKING: the `epics-pv-mcp` console command is removed.** Use `epics-mcp`, which has been the
  primary command since 0.3.0. The alias was added in 0.3.0 for anyone following pre-rename docs,
  and 0.3.0 shipped it: every installation of that release carries `epics-pv-mcp`, so upgrading to
  0.4.0 removes a command that works today. Check your wrapper scripts and MCP client configs for
  it. Nothing older is affected, because 0.3.0 was this project's first published release of any
  kind. The four diagnostic commands (`epics-doctor`, `epics-diagnose`, `epics-crossplane`,
  `epics-coverage`) are unaffected.
- **BREAKING: the `all` extra is removed.** `pip install epics-mcp[all]` no longer resolves; use
  `epics-mcp[dev]`, which is what `all` contained. It was exactly `epics-mcp[dev]`, so it promised a
  reader everything the package can do and delivered the developer toolchain, and the display-aware
  tools it seemed to imply are not an extra at all but a local dependency group. `dev` is now the
  only extra. Nothing else changes: no dependency is added or dropped by this.
- **The product is called EPICS MCP.** The prose name was still "EPICS PV MCP Server" in 19 places,
  including the README H1 (which is the project page title on the index), the `CITATION.cff` title
  that published work cites, and the first line of the operator guide that ships in the wheel. The
  rename to `epics-mcp` in 0.3.0 was made because the PV plane is one of six, so a title saying PV
  contradicted its own reason. Nothing user-facing behaves differently; the distribution, the
  commands, the import package and the MCP identity are unchanged.
- **`epics-doctor` exits 1, not 2, on an internal error.** 2 is the usage-error code across these
  commands and in argparse, so the old value told a wrapper the caller had passed something wrong
  when the command itself had failed. A genuine usage error (an unknown flag) still exits 2.
  Note the cost: exit 1 is also "a configured plane hard failed", so the exit code alone no longer
  separates the two. They differ on the streams: an internal error writes a `doctor:` line to
  stderr and no report to stdout.

### Fixed

- **The display-aware CLIs tell a broken engine apart from a missing one.** `epics-crossplane` and
  `epics-coverage` probed for the `opi_navigation` engine with a name lookup only, so an engine
  that was installed but did not import (typically a missing transitive dependency) passed the
  check and the command then died with a bare traceback. It now reports the failure with the
  underlying exception named and exits 1, while a genuinely absent engine keeps its explanation and
  exit 2. The advice differs too: installing the engine will not fix a broken one.
- **A long live value in the `find_device` report is capped at 80 characters, not 82**, as its own
  documentation promised.
- **The ready-to-paste write-enabled MCP client block now starts.** As shipped it omitted
  `EPICS_MCP_AUDIT_LOG_FILE` and the loopback reach settings, both of which a write-enabled server
  refuses to start without, so a reader who pasted it saw only "server not connected".
- **The setup instructions no longer describe a `.env` file.** Four places told adopters to copy
  `.env.example` to `.env`, or to run `epics-doctor` to confirm one. Nothing in the server ever
  loads a dotenv file; configuration is read from the process environment, and a variable that does
  not reach the process is dropped silently. `.env.example` is a reference to copy lines OUT of.
- **`EPICS_MCP_ARCHIVER_RETRIEVAL_URL` is spelled with its prefix in the README.** The unprefixed
  form does not bind, so a split-appliance operator who followed it got history requests sent to
  the mgmt port with no error to explain it.
- **`.env.example` names all four tools the Olog write gate covers**, not two. It was the only
  place that understated the gate's reach.
- **Every link on the PyPI project page now goes somewhere.** The README is the `long_description`,
  and on that page there is no repository around it, so its relative targets resolved against
  pypi.org and landed nowhere; measured on the rendered page, all of them. They are absolute GitHub
  URLs now, pinned to `main` so the page always points at current documentation. Anchors were
  already fine there (the index rewrites them) and are untouched. The link guard follows the new
  spelling rather than skipping it as a URL, so a renamed page still fails the build.
- **The Naming-Service modules describe their own origin correctly, and no longer ship a developer
  path.** Both called themselves "vendored" from pvValidator, which is GPL-3.0-only while this
  package is MIT, so the wording asserted a licence problem that does not exist: measured with
  `difflib` over the whole files, the longest identical run of lines is four (two of them non-blank,
  both imports), and the shared remainder is signatures the REST endpoint dictates. They now say
  they follow that client's API shape and carry none of its code, the measurement is recorded in
  the known limits, and attribution sits in the README credits. One of the two also carried an
  absolute path from the author's machine into the published wheel; that string is gone, and the
  repo-wide facility guard now rejects a local drive path so it cannot come back.
- **The operator guide no longer promises an `op=` correlation the refusals cannot honour, and its
  Olog separator table is right about `+`.** The write-posture section said every stage of a
  `set_pv_value` write is `op`-correlated; the id is issued when a write is dispatched, so the two
  pre-dispatch refusals (`DENY`, `BOUNDS_DENY`) carry none, and a reader correlating an audit trail
  by `op` would have looked for a token that is never written. The filter table listed a
  separators-only value as dropped for `level` as well, while the same page explains ten lines lower
  that `+` splits only `title` and is an ordinary level. The guide ships in the wheel and is served
  as the `epics-pv://guide` resource, so both were wrong at the point of use.
- **The start conditions of the PV write gate are now listed completely wherever they are listed at
  all.** The tool description, the server instructions, the configuration reference and the
  deployment guide each enumerated what a write-enabled server needs and left out the loopback-only
  search reach, while other places state it. An enumeration that stops one item short reads as
  complete, so an operator learned that the network posture and the write gate were independent
  settings, when in fact writes on plus a reach beyond loopback is a start-time refusal. The
  configuration reference also gained the `EPICS_CA_NAME_SERVERS` row: it takes part in that check
  but was missing from the EPICS network table.
- **The bug report template no longer calls this an unqualified read-only server.** It can write,
  behind gates; the reason not to paste credentials is that each REST plane takes an
  `EPICS_MCP_*_AUTH` value and an error string can carry the request URL.
- **`epics-mcp --help` prints help, and `--version` prints the version.** `epics-mcp` accepted no
  options at all, so `--help` reached the stdio transport instead of a parser: the command appeared
  to succeed, printed nothing and exited 0, because a server started on a closed stdin ends at once.
  An unknown option is now a usage error (exit 2). `--version` answers the question the bug report
  template asks, which no command line could answer before. The usage line of all five commands is
  also pinned to the command's own name; on Python 3.14 argparse derived it from the interpreter and
  printed the absolute path of the installed script instead.

### Internal

- The two prose guards no longer leak their exemptions: the language guard's self-exemption is keyed
  on file paths rather than bare file names, and both guards anchor an exception's path key on a
  path-component boundary, so a key written for `scripts/x.py` no longer also frees
  `myscripts/x.py`.
- The GitHub Actions workflows moved off the deprecated Node 20 runtime (`setup-uv`,
  `upload-artifact`, `download-artifact`).

## [0.3.0] - 2026-07-28

### Added

#### New tools

- **`list_channel_vocabulary`** lists the ChannelFinder property keys and tag names that
  `find_channels` can be filtered on, as `{enabled, properties, tags}` (names only). `properties`
  is reduced to the same safe-property allowlist `find_channels` enforces, so it never advertises
  a key that would then be refused. An unreadable listing raises rather than reading as "there are
  none". Reuses `EPICS_MCP_CHANNELFINDER_URL`; no new variable.
- **`get_appliance_info`** returns the Archiver Appliance's own topology (`identity`, the per-plane
  root URLs, `cluster_inet_port`, `version`), with no PV argument. It answers "am I pointed at the
  intended cluster before I trust `list_archived_pvs` or `get_pv_history`", where the wrong cluster
  silently yields a complete-looking list of the wrong PVs. A 404 means the wrong endpoint (the
  retrieval webapp serves `/retrieval/bpl`, not `/mgmt/bpl`) and propagates as an error.
- **`list_log_levels`** returns the Olog level names plus `default_level`, or `null` and a note when
  the server does not state a default unambiguously. Call it before filtering a search by level: an
  unknown level returns 0 hits rather than an error.
- **`update_log_entry`** edits an existing Olog entry's `title`, body, `level`, `logbooks` or `tags`.
  Whole-mode only. An omitted argument means *unchanged*: the tool round-trips the entry and
  overlays only what was passed, so attachments, properties and unedited fields survive the
  server's destructive full replace. The logbook allowlist is keyed on the **union** of current and
  resulting logbooks, because moving an entry into a logbook and pulling it out of one are both
  writes to that logbook. An entry whose attachments have duplicate or missing filenames is refused
  rather than edited, since attachment retention is filename-keyed.
- **`add_log_attachment`** attaches files to an existing Olog entry. Whole-mode only, and purely
  additive for content. Note the server re-stamps `owner` to the write service account on every
  call, because this endpoint is its destructive update.
- **`list_log_attachments`** and **`download_log_attachment`** list one entry's attachments and
  fetch raw bytes by (log id + filename) or GridFS id. Bytes bypass the entry redaction, so a
  download is withheld unless whole-mode **and** `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` are both
  in effect. An `output_path` must be a new file: it refuses to overwrite and refuses a symlink
  target.

#### New capabilities on existing tools

- **Attachments on write.** `create_log_entry` and `reply_to_log` take `attachments` (workspace file
  paths, sent as multipart) and `embed_image_base64` for a small inline image. Uploads ride the
  existing Olog write gate plus a size cap (`EPICS_MCP_OLOG_ATTACH_MAX_BYTES`), which also bounds
  the download body.
- **ChannelFinder query filters on `find_channels`:** `has_properties`, `lacks_properties`,
  `not_property_values`, `has_tags` and `lacks_tags`, plus `count_only` for an exact, window-free
  match count from the `/count` endpoint. An unknown property name is a filter, not a silently
  ignored parameter, so a typo narrows the result to roughly zero. Property filtering is gated to
  the safe-property allowlist: filtering on a redacted property is refused, because it would
  reconstruct the partition the response projection hides.
- **`level` and `title` filters on `search_logbook`.** `title` matches whole words, not substrings,
  and is a separate axis from `text`, which searches the body. A blank filter is refused before any
  request, because the server's answers to a blank value are misleading and disagree between the two
  fields. An empty level-filtered result carries a note when the value is not a configured level.
- **Archiver fields that were already fetched and then discarded** are now surfaced, with no extra
  HTTP request. `is_archived` adds the getPVStatus connection-history cluster
  (`connection_loss_regain_count`, `connection_first_established`, `connection_last_restablished`).
  `get_archive_info` adds the alarm, display and control limits plus `units`, `precision`,
  `controlling_pv`, `policy_name` and `modification_time`. The nine numeric limits are always
  present and read `"0.0"` when the PV had no control info, so `"0.0"` does not necessarily mean a
  literal zero limit.
- **Write-side `level` validation.** `create_log_entry`, `reply_to_log` and `update_log_entry` refuse
  a level the server does not list, and refuse a blank one separately, since a blank level would
  silently clear the entry's level. The check runs before the rate token, so a typo costs no token,
  and only when a `level` was passed.
- **`entry_id` in the FAILED write audit.** The server archives and mutates before answering, so a
  timeout can leave an applied write in front of a client that sees FAILED. The record now names the
  entry.
- **Client-side consent hint on `set_pv_value`.** Its `tools/list` entry carries
  `_meta["anthropic/requiresUserInteraction"]=true`. A client that honours it prompts a human on
  every call and fails closed when it cannot ask. This is advisory defense-in-depth only: the
  server-side write gate (env gate, regex allowlist, rate limit, audit) is unchanged and remains the
  sole client-independent guard.

#### Diagnostics

- **`epics-doctor` checks the alarm logger's Elasticsearch backend.** The transport probe is a blind
  HEAD and reported the plane healthy even when the backend was dead. The identity probe now reads
  `elastic.status` from the same response body it already fetches and reports the new `backend_down`
  status (exit `1`) when the logger says its backend is not `Connected`. This is distinct from
  `unverified`: identity is proven, and the service reports its own backend broken.
- **`epics-doctor` reports a failed identity probe** as `identity_probe_failed` (glyph `!`, exit `3`,
  INCONCLUSIVE) instead of collapsing it into `unverified` and exit `0`. A 2xx that merely could not
  be named stays `unverified` and exit `0`. New `--json` field `inconclusive_identity_planes`.
  **Migration:** a script that gated on exit `0` now sees exit `3` for a reachable but
  unidentifiable plane, and should read `inconclusive_identity_planes` alongside `unverified_planes`.
- **`python -m epics_pv_mcp.find_moderate_pv`**, a read-only fixture finder that walks the Archiver
  MGMT event-rate report, filters a rate band, and counter-verifies each examined candidate against
  the target window with a real history fetch.

### Changed

- **Renamed: the distribution is `epics-mcp`, the repository is `epicDirk/EPICS-MCP`** (before
  anything was ever published, so nothing breaks for anyone). The old name undersold the server:
  the PV plane is one of six. The server command is now `epics-mcp` (the old `epics-pv-mcp`
  stays as an alias), and the server identifies itself to MCP clients and to the Olog as
  `epics-mcp`. The import package (`epics_pv_mcp`) and the four `epics-*` diagnostic commands
  keep their names for now; renaming those is a separate, deliberate step.
- **BREAKING: tool argument names unified.** The tools carried four different argument names for
  "the PV this tool is about". They are now `pv_name` for a single PV (`monitor_pv` from `name`;
  `is_archived`, `get_pv_history`, `get_archive_info`, `is_alarm_configured` and `get_alarm_history`
  from `pv`) and `pv_names` for a list (`get_pvs` from `names`, `validate_pvs` from `pvs`).
  Unchanged: `lookup_device_name.name` (a device name, not a PV), the glob parameters `pattern`,
  `name_pattern` and `query`, and **every output field**. This affects INPUTS only; the output field
  of the five renamed REST tools is still `pv`, and aligning those would be a second breaking change
  for anyone reading results. MCP clients that call these tools by argument name must follow;
  positional calls are unaffected. The old name now returns a clean `ToolError`.
- **BREAKING: the server runtime moved from the SDK-bundled FastMCP 1.0 (`mcp.server.fastmcp`) to
  standalone `fastmcp`** (`fastmcp>=3,<4`; `mcp>=1,<2` stays for `mcp.types.ToolAnnotations`). This
  puts both project MCP servers on one stack and removes the two-`ToolError`-class hazard by
  construction, since only one `fastmcp` is on the path. Anyone embedding this server and catching
  `mcp.server.fastmcp.exceptions.ToolError` must switch to `fastmcp.exceptions.ToolError`.
- **Typed output schemas.** 22 of the 32 tools now advertise a typed `outputSchema` instead of an
  open `dict[str, object]`, so a caller can tell "no such PV" from "the service is not configured
  and I could not look", and a complete result from a truncated one. Notable field contracts:
  - `find_channels` returns **disjoint** fields per mode, and the configuration splits them again:
    a configured list gives `{enabled, channels, total, capped}`, a configured count gives
    `{enabled, match_count}`; unconfigured they are `{enabled, channels, total, note}` and
    `{enabled, match_count, note}`. `enabled` is the only field present on every path.
  - `discover_pvs` returns `DiscoverPvsResult`: `pattern`, `pvs` and `total` on every path, plus
    `capped` and `source` on the wildcard-with-ChannelFinder path and `note` on both wildcard paths.
  - `list_archived_pvs`, `get_appliance_info`, `get_archive_info`, `list_channel_vocabulary` and
    `get_alarm_history` carry typed schemas as well.
  The remaining untyped tools are the PV-value tools and the display-lane tools.
- **Three pre-gate refusal codes changed on the wire.** A refusal raised before a write gate is
  consulted writes no audit line, so it must not wear the gate's error code: otherwise an
  un-audited refusal is indistinguishable from an audited gate DENY. `OLOG_WRITE_DENIED` became
  **`OLOG_WHOLE_MODE_REQUIRED`** for the whole-mode preconditions of `add_log_attachment` and
  `update_log_entry`; `RATE_LIMIT_EXCEEDED` became **`READ_RATE_LIMIT_EXCEEDED`** for the opt-in
  read throttle; `OLOG_ATTACH_TOO_LARGE` became **`OLOG_ATTACH_TOO_LARGE_AT_READ`** for the
  post-admission size re-check. Every class stayed a subclass of the one it replaced, so `except`
  clauses are unaffected; a caller matching on the code *string* sees a different value. None of the
  three was ever released.
- **`[INTERNAL]` errors no longer carry the exception's raw text.** An unexpected non-`EpicsError`
  at the tool boundary could put an internal detail (a request URL, a live PV name, a path) in front
  of the client. The client now receives `[INTERNAL] <ClassName>`; the full message and traceback are
  logged server-side at ERROR. The curated `[<code>] message` path is unchanged.
- **Olog refusals no longer report as `INTERNAL`.** `OlogError` carries `error_code` as a class
  attribute and each subclass sets its own. `INTERNAL` read as transient and invited retries that
  each burned a rate token and wrote a FAILED line for a write that never happened.
- **`epics-doctor` no longer fails a service that answers with a different known service's name.**
  It is reported `unverified` (exit `0`) with the found name in the detail. A path-based reverse
  proxy can serve the real API behind a base URL that names another service, so the previous hard
  failure flagged working configurations.
- **`EPICS_MCP_DEFAULT_TIMEOUT` is honoured on the whole read and write path.** Tool timeouts default
  to the configured server timeout instead of a hardcoded `5.0`.
- **The live probe in `diagnose_connection` runs concurrently with the explanatory planes**,
  so worst-case latency is about one timeout instead of two.
- **ChannelFinder property filter semantics are live-verified.** The "unverified until a differential
  live probe" caveat is dropped for `has_properties`, `lacks_properties`, `not_property_values` and
  `count_only`. The tag filters (`has_tags`, `lacks_tags`) remain unverified. The "0 results does not
  distinguish an unknown property from an empty match" note stays, since it is structural.

### Fixed

- **`monitor_pv` reported `truncated` on a complete, exactly-full stream.** The service capped
  collection before appending, so it could not tell "the cap cut the stream" from "exactly
  `max_events` arrived, then it went quiet". It now over-collects one canary event, trims it, and
  reports `truncated` from the real comparison.
- **`--timeout` accepted `-1`, `0` and `inf`, misdiagnosing a healthy service as unreachable.**
  `epics-doctor`, `epics-diagnose` and `find_moderate_pv` took a bare `type=float`, so a
  non-positive or non-finite timeout flowed into a live probe. A shared argparse type now rejects
  those as a usage error (exit `2`) at parse time, before any probe.
- **Two shipped surfaces still taught the pre-rename argument names.** The `compare_machine_state`
  prompt instructed `get_pvs(names=[...])`, to which the server answers "Missing required argument
  `pv_names`", and the `epics-pv://guide` resource still named `pv` for `get_alarm_history`. Both
  corrected, and a contract test now pins every documented `tool(keyword=...)` example against the
  live schema.
- **The attach documentation said the opposite of the truth.** Five places claimed an attach
  preserves every field. The endpoint delegates to the server's `updateLog`, which sets the owner
  unconditionally, so attaching a file rewrites the entry's author. The claim is now narrowed to
  every *content* field.
- **The safety prose named 2 of the 4 write tools.** `EPICS_MCP_ALLOW_OLOG_WRITE` gates
  `create_log_entry`, `reply_to_log`, `add_log_attachment` and `update_log_entry`, but README,
  ARCHITECTURE and the operator guide described it as the first two. A site admin approving the gate
  on that basis would unknowingly enable the destructive `update_log_entry`.
- **The archive was cited as a safety net it cannot be.** Three places pointed at the server-side
  archived version to soften the owner re-stamp; no tool here can read or restore it. All three now
  say recovery is manual.
- **The `level` parameter descriptions disagreed** across the tools that take one. All three now
  point at `list_log_levels`.
- **`set_pv_value` audit blind spot on cancellation.** A write cancelled mid-put left no `PV_WRITE`
  record, even though the p4p put keeps running and may still land at the IOC. It now emits
  `ATTEMPT` with a correlation id before the I/O and `UNKNOWN_PENDING` on cancel, then re-raises the
  cancellation unchanged. It is never mislabelled `FAILED` and never blindly retried.
- **`NamingServiceClient` normalises `base_url`** (trailing-slash strip) like the other REST clients,
  so a URL configured without a trailing slash no longer 404s.

### Removed

- **The `[displays]` extra.** `pip install epics-pv-mcp[displays]` no longer resolves. The extra
  pointed at the `opi_navigation` PV engine, which lives in a private repository, so no outside
  user could ever install it: the extra advertised a capability it could not deliver. The engine is
  now wired in as a local dependency group (`uv sync --group displays`), which keeps it working in
  a checkout that has access while keeping it out of the published package. The four display-aware
  tools (`validate_pvs`, `crossplane_check`, `coverage_audit`, `find_device`) are unaffected where
  the engine is present, and register themselves only when it is, exactly as before.
- **The post-registration schema-pruning pass** (`_prune_tool_schemas` and three helpers, roughly 130
  lines) is gone. Standalone `fastmcp` emits the lean schemas natively, so the pass had nothing left
  to do. The one remaining need, dropping an accept-all `outputSchema`, is now the public
  `output_schema=None` on the tools that return an untyped dict.

## [0.2.0]

Baseline: an independently developed EPICS PV MCP server on FastMCP and p4p with a
write-safety layer, batch operations, PV monitoring, cross-plane provenance
(ChannelFinder, Archiver, Alarm, Naming), device lookup, and OPI file validation.
Originally seeded from [Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server).

### Added

- `epics-pv://guide` resource and `OPERATING.md`: an operational cookbook (service planes,
  archiver-enumeration, retrieval-cluster and CA-bundle recipes, error signatures) shipped inside
  the package, so the server carries its own operational knowledge. Backed by one source file
  (`src/epics_pv_mcp/operator_guide.md`), drift-guarded against the tool and env surface.
- Optional `[displays]` extra: the display-aware tools (`validate_pvs`, `crossplane_check`,
  `coverage_audit`, `find_device`) live behind an optional dependency on the `opi_navigation` PV
  engine. The core server (live PV access, diagnosis, REST-service planes) installs and starts
  standalone without it.
- Packaging metadata (`license`, `readme`, `authors`, `keywords`, project URLs), plus
  `ARCHITECTURE.md`, `CONTRIBUTING.md` and this changelog.
