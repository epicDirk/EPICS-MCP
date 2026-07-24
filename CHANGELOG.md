# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
carry breaking changes).

## [Unreleased]

### Added

- **The first LIVE write-gate deny test (`tests/test_write_gate_live.py`).** Contract point 6 prefers a
  deny path observed against a real service over an in-memory assertion; until now no write gate had
  one, and `CLAUDE.md` recorded that as an open gap. Exactly ONE path is covered live, deliberately:
  **`olog_allowlist_miss` on `add_log_attachment` / `update_log_entry`** — the only deny path whose
  *decision input* comes from the server (the target entry's logbooks, read back over HTTP), i.e. the
  only class a mock cannot falsify, because a mock supplies the very payload shape the code assumes.
  The call passes a log **id** and nothing else, so the refusal naming the target's logbook can only
  have travelled id → `GET /logs/{id}` → gate. The other eight rows are argued out by name in the
  module docstring (PV: every deny raises before the first I/O; Olog env-off / URL boundary: shadowed
  by the whole-mode precondition on these tool paths; empty-logbooks: unreachable there; size cap: no
  server-side input; rate limit: would need N real mutating writes). Built against the trap that
  would make it worthless — all four Olog gate branches raise the same class, the same code and a
  byte-identical audit line, so one missing env var would deny at branch 1 and leave a naive test
  green: it matches the allowlist branch's OWN message, asserts the server-reported logbook name and
  the exact code, demands all SIX env vars through `assert_live_available` (never a one-variable
  gate), routes "no usable logbook" through that same gate rather than a bare `pytest.skip` (which
  would defeat `EPICS_MCP_REQUIRE_LIVE`), and runs a POSITIVE CONTROL per tool so a deny-all allowlist
  cannot masquerade as a working gate. Self-sufficient: it creates its own deny target through the RAW
  client (which bypasses the gate by construction) instead of depending on a pre-existing corpus.
  Provably red under two targeted mutants: branch 3 denying with branch 1's message, and branch 3
  disabled. The module is **not idempotent**: a full run leaves four entries (each deny test creates
  its own target through the raw client before the gate refuses — only the *gated call* mutates
  nothing — and each positive control creates one and then writes to it). Olog has no delete, so
  every entry is labelled as a test artifact and **`EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK` is required**:
  it names the scratch logbook the deny targets go into, and unset is a refusal rather than a guess.
  `CLAUDE.md`'s "honest limit" is re-scoped accordingly: **PV = not applicable with a reason,
  Olog = one path live, the rest reasoned** — not "all deny paths proven".

- **Pre-gate refusals no longer wear a write gate's error code — and the rule is now CI-enforced.**
  The write-gate contract (`CLAUDE.md`, point 4) says *"a refusal raised outside the gate must not
  carry the gate's error code"*: such a refusal writes **no audit line at all**, so reusing the
  gate's code makes an un-audited refusal indistinguishable from an audited gate `DENY` and the
  audit's coverage claim unfalsifiable from outside. Five call sites violated it. Four new codes,
  each on a class that stays a **subclass** of the one it replaces, so every existing `except`
  keeps catching it:
  - `OlogWholeModeRequiredError` → **`OLOG_WHOLE_MODE_REQUIRED`** for the whole-mode preconditions
    of `add_log_attachment` / `update_log_entry` (`services/checkers_olog.py`, raised *before*
    `get_olog_safety()`), replacing `OLOG_WRITE_DENIED`.
  - The client-side backstop `OlogWholeModeRequired` (`services/olog_exceptions.py`) moves to the
    **same** code via its ClassVar, so one condition reports as one code on both layers.
  - `ReadRateLimitError` → **`READ_RATE_LIMIT_EXCEEDED`** for the opt-in read throttle
    (`services/_http.py::ReadThrottle.check`), which shared the write gates' audited
    `RATE_LIMIT_EXCEEDED` although it guards reads, writes no audit line, and is reached from the
    reads the Olog write tools perform before their gate. A named scope exception was considered
    and rejected: a rule filed as "hard" that carries a carve-out from day one is prose again.
  - **`OLOG_ATTACH_TOO_LARGE_AT_READ`** for the TOCTOU size re-check in
    `services/olog_attachments.py::read_uploads` — found by the new guard, not by review. It is a
    *post*-admission refusal (the rate token is already spent) and un-audited, so wearing the gate's
    audited `OLOG_ATTACH_TOO_LARGE` hid exactly the distinction the contract is about.

  The guard is `tests/test_write_gate_contract.py::test_no_pre_gate_refusal_carries_a_gate_error_code`:
  an AST sweep of the whole flat `services/` package (~31 modules) for any exception raised with one
  of the gates' audit codes, resolving class → code through both repo conventions (constructor in
  `errors.py`, ClassVar in `services/*_exceptions.py`) and honouring a literal `error_code=` keyword.
  The code set is **derived from** the canonical `DENY_PATHS` table, so a third gate widens the guard
  in the same edit. Provably red: on the pre-fix tree it reports all five sites by file:line.
  A companion behavioural test pins that the whole-mode refusal reports the new code **and** leaves
  no audit record, with a positive control proving the audit logger is genuinely being captured.
  Deliberately NOT changed: these refusals still emit no audit line — contract point 4 scopes the
  audit promise to gate verdicts and writes that reach the I/O, and an audit call from `services/`
  would sit outside the reach of the `_audit_deny` drift guard, buying a new blind spot as a "fix".
  The reasoning is written at the raise sites, not only here.

