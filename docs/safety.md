# Safety and network posture

What this server does by default, what the write gates guarantee, and what decides its network reach. If you are approving this for a facility, read this together with [SECURITY.md](../SECURITY.md).

[Back to the README](../README.md)

This is a controls tool, so the trust questions come first.

- **What leaves your machine, and where it goes.** Answering a question means handing the answer to
  whoever asked, which here is the MCP client, the model provider behind it, the conversation
  transcript and any logs that client keeps. That is what the tool is FOR, and it is not reversible,
  so the honest version is a list of what travels: PV names, their values, units and alarm state
  (live); registered channel names with their IOC and host, after the ChannelFinder redaction below
  (registry); archived samples and retention settings (history); alarm configuration and history
  (alarm); device names (naming); **whole logbook entries in clear text, author included** (logbook,
  see the entry below, which is the sharpest case); and for a display file you point it at, its path
  and its macro-expanded PV inventory (display). What decides how much of that exists is your
  configuration, not this list: a plane whose URL is unset is never contacted at all, and the live
  plane reaches exactly what the EPICS search environment allows, which `epics-doctor` prints back to
  you. None of it is reported anywhere else, and this server has no telemetry of its own.
  ⚠️ **One connection is not ours and is on by default:** the MCP framework this server is built on
  (FastMCP) asks `pypi.org` about ITSELF when it prints its startup banner. Stated precisely, since
  this is the page for it: it sends none of your data and caches the answer for hours, but it fetches
  that package's whole metadata document rather than a single number, and the request itself tells
  pypi.org your address and that this host runs it. Where it cannot connect it gives up after roughly
  two seconds and the server starts regardless, so it delays rather than blocks. Set
  `FASTMCP_CHECK_FOR_UPDATES=off` in the same env block to remove it.
- **Read-only by default.** The mutating tools are gated off. `set_pv_value` is **triple-gated**:
  `EPICS_MCP_ALLOW_PV_WRITE=true` **and** a regex allowlist (`EPICS_MCP_PV_WRITE_PATTERN`,
  **required** when writes are on: an empty pattern makes the server **refuse to start** rather
  than silently allow every PV; use `.*` to deliberately allow all) **and** a per-minute rate
  limit. Every write is audit-logged: a rejected one is `DENY`d at the gate (nothing sent); an
  accepted one logs `ATTEMPT` before the I/O, then a terminal `ALLOW`/`FAILED`, or `UNKNOWN_PENDING`
  if it was cancelled mid-put (the value may still land at the IOC, so verify by read-back and never
  blindly retry). A write-enabled server **refuses to start** unless `EPICS_MCP_AUDIT_LOG_FILE` names
  a durable path: an ephemeral stderr audit would lose this trail on restart.
- **Every write is bounds-checked (always-on).** Before the put, the written value is checked
  against the record's own drive limits (`control` DRVL/DRVH); an out-of-range value is refused with
  `PVWriteBoundsError` (`PV_WRITE_OUT_OF_BOUNDS`) plus a `BOUNDS_DENY` audit event **before it
  reaches the IOC**: the name/rate gate only allowlists the PV name, never the value. A record with
  no declared limits (or a non-numeric value) is not bounds-checkable; the write proceeds and the
  result's `bounds_note` says so.
- **Every write is read back (always-on).** After a sanctioned put the server reads the value back
  and compares it to what was written: the result carries `verified` (true within tolerance / false
  mismatch / null not verifiable) plus `readback`/`tolerance`/`note`, and a `READBACK_OK` /
  `READBACK_MISMATCH` / `READBACK_UNVERIFIED` audit line follows the `ALLOW`. A mismatch is **not**
  an error (the put happened); it is a loud `verified=false` plus the audit line, the direct
  countermeasure to a silent wrong-write. Tolerance is the record's `control.min_step` (> 0) or
  `EPICS_MCP_READBACK_TOLERANCE`. On an **enum** PV (a switch, a command, a reset) a written
  **label** is compared by index, exact and without a tolerance: it is resolved against the record's
  own choices exactly as the write path resolves it, and `readback` stays the numeric index. Writing
  the index instead stays on the numeric compare, tolerance included. This lane needs the readback
  most, because a command record declares no drive limits and so is not bounds-checked either. One
  consequence is worth knowing before you meet it: a command record that clears itself after the
  pulse may already have cleared when the readback arrives, so it reads back its idle state and the
  verdict is `verified=false`. That is what the readback saw, not a claim that the command failed.
- **Writes enabled require a loopback-only search reach.** Enabling writes with the EPICS client
  search env able to reach beyond loopback makes the server **refuse to start** (`SafetyConfigError`):
  the name-pattern above scopes *what* may be written, this gate scopes *where* a write can
  physically go, and both must hold. To write, the address lists (`EPICS_PVA/CA_ADDR_LIST`) and
  name servers (`EPICS_PVA/CA_NAME_SERVERS`) must be loopback hosts only, and the subnet broadcast
  must be off (`EPICS_PVA_AUTO_ADDR_LIST=NO` **and** `EPICS_CA_AUTO_ADDR_LIST=NO`; unset means ON).
  The check is parser-faithful (a spelling the real client rejects still counts as broadcasting) and
  resolution-free (a hostname is never trusted as loopback). This makes "read the facility, write
  the facility" a start-time impossibility, not a discipline.
