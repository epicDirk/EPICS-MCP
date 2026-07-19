# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
carry breaking changes).

## [Unreleased]

### Added

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