- **Annotation consistency / drift guard (test-only).** Four tests pin every tool's client-facing
  safety hints (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`), which drive real
  client behaviour since MA-Q2 (K1 keys the consent invariant on `destructiveHint`; a honouring client
  derives permission prompts from them, so a mislabelled or silently drifted field is security-relevant).
  A completeness law (every tool sets all four hints explicitly — `ToolAnnotations()` defaults them to
  `None`, not a bool), a spec-consistency law (`destructiveHint=True ⟹ readOnlyHint=False`), and a
  golden map of all 32 tools' hints. The golden map is load-bearing: it is the ONLY guard that catches a
  SILENT change of an existing tool's annotations — e.g. flipping `set_pv_value` to
  non-destructive/read-only — which the two structural laws AND the MA-Q2 consent guards all pass. Two-lane
  robust (core-only 28 / full 32) via an AST-scan of the display-extra tools, with a pure set-logic helper
  unit-tested against both lanes; each guard is provably red. No new tool; `tools/list` unchanged. Honest
  limit: structure + drift, not semantics (whether a hint matches the tool's true behaviour — a review).
- **MA-Q2 client-side consent hint on `set_pv_value`.** The `tools/list` entry for the sole
  PV-mutating tool now carries `_meta["anthropic/requiresUserInteraction"]=true`. A client that honours
  it — Claude Code from v2.1.199 — prompts a human on every call, even under `bypassPermissions`, and
  fails closed on a recognising client (`dontAsk` and non-interactive `--permission-prompt-tool` runs
  deny the call rather than writing silently; the Agent SDK `canUseTool` callback can still approve).
  An older or non-recognising MCP client ignores the hint. This is advisory **Defense-in-Depth only** —
  the server-side write gate (env gate + regex allowlist + rate-limit + audit) is unchanged and remains
  the sole client-independent guard. A fail-closed class-invariant test keyed on `destructiveHint` (with
  the Olog-write class explicitly deferred) ensures a future PV-write tool cannot ship without the hint,
  and a drift-guard test ensures a consent tool documents the key in its description. No new tool;
  `tools/list` stays 32/28 and within the size-gate ceiling.
- **MA-Q1a tools/list schema-hygiene hardening (nit/low follow-ups to MA-Q1).** Robustness and
  precision polish on `_prune_tool_schemas`, with NO wire change — `tools/list` stays 64499 characters,
  `list_tools` 32/28. The title-strip now also descends into the JSON-Schema 2020-12 single-subschema
  keywords `contains` / `unevaluatedItems` / `unevaluatedProperties` (a `title` under one of them would
  otherwise survive). A2's empty-outputSchema test is tightened, via a named helper
  `_is_information_empty_output_schema`, to the exact accept-all object form
  (`{type: object, additionalProperties: true}` with no `properties`) — so a future array / `RootModel`
  return, which also lacks a top-level `properties`, is no longer dropped by mistake. The prune loop now
  isolates each tool in its own `try/except`: one broken tool is logged and skipped, the others still
  prune, so the pass never leaves a half-pruned state. Each code fix ships a provably-red regression
  test; plus three doc corrections (A1 contributes ~3.7k, not ~4.7k; a truthful crash-guard raise-source
  example; the ceiling is a character budget, not a byte budget).

- **MA-Q3 tools/list size-gate (test-only guard).** A relational `<=` ceiling test
  (`test_tools_list_within_budget`, ceiling 70000) so the wire `tools/list` payload MA-Q1 shrank cannot
  silently regrow — a new tool, an SDK change that inflates the wire, or a regression that stops the pruning
  now turns a green suite RED. This is the guard behind MA-Q1's "precondition for every new tool" framing,
  which was previously documented intent only (no test asserted the character budget). Relational (not a count) so
  the core-only [28 tools] and full [32] lanes both pass; provably red below the current 64499 characters; also catches a
  broken prune (the un-pruned payload is 70237 > 70000). Headroom is deliberate — raising the ceiling is a
  conscious, reviewed one-line change with a rationale, never a silent bump.

- **New `list_channel_vocabulary` tool — discover what `find_channels` can be filtered on (MA-2 CF-Query-Fläche).**
  The MA-2 filters (`has_properties`/`lacks_properties`/`not_property_values`/`has_tags`/`lacks_tags`)
  were ergonomically dead: a caller had no way to learn which property keys and tag names exist. The new
  read-only, no-`pv` tool lists them as `{enabled, properties, tags}` — NAMES only (the DS-privacy `owner`
  and `value` on the vendor `PropertyDto`/`TagDto` are dropped). `properties` is fetched from
  `/resources/properties` and reduced to the SAME safe-property allowlist `find_channels` enforces (and
  `_project` surfaces), so it lists only the keys that both exist in this instance AND are accepted as a
  filter — a non-allowlisted, person-bearing property like `ENGINEER` is never advertised (it would be
  refused with `INVALID_INPUT` anyway); expand `EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES` to surface
  more. `tags` (from `/resources/tags`) is the full, ungated server tag set (tags are ungated in
  `find_channels` too). Strict like the Olog `_named_list` (S11): an unreadable/non-list listing RAISES
  rather than collapsing to `[]` — the listing IS the answer, so "unreadable" must never read as "there
  are none"; an empty list is valid, and `enabled:false` (CF unconfigured) is distinct from it. Reuses
  `EPICS_MCP_CHANNELFINDER_URL`/`_AUTH` — no new env var. `list_tools` 31 → 32, no schema change (the
  tool returns an untyped `dict`). Red-proof-first: client tests (allowlist intersection, ungated tags,
  strict-raise on bad payload, empty→`[]`) + the tool disabled/enabled pair.

- **New `get_appliance_info` tool — surface the Archiver `getApplianceInfo` body (Fundort 3, decision KK).**
  The MGMT `getApplianceInfo` body names WHICH appliance this is (`identity`) and where each plane is
  served (`mgmt_url`/`engine_url`/`etl_url`/`retrieval_url`/`data_retrieval_url`, `cluster_inet_port`)
  plus a `version` string — but nothing surfaced it: the doctor plane-check fetches the SAME body and
  keeps only `identity`, discarding the rest. The new read-only, appliance-scoped tool (no `pv` arg)
  projects the whole body through an `_APPLIANCE_INFO_FIELDS` allowlist so an unexpected field is
  never surfaced and an absent field (e.g. `version` on a pre-`version.txt` appliance) is OMITTED, not
  `null`. It answers two questions the per-PV archiver tools cannot: "am I pointed at the intended
  cluster before I trust `list_archived_pvs`/`get_pv_history` (the wrong cluster silently yields a
  complete-looking list of the WRONG PVs)?" and "is this a split/proxied deployment — which plane is
  served where?". Field names + the all-string contract are the vendor `getApplianceInfo` JSON body
  (`GetApplianceInfo.java`/`ApplianceInfo.java`) — a JSON contract, not a live measurement. Deliberate
  differences from the sibling `get_archive_info`: **no `found` key** (there is no PV present/absent
  duality) and **a served 404 PROPAGATES** as an error (it means the wrong endpoint — the retrieval
  webapp serves `/retrieval/bpl`, not `/mgmt/bpl` — not "not found"). A 2xx whose body carries no
  non-empty `identity` raises rather than fabricating an empty success (the getPVStatus/getPVTypeInfo
  S11 anchor discipline). The plane URLs embed internal cluster-member host names, surfaced
  un-redacted BY DESIGN — pure technical infra (no person data), consistent with the shipped
  `host_name`/`data_stores` fields of `get_archive_info`. `list_tools` 30 → 31, no schema change (the
  tool returns an untyped `dict`). Red-proof: `test_get_appliance_info_projects_fields` /
  `_omits_absent_fields` (exact-equality, also pin that a non-allowlisted extra — incl. a plausible
  future `serverStartEpochSeconds` — is dropped), `_unreadable_2xx_raises`, `_served_error_propagates`
  (404 + 500), and the tool disabled/enabled pair.

- **ChannelFinder query filters + exact count on `find_channels` (MA-2 CF-Query-Fläche).** The tool
  gained additive, server-side filters — `has_properties` (`{name: value-glob}`, `"*"` = present),
  `lacks_properties` (`prop!=*` — "does NOT have the property"), `not_property_values` (`prop!=value`
  — has it, value differs), `has_tags`/`lacks_tags` (OR / any-of) — plus `count_only`, which returns
  `{enabled, match_count}` from the `/resources/channels/count` endpoint (an exact, window-free count
  without pulling the matches). The wire grammar is taken from the vendor source
  (`ChannelRepository.getBuiltQuery`): negation is a trailing `!` on the key, and an unknown property
  name is a property filter, NOT a silently-ignored param, so a typo narrows the result to ~0.
  **DS-privacy:** the property-filter axis is gated to the same safe-property allowlist as the
  response projection (`resolve_safe_property_names`) — filtering on a redacted property (e.g.
  `accessGroup`) is refused, because it would reconstruct the name→value partition the projection
  hides; an empty allowlist disables property filtering (tags are not redacted, not gated). To filter
  on more properties, expand `EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES`. **Honest caveats** (as
  shipped in this entry; the property filters were later live-verified — see the "### Changed" entry;
  the tag filters remain unverified): the filter semantics are UNVERIFIED until a differential live
  probe; and 0 results cannot distinguish an unknown property name from a genuinely empty match. Guards raise
  `INVALID_INPUT` on a redacted/reserved (`~name`/`!`)/contradictory/separator-in-negation filter, so
  no path can emit the forbidden `~name!`. No new tool (`list_tools` stays 30), no schema change (the
  tool returns an untyped `dict`). Red-proof: the `_build_query_params` exact-equality tests pin both
  what is emitted and what is never emitted, plus the conditional-forwarding regression.

- **Surface already-fetched-but-discarded Archiver fields (AR-D).** Two projections harvest fields
  the appliance already returns in the SAME call and previously threw away — no extra HTTP request.
  `get_archive_status` (via `is_archived`) now also surfaces the getPVStatus connection-history
  cluster: `connection_loss_regain_count` (the flapping counter — "did the IOC connection drop, and
  how often?"), `connection_first_established` and `connection_last_restablished` (first/last
  reconnect time, `"Never"` if it never dropped). `get_archive_info` (getPVTypeInfo) now also
  surfaces the alarm/display/control limits + unit/precision (`upper_alarm_limit`=HIHI,
  `upper_warning_limit`=HIGH, the `lower_*` mirror, `*_display_limit`=HOPR/LOPR, `*_ctrl_limit`=
  DRVH/DRVL, `precision`, `units`=EGU) plus `controlling_pv`/`policy_name`/`modification_time`.
  Field names and the all-string value contract are the Archiver Appliance response as serialized by
  its own source (`EngineChannelStatus.java` for getPVStatus, `PVTypeInfo.java` for getPVTypeInfo) —
  a JSON contract, not a live measurement; the existing `if key in record` allowlist guard omits any
  field a given appliance/PV lacks. TWO honest caveats baked into the docstrings: (1) the getPVStatus
  key `connectionLastRestablished` carries an UPSTREAM TYPO (missing the second "e"), preserved
  verbatim so the projection matches the wire; (2) the nine numeric getPVTypeInfo limits are always
  present and read `"0.0"` when the PV had no ctrl info, so `"0.0"` may mean "no limit configured",
  not a literal zero — the guard cannot omit an always-present key. `lastRotateLogs` is deliberately
  NOT surfaced (the appliance never sets it → epoch-0 noise). No new tool, no schema change (the
  Archiver tools return an untyped `dict`). Red-proof: `test_get_pv_type_info_projects_fields` and
  `test_get_archive_status_surfaces_connection_cluster_and_drops_unknown` (exact-equality, also pin
  that any non-allowlisted extra is dropped).

- **`epics-doctor` alarm plane now checks its Elasticsearch backend (`backend_down`, MA-2b(e)).**
  The alarm transport probe is a blind HEAD, so it reported the plane healthy even when the alarm
  logger's Elasticsearch — which backs its search and history tools — was dead. The identity probe
  already fetches the logger's `GET /` beacon and parses the body to read its `name`; it now reads
  `elastic.status` from that SAME body (no second request) and, once the name confirms this IS the
  alarm logger, reports a new `backend_down` status (exit `1`, glyph `✗`) when the status is present
  and not `"Connected"`. `backend_down` is distinct from `unverified`: identity IS proven and the
  service reports its OWN backend broken. A missing or unreadable `elastic.status` falls back to `ok`
  — no failure is claimed that cannot be proven. The healthy sentinel (`"Connected"`) and the
  HTTP-200-even-when-down contract are pinned to the Phoebus source `SearchController.info()`; the
  shared name-classification is factored into `_classify_phoebus_name` so the S14 handling cannot
  drift between `_identify` and the new `_identify_alarm`.

- **Write-side `level` validation (OQ1).** `create_log_entry`, `reply_to_log` and
  `update_log_entry` now refuse a `level` the server does not list, and refuse a blank one
  separately (it would silently CLEAR the entry's level). Matched EXACTLY — no OR-separators, no
  wildcards, no case-folding; those are search semantics and a written level is a scalar. The
  check sits BEFORE the rate token on both paths, so a typo costs no token, and it only runs when
  a `level` was passed — a create that takes the server default still makes exactly one HTTP call.

  The premise is pinned by a **differential live probe**, not read off the Java source
  (`test_server_does_not_validate_a_written_level`): the server stores `"Urgnet"` verbatim behind
  HTTP 200, after which no level filter finds the entry, and a blank level clears the field. If a
  future Olog starts validating, that test goes red before the documentation becomes a lie.

- **`entry_id` in the FAILED write audit (OQ6).** `audit_write_failed` accepts an optional
  `entry_id`; the two EDIT paths pass it. The server archives and mutates before answering, so a
  timeout can leave an APPLIED write in front of a client that sees FAILED — the record now names
  the entry. The create call is byte-identical (a failed create still has no id). Metadata-only,
  unchanged: no owner, no free text.

### Changed

- **`tools/list` schema hygiene — lossless payload reduction (MA-Q1, precondition for new tools).**
  A single post-registration pass `_prune_tool_schemas(mcp)` (in `server.py`, run AFTER the display-tool
  registrar, wrapped in a crash-guarding `try/except` that mirrors `_load_display_registrar` — an optional
  hygiene pass must never take down the core PV server) shrinks the wire `tools/list` payload with NO loss
  of capability: **70,237 → 64,499 chars** (compact `model_dump_json(by_alias=True, exclude_none=True)`, the
  transport form; +`instructions` the budget is 66,172 vs a soft ~60k target). Two passes: **A1** strips the
  derived pydantic `title` ANNOTATIONS from every inputSchema — SCHEMA-AWARE, so the four Olog tools with a
  parameter literally named `title` (`create_log_entry`/`reply_to_log` [required], `update_log_entry`,
  `search_logbook`) keep that parameter (a naive "pop every `title` key" would delete `properties["title"]`
  and leave it dangling in `required`, silently breaking the write tools). **A2** drops the 21 information-empty
  outputSchemas (`{additionalProperties: true, …DictOutput}`, which validate nothing) via
  `Tool.output_schema = None` — an advertise-only drop: `fn_metadata.output_schema` stays intact, so the tools
  still return `structuredContent` at call time. The 11 typed Olog outputSchemas (they carry `properties`) are
  KEPT (test-pinned). `list_tools` unchanged (32 with `[displays]`, 28 core-only). New relational regression
  tests in `tests/test_server.py`, each proven able to go red via a mutant (naive strip, no strip, blanket
  drop, cleared runtime schema, removed crash-guard). This is a PARTIAL step toward the ~60k soft target:
  the remaining ~6k would need capacity-sensitive description compression, deliberately left as separate,
  scoped follow-up work.

- **ChannelFinder PROPERTY filter semantics VERIFIED against a live server (MA-2 Teil C, 2026-07-22).**
  A differential live probe (positive + negative controls) confirmed the `find_channels` PROPERTY
  filters (`has_properties`/`lacks_properties`/`not_property_values`) and `count_only` behave as
  documented, so the "UNVERIFIED until a differential live probe" caveat is dropped for them from the
  tool description, the `find_channels` docstring, `README.md` and the operator guide (the "0 results
  ≠ unknown property" honesty rule stays — a structural fact, not a provisional marker). The TAG
  filters (`has_tags`/`lacks_tags`) remain UNVERIFIED — the sandbox ChannelFinder carries no tags to
  probe them; their caveat is re-scoped to the tag axis, not dropped. Five new controls in
  `tests/test_channelfinder_live.py`, all on the surfaced `pvStatus` property: a
  positive strict-subset (`has_properties` returns a non-empty subset whose members carry the value),
  an absence-partition (`lacks_properties` + `has_properties={p: "*"}` sum to the unfiltered count), a
  value-negation (`not_property_values` drops that value only), `count_only`↔list agreement, and an
  impossible-value collapse (an unknown value → 0 — the very reason "0 ≠ unknown property" holds).
  They run under `pytest -m live` with `EPICS_MCP_CHANNELFINDER_URL` + `EPICS_MCP_LIVE_CF_GLOB`, and
  skip silently otherwise.

- **Refusals no longer report as `INTERNAL` (OQ5).** `OlogError` carries `error_code` as a class
  attribute and each subclass sets its own, mirroring `EpicsError`. `OlogRoundTripUnsafe` →
  `INVALID_INPUT`, `OlogWholeModeRequired` → `OLOG_WHOLE_MODE_REQUIRED` (it landed on
  `OLOG_WRITE_DENIED` first; corrected in place below, still unreleased),
  `OlogAttachmentDownloadDenied` →
  `OLOG_ATTACHMENT_DOWNLOAD_DENIED`. `INTERNAL` reads as transient and invited retries that each
  burned a rate token and wrote a FAILED line for a write that never happened. The new branch is
  LAST in `_olog_error_code` — the connection/response branches are themselves `OlogError`
  subclasses and would otherwise be swallowed along with the HTTP-status resolution.

- **The `level` parameter descriptions agree again (OQ3)**, all three pointing at
  `list_log_levels`, guarded by a new AST drift test so they cannot silently diverge.

### Fixed

- **The attach documentation said the opposite of the truth (OQ4).** Five places claimed an
  attach preserves "every field". `POST /logs/multipart` delegates to the server's `updateLog`,
  which runs `setOwner(principal.getName())` unconditionally (`LogResource.java:550`) — attaching
  a file REWRITES the entry's author, and the original survives only in the server-side archived
  version, which this server cannot read. **Measured live with a second principal**
  (`test_add_attachment_restamps_the_owner`): owner flips from the creating account to the write
  service account while the content is untouched. The sibling additive test could never see this —
  it creates and attaches as the same account — and its "every field is UNCHANGED" comment is now
  narrowed to "every CONTENT field".

- **The safety prose named 2 of 4 write tools (OQ8).** `EPICS_MCP_ALLOW_OLOG_WRITE` gates
  `create_log_entry`, `reply_to_log`, `add_log_attachment` and `update_log_entry`; README,
  ARCHITECTURE and the operator guide described it as "create_log_entry / reply_to_log". A site
  admin reading that section to approve the gate would conclude that only NEW entries can be
  created and unknowingly enable the destructive `update_log_entry`.

- **The archive was cited as a safety net it cannot be (OQ7).** Three places pointed at the
  server-side archived version to soften the owner re-stamp; no tool here can read or restore it.
  All three now say recovery is manual.

- **Olog log levels + the `level`/`title` search facets (OA2 / part of OA5).** New read tool
  `list_log_levels` (`GET /levels`, ungated like `list_tags`) returning the level names plus
  `default_level` — the level a create uses when none is given, reported only when the server states
  it unambiguously and `null` with an explaining note otherwise (no level flagged, several flagged,
  or the flag unreadable; the server's own seed data ships two defaults). `search_logbook` gained
  `level` and `title` filters.

  Both filters were confirmed by a **differential live probe** with a positive control, a negative
  control, and an ignored-parameter control — Olog silently drops parameters it does not know
  (`default: // Unsupported search parameters are ignored`), so "the filter returned results" proves
  nothing on its own. Two measured hazards are handled rather than documented away: an **unknown
  level returns 0 hits instead of an error**, so an empty level-filtered result now carries a note
  saying the value is not a configured level (checked only on an empty result, and a failed
  cross-check says so rather than overturning the search); and a **blank filter is refused before any
  request**, because the server's two answers to it are both misleading and disagree with each other
  (a blank `level` matches nothing, a blank `title` is dropped and returns everything).
  `title` matches whole words, not substrings — deliberately unlike `find_channels`.

  Hardened by an adversarial review of the diff (19 findings confirmed, 8 refuted), all
  re-measured against the running server before acting:

  - **`level` is trimmed the way Java trims**, i.e. only code points ≤ U+0020. Python's wider
    `strip()` removed NBSP-class spaces that the server KEEPS, so `level="\xa0Info"` was normalised
    into the configured `Info`, the cross-check saw a known level, and the empty result went out
    unannotated (measured: the server returns 0 for it and 19 for `"Info"`).
  - **The blank guard uses each field's OWN separator class.** `title` also splits on whitespace
    and on a **literal `+`** (the Java class is `[\|,;\s+]`, where the trailing `+` is a class
    member, not a quantifier), so `title="+"` produced no search term, Olog dropped the filter, and
    the UNFILTERED set came back looking filtered. `"+"` remains an ordinary `level`.
  - **The empty-result note no longer overclaims.** It is a statement about the VALUE, never about
    the cause: it is skipped for an empty PAGE past the end of a paginated set (which contradicts
    `total_matches` in the same payload), it does not judge a wildcard level (the server honours
    `Inf*`), it says an OR-ed filter still ran on its recognised parts, and it admits that another
    filter in the same search may account for the 0.
  - Documentation corrected where it was wrong: a separators-only `level` does **not** match
    nothing, it is dropped and returns the unfiltered set — a different mechanism from `""`.
  - The free-text withholding now states its honest limit: a search still answers with a hit COUNT,
    so `title=<word>` reveals whether a word occurs in some withheld title. Pre-existing via
    `text`/`desc`, not introduced here, but previously unacknowledged.

- **Olog attachments (OA1).** `create_log_entry` / `reply_to_log` now take `attachments` (workspace
  file paths, any type/size — sent as multipart `PUT /logs/multipart`, mirroring CS-Studio) and
  `embed_image_base64` (a small inline image embedded via `![](attachment/<id>)`). Two new read
  tools: `list_log_attachments` (id + metadata; filename **whole-mode only**) and
  `download_log_attachment` (raw bytes by log+filename or GridFS id, to a workspace `output_path` or
  `as_base64`). Bytes bypass the entry redaction, so a download is **withheld** unless whole-mode
  **and** the new `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` opt-in; uploads ride the existing Olog
  write gate, extended with a size cap (`EPICS_MCP_OLOG_ATTACH_MAX_BYTES`) that also bounds the
  download body (anti-OOM; a base64 download is capped further). A download `output_path` is a NEW
  file — it refuses to overwrite and refuses a symlink target (no data loss, no boundary escape).
  Verified against a live loopback Olog with a byte-identical round-trip. The no-attachment create
  path is unchanged.
- **Olog entry editing (OA3).** New write tool `update_log_entry(log_id, title?, description?,
  level?, logbooks?, tags?)` edits an existing entry via `POST /logs/multipart` (the `logEntry` part
  with no file parts — the same server core, and the same already-probed transport, as OA1b). An
  omitted argument means **unchanged**: the tool round-trips the target entry's full content and
  overlays only what was passed, so attachments, properties and unedited fields survive the
  server's destructive full-replace. **Whole-mode only**; same write gate as create, but the logbook
  allowlist is keyed on the **UNION** of the entry's current and resulting logbooks — moving an entry
  *into* a logbook and pulling it *out* of one are both writes to that logbook, so gating on either
  side alone left a hole. Compensates three server behaviours measured in the Olog source: a body
  edit is written to the raw `source` (under `markup=commonmark` the server regenerates
  `description` from it, so a new description beside a stale source would be silently overwritten);
  logbook/tag names are validated client-side (the update, unlike create, does not validate them and
  would store phantom references); and an empty title is rejected here because the server accepts it.
  An entry whose attachments have duplicate or missing filenames is **refused** rather than edited —
  attachment retention is filename-keyed (`Attachment.compareTo` inside a `TreeSet`), so such an
  entry cannot round-trip without silently losing a file. Note the server re-sets `owner` to the
  write service account on every update, and editing a legacy entry without a raw `source` makes it
  re-render the visible body (returned as a `warnings` entry). 13 guards, each mutant-red-proven.
- **Olog attach-to-existing (OA1b).** New write tool `add_log_attachment(log_id, attachments,
  embed_image_base64?)` attaches file(s) to an EXISTING entry via `POST /logs/multipart`. That
  endpoint is the server's DESTRUCTIVE `updateLog` (it `retainAll`-prunes any attachment not
  resubmitted and overwrites the entry's fields), so the tool round-trips the target entry's full
  content and only appends — the attach is purely **additive** (existing attachments and every field
  survive). **Whole-mode only** (the round-trip source is readable only from a declared local
  sandbox); against a redacted/remote server it is refused. Same write gate as create, with the
  logbook allowlist keyed on the TARGET entry's own logbooks. Verified live: create → add → both
  attachments preserved, title/body/logbooks unchanged, bytes byte-identical.
- `python -m epics_pv_mcp.find_moderate_pv`: read-only fixture finder — walks the MGMT
  event-rate report (omitting `limit` returns the whole report — near-complete, measured
  against `getAllPVs`; `limit` behaves as if applied per cluster member and merged, measured),
  filters a rate band, and counter-verifies each examined candidate (bounded by
  `--max-verify`) against the target window with a real history fetch (the report's rate is
  the appliance's own recent-window figure, never the caller's window). Deliberately a
  module, not a console script (build-once: no packaging change).
- `epics-pv://guide` resource + `OPERATING.md`: an operational cookbook (service planes,
  archiver-enumeration/retrieval-cluster/CA-bundle recipes, error signatures) shipped inside
  the package, so the server carries its own operational knowledge. Backed by one source file
  (`src/epics_pv_mcp/operator_guide.md`), drift-guarded against the tool/env surface.
- Optional `[displays]` extra: the display-aware tools (`validate_pvs`,
  `crossplane_check`, `coverage_audit`, `find_device`) now live behind an optional
  dependency on the `opi_navigation` PV engine. The **core** server (live PV access,
  diagnosis, REST-service planes) installs and starts standalone without it.
- Packaging metadata (`license`, `readme`, `authors`, `keywords`, project URLs);
  `ARCHITECTURE.md`, `CONTRIBUTING.md`, and this changelog.

### Changed

- `epics-doctor`: a FAILED identity probe (a served non-2xx like a 401/404, a transport error, or a
  refused redirect on the identity endpoint) is now reported as the new status
  `identity_probe_failed` (glyph `!`) and exits **`3`** (INCONCLUSIVE), instead of collapsing into
  `unverified`/exit `0`. A 2xx that merely could not be named (an anonymous or unreadable body, or a
  foreign service name) stays `unverified`/exit `0`. New `--json` field `inconclusive_identity_planes`
  carries the failed-probe planes (distinct from `unverified_planes`); `verification_complete` is now
  also False when a probe failed. Motivation: a probe that FAILED used to be indistinguishable from a
  reachable-but-anonymous one — the false all-clear this tool exists to catch (the S4 dead-container
  case). Migration: a script that gated on exit `0` now sees exit `3` for a reachable-but-unidentifiable
  plane and must read `inconclusive_identity_planes` alongside `unverified_planes` from `--json`. Also
  fixes the redirect-refusal message wording (`rest_get_json` / `rest_put_json`): it no longer claims
  "different host" for a same-origin redirect.
- `epics-doctor`: a service answering with a *different* known service's name is now reported
  `unverified` (exit 0) with the found name in the detail, instead of the removed
  `wrong_service` failure (exit 1). Measured motivation: a path-based reverse proxy served the
  real ChannelFinder API while the base GET answered as Olog — the old hard failure flagged a
  working configuration, and its "unambiguous at any site" rationale did not survive that
  measurement. Migration: a script that relied on exit `1` for a cross-wired URL must now read
  `unverified_planes` from `--json` (and assert `identified_planes` for positive confirmation).
- `EPICS_MCP_DEFAULT_TIMEOUT` is now honoured on the whole read/write path. Tool
  timeouts default to the configured server timeout instead of a hardcoded `5.0`.
- The 15 tool wrappers now share a single `translate_epics_errors` decorator instead of
  a copied `try/except` block.
- The live probe in `diagnose_connection` runs concurrently with the explanatory planes
  (worst-case latency ~1×timeout instead of ~2×timeout).

### Fixed

- `set_pv_value` audit blind spot on cancellation (N01): a write cancelled mid-`pv_put` (asyncio
  `CancelledError`) used to leave **no** `PV_WRITE` record even though the `asyncio.to_thread` p4p
  put keeps running and may still land at the IOC. It now emits `event=ATTEMPT` (with a correlating
  `op=<id>`) **before** the I/O and `event=UNKNOWN_PENDING` on cancel, then re-raises the cancel
  unchanged — never mislabelled `FAILED`, never blindly retried. So "every write attempt is
  audit-logged" now holds even for an interrupted write. `ALLOW`/`FAILED` carry the `op` too.
- `NamingServiceClient` normalises `base_url` (trailing-slash strip) like the other REST
  clients, so a URL configured without a trailing slash no longer 404s.
- README ↔ code drift: resource URIs, the full configuration table (incl. the optional
  REST-service and EPICS-network variables), and the uv-based development gate chain.

## [0.2.0]

Baseline: an independently developed EPICS PV MCP server on FastMCP + p4p with a
write-safety layer, batch operations, PV monitoring, cross-plane provenance
(ChannelFinder · Archiver · Alarm · Naming), device lookup, and OPI file validation.
Originally seeded from [Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server).