- **Olog logbook write is a *separate* gate.** All four write tools, namely `create_log_entry`, `reply_to_log`, `add_log_attachment` and `update_log_entry` (the last two MUTATE an existing entry), need
  `EPICS_MCP_ALLOW_OLOG_WRITE=true` **and** a **test-server URL boundary** (only a loopback Olog,
  or an exact **https** URL in `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` with
  `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true`; a non-loopback/private host, and any plain-http remote,
  is refused by default, so a production write is a deliberate double action) **and** a logbook allowlist
  (`EPICS_MCP_OLOG_WRITE_LOGBOOKS`; empty = deny-all) **and** a rate limit. The author is the write
  service account (`EPICS_MCP_OLOG_WRITE_USER`), set server-side and not spoofable; the audit line
  is metadata-only (never the title/description free text). **`ALLOW_PV_WRITE` is untouched by it.**
- **The effective write posture is inspectable, so you do not have to take this page's word for it.**
  `epics-doctor` prints a `Write gates` block, and `epics-doctor --json` carries the same under
  `write_safety`: for each gate whether it is armed, what it allows by name, its rate limit, and
  **where** a write could physically go, which is the PV search reach for one gate and the Olog
  target URL for the other. It also names the audit log and says whether it can be appended to.
  Two limits, stated because they decide how much the block is worth to you. It describes the
  environment of the command YOU ran, and a server started by an MCP client may have been given a
  different one; and it reports what the gates are SET to without evaluating whether such a server
  would start, which for an armed gate is a separate question with the conditions listed above.
- **Olog reads return the whole entry, a deliberate prototype decision (2026-08-01).** Every read
  (`search_logbook`, `get_log_entry`, the create/reply/update echoes, attachment listing and
  download) returns the full server record: `title`, `description`, `owner` (the author's
  account), `source`, `properties` and the raw attachments. **Consequence, stated plainly:** an
  AI assistant reading a production logbook hands its clear text, author names included, onward
  to the MCP client, the model provider, the conversation transcript and any client-side logs.
  The former DS-PRIVACY read redaction was removed because it was built against an *assumed*
  privacy rule that was never specified for this server, and it cost the logbook its point (a
  search returned ids whose content you could not judge, and a write could not verify what it
  wrote). If a real facility privacy specification ever arrives, it will be rebuilt against that
  specification; the removed mechanism is in the git history (up to 2026-08-01). The Olog client
  still refuses redirects instead of
  following them. Note this is a *runtime output* decision, unrelated to keeping
  person data out of committed files (see `CLAUDE.md`).
- **Network reach is the launcher's decision, not this server's.** PV searches follow the
  standard EPICS env: the address lists (`EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST`), the name
  servers (`EPICS_PVA_NAME_SERVERS`: TCP unicast, **not** subnet-bound), and the auto-address
  search (`*_AUTO_ADDR_LIST`), which EPICS defaults to **ON** when unset, so even an empty
  environment broadcasts PV searches into the local subnets. A deployment may well point the env
  at a real facility, so do **not** assume isolation from this document; run `epics-doctor` to
  see what your instance actually reaches (it claims `localhost-isolated` only when every search
  list is unset and the auto search is explicitly disabled). The write gates above hold either way.
- **Optional service planes are off until configured.** ChannelFinder, Archiver, Alarm and
  Naming stay disabled until their `*_URL` env vars are set; an empty/unset URL means *no
  client and no network call*. The ESS Naming plane has **no built-in host**, so no egress
  unless you set `EPICS_MCP_NAMING_URL`.
- **Opt-in path boundary.** File/dir tool arguments are canonicalized and existence-checked;
  set `EPICS_MCP_ALLOWED_ROOTS` to confine them to specific roots (empty = no boundary).


## Strict response schemas

**Strict response schemas (S11).** Every REST client validates a 2xx payload against the
measured schema of its endpoint. An unreadable payload **raises a loud error**, except in
`get_pv_history`, whose existing `status` channel reports it as `withheld`, and is **never**
minted into a definitive answer. (`is_alarm_configured`'s `null` stays the *readable-but-
tree-ambiguous* verdict; an unreadable payload there raises like everywhere else.) Definitive
negatives come only from each service's measured signal: Archiver `getPVTypeInfo` HTTP 404 and
Olog `get_log_entry` HTTP 404. **Naming HTTP 204/404 is definitive only once the responder proves
it is the Naming Service** via its `/rest/swagger.json` beacon (S13); otherwise the "not
registered" answer is withheld, so a foreign/misconfigured URL's 404 no longer mints a false
`name_typo`. (Archiver and Olog 404 stay ungated: the same latent question, deferred as
lower-severity.) On every top-level listing and record, junk list items are never silently
dropped, missing fields never zero-filled, non-strings never stringified into names. The one
deliberate exception is metadata INSIDE an already-anchored record (a channel's
properties/tags, a log entry's logbook/tag names): there a malformed element is dropped rather
than sinking the whole record (documented at the two projection helpers), and even there
nothing is ever fabricated (a `null` never becomes the string `"None"`).
