# Safety and network posture

What this server does by default, what the write gates guarantee, and what decides its network reach. If you are approving this for a facility, read this together with [SECURITY.md](../SECURITY.md).

[Back to the README](../README.md)

**This page is the fullest statement of that posture, and the shorter statements elsewhere point
here.** Six of them: `README.md`, `SECURITY.md`, `docs/tools.md`, `docs/deployment.md`, the shipped
operator guide, and the server's own `instructions` header, which reaches a client that has no
repository and therefore points at the `get_guide` tool instead of at this file. Each keeps a
version sized for its own reader, deliberately: someone who finds only a cross-reference on the
page they opened is worse off than someone who finds the answer. What those copies must not do is
age apart, which is what `tests/test_write_posture_sites.py` holds, over exactly this list. The
gate SPECIFICATION is a different question for a different reader (whoever builds the next gate)
and lives in [write-gate-contract.md](write-gate-contract.md).

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
  ⚠️ **What was READ is not recoverable afterwards, and that cuts both ways.** The audit log records
  write attempts and gate verdicts only; no read leaves a line, and on a read-only deployment, the
  default, the only records that can arise at all are the DENY lines of refused write attempts, and
  with no `EPICS_MCP_AUDIT_LOG_FILE` configured they go to the process's stderr rather than into a
  file. So "what did the assistant take out of my facility in the
  last hour" has no answer on this side, and the record that does exist is the MCP client's own
  conversation log. Decide with that in mind rather than discovering it later. The operational half
  of these questions is in the deployment guide: whether a running instance is pointed at the
  facility you think ([troubleshooting](deployment.md#6-troubleshooting)), whether it works against
  YOUR service versions and how to check rather than take our word
  ([versions](deployment.md#will-it-work-against-my-version-of-these-services)), and how to remove
  it again ([uninstall](deployment.md#7-removing-it-again-and-going-back-a-version)).
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
  limit. **Three is the number of GATE checks, and one more refusal sits behind them:** a value
  outside the record's own drive limits is rejected once the gate has already admitted the write
  (`BOUNDS_DENY`, nothing reaches the IOC). It is counted separately on purpose, because it has
  spent a rate token where a gate denial never does. Every write is audit-logged: a rejected one is `DENY`d at the gate (nothing sent); an
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
- **PV writes enabled require a loopback-only search reach.** ⚠️ The PV gate only: this condition
  and the non-empty-pattern one hang off `EPICS_MCP_ALLOW_PV_WRITE`, while the durable audit path is
  required by both gates. Measured: an Olog-write-enabled server starts with the subnet broadcast
  search on, and its own boundary is on the write target instead (see the Olog bullet below).
  Enabling PV writes with the EPICS client
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
  (`EPICS_MCP_OLOG_WRITE_LOGBOOKS`; empty = deny-all) **and** a rate limit. Two further checks are
  easy to forget because they are not configuration: the entry must NAME at least one target
  logbook (an empty target list is refused before anything else), and an attachment payload over
  `EPICS_MCP_OLOG_ATTACH_MAX_BYTES` is refused with its own code. **Six checks in all**, each
  fail-closed and each audited before the raise. The author is the write
  service account (`EPICS_MCP_OLOG_WRITE_USER`), set server-side and not spoofable; the audit line
  is metadata-only (never the title/description free text). **`ALLOW_PV_WRITE` is untouched by it.**
  ⚠️ **A refusal from the URL boundary names the variable, not its value**, and that is deliberate:
  `EPICS_MCP_OLOG_URL` is an unvalidated string that accepts `https://user:password@host/Olog`, this
  refusal fires on the gate's ordinary state ("remote and not allowlisted", nothing has to be
  broken), and its text reaches the caller verbatim. Measured 2026-08-14, before it was closed, all
  four write tools answered with the configured password in clear text. What a caller gets instead
  is the gate's own verdict, `olog_write.target_allowed` in `epics://health`, out of the process
  that answered. The address stays operator-side: `epics-doctor`'s `Write gates` block deletes the
  userinfo and drops the query and fragment, but it prints that line only when the environment of
  THAT command arms the gate, and a URL it cannot prove it has redacted prints as `(unparseable)`.
  ⚠️ That block REBUILT the address from the parse until 2026-08-14, and rebuilding was measured
  to leak: for `https://svc:p@ss/w0rd@host/Olog` the parser reads the host as `ss`, so the rebuild
  printed part of the password inside the path, with no `@` left for a structural check to see.
  Deleting and then asking the parser to confirm the result is the same address is what closed it. The
  logbook-allowlist refusal does still name the logbooks it refused: a logbook name cannot carry a
  credential, and which denials may name their target is decided per surface, never copied from a
  sibling gate. ⚠️ This covers the REFUSAL, and the ordinary HTTP failure of a PERMITTED target
  used to be a second route out for the same value, including a loopback sandbox spelled with a
  userinfo. That route is closed as well since 2026-08-14, in the shared REST layer rather than
  here: measured at the built artefact over every REST-backed tool and both failure kinds, 16 of 16
  disclosed a configured password before the fix and none does after it. What remains open, by
  decision rather than oversight, is the LOG, which the entry below is about.
- **The server's own log is NOT a redacted surface, and it is a different channel from the answer.**
  Everything above is about what travels toward the MCP client. An UNEXPECTED internal error is
  handled the other way round on purpose: the caller gets only the exception's class name
  (`[INTERNAL] <ClassName>`), while the full message and its traceback go to the server log, which
  is what makes such a bug debuggable at all. That detail can include a service URL exactly as
  configured, credentials included. **Treat the log as carrying whatever the process environment
  carries**, and put credentials in `EPICS_MCP_*_AUTH` rather than in a `*_URL`, which is the only
  thing that removes them from this channel entirely.
  Three qualifications, because each one decides how much this actually costs you, and stating the
  exposure without them would overstate it. The shared REST layer also names a request URL, but only
  for a request that FAILED and only at `DEBUG`, which Python's default root level already
  suppresses; this server sets no log level of its own, so how much of that a deployment sees is the
  embedder's decision, not a switch here. Where the stream goes is likewise not this server's to
  say: it speaks stdio unless `FASTMCP_TRANSPORT` says otherwise (see the caveat further down), so
  the stderr belongs to whatever launched the process. And the audit logger's stderr fallback is
  **not** a state an armed gate can be in: a write-enabled server refuses to start without a durable
  `EPICS_MCP_AUDIT_LOG_FILE` (above), so that fallback only ever carries the DENY lines of a server
  whose gates are off.
- **The effective write posture is inspectable, so you do not have to take this page's word for it.**
  `epics-doctor` prints a `Write gates` block, and `epics-doctor --json` carries the same under
  `write_safety`. A gate that is OFF gets one line saying so, which is the whole answer for it. For
  an ARMED gate the block adds what it allows by name, its rate limit, and **where** a write could
  physically go, which is the PV search reach for one gate and the Olog target URL for the other.
  It also names the audit log, and says whether it can be appended to, cannot, or could not be
  decided without creating it. `--json` always carries every field, armed or not.
  Two limits, stated because they decide how much the block is worth to you. It describes the
  environment of the command YOU ran, and a server started by an MCP client may have been given a
  different one; and it reports what the gates are SET to without evaluating whether such a server
  would start, which for an armed gate is a separate question with the conditions given per gate
  in the bullets above.
  ⚠️ **The first limit does not apply to the resource route.** `epics://health` carries the same
  posture out of the process that is actually answering: `any_write_gate_armed` for the one-field
  question, the `olog_write` block for that gate's allowlist, rate limit and target predicates,
  `pv_search` for the live plane's reach, and a posture group for REST TLS verification, the REST
  read throttle, the opt-in file boundary and the ChannelFinder redaction. Read through the client,
  it needs no shell and no guess about whose environment you are looking at. Three things it
  withholds are things the CLI does print as a VALUE and this payload carries only as a boolean or
  a count, because a resource payload is kept by the client: the Olog target URL, the raw
  search-reach strings and the ChannelFinder allowlist entries. The audit path is a fourth the CLI
  prints, and this payload does not carry it in ANY form, not even as a boolean. Two more, the
  CA-bundle path and the allowed roots, are withheld here and printed by no surface at all; for
  those the answer is the server's environment. So for the write gates the
  block above remains the fuller answer; for TLS verification, the read throttle and the file
  boundary the resource is the ONLY answer, and in both cases it is the trustworthy one about
  **this** process.
  The sibling resource `epics://config` does print three service URLs, and the same premise
  lands differently there rather than identically: those three hosts were a deliberate disclosure
  from the start, so the decision was not whether to print an address but what may travel with it.
  A userinfo may not: `https://user:password@host/path` is a spelling those unvalidated fields
  accept. Everything else in such a URL survives character for character, because comparing that
  value against a client's own configuration is what the resource is for, and a query string is
  therefore NOT redacted (`docs/known-limits.md`, entry 17). Credentials belong in
  `EPICS_MCP_*_AUTH`, which four of the planes have.
  ⚠️ **The error route is closed too, on a different rule.** Measured 2026-08-13 a REST failure
  carried the request URL into a `note` of a successfully returned payload; measured 2026-08-14
  after the fix it carries none, across both failure kinds and every REST-backed tool. A message
  names an ADDRESS, so it drops the userinfo AND the query and says `(unparseable)` where it cannot
  prove the result names the same address; this resource keeps the query because being comparable
  is what it is for. What still carries a credential is the log above, by decision. `epics-doctor`
  stood here too until 2026-08-14, on the strength of a local pattern-based redaction that cut at
  the first `@`; that redaction is gone, its cause texts cross the same barrier as everything else,
  and no unredacted exception was found that could reach the old one in the first place.
  ⚠️ **A local FILE PATH is withheld on the same rule, at BOTH write gates.**
  An unwritable `EPICS_MCP_AUDIT_LOG_FILE` is refused with `SAFETY_CONFIG_INVALID`, and neither
  gate names its value: both name the variable and tell the reader to check it and the directory's
  permissions on the server host. The path stays in the exception's `details`, which nothing in
  `src/` reads and which the tool boundary does not send.
  ⚠️ **This said "in the one place it could reach a caller" until 2026-08-20, and the PV gate was
  that sentence's exception on the argument that it is built eagerly at start-up, so that only the
  operator on stderr reads it. Measured in-process, the argument is false at the DEFAULT posture.**
  `get_safety` is a lazy singleton; the eager construction in `server.main` runs only under
  `if config.allow_pv_write`; `set_pv_value` is registered unconditionally; and the write tool
  calls `get_safety()` before it checks whether writes are allowed at all. So with PV writes off
  and a set-but-unopenable audit path, which an Olog-write deployment is required to configure, the
  first `set_pv_value` call from any caller answered with the full path, account name included,
  twice over (once from the `!r`, once inside the chained `OSError`, which stringifies with the
  filename in it). Both gates now give the same answer, and the operator loses an echo rather than
  the actionable half: the variable is named and its value is theirs to read back from the
  environment of the process they started.
- **Olog reads return the whole entry, a deliberate prototype decision (2026-08-01).** Every ENTRY
  read (`search_logbook`, `get_log_entry`, the create/reply/update echoes) returns the full server
  record: `title`, `description`, `owner` (the author's
  account), `source`, `properties` and the raw attachments. The two ATTACHMENT tools project
  narrowly instead: the listing returns an attachment's id, filename and description, the download
  returns the bytes with their size and content type, and neither carries the entry's title, body
  or owner, even though the listing has to read the whole entry to find the attachments. **Consequence, stated plainly:** an
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
- **One command in this package LISTENS, and it is the only one that both listens and accepts
  writes.** Everything else on this page is about outbound reach; `epics-testpv` is the exception,
  and it is worth knowing about before someone runs it on a facility machine. It is a real
  PVAccess server: it serves `TEST:Temperature` and `TEST:Heater`, and the second one takes writes
  from anybody who can reach it, with no gate of any kind, because it exists so a quick start needs
  no control system. It binds **loopback** unless `--interface` says otherwise, so its default
  reach is this host. It is the only command here that opens a listening service **of its own**:
  the other six are one-shot CLIs or, in the server's case, speak stdio, and none of them has a
  host or port option. Do not leave it running, and do not give it `--interface 0.0.0.0` on a
  machine that others can reach.
  ⚠️ **One caveat on that, because it is not ours to close.** The MCP framework reads its own
  `FASTMCP_*` environment, and `FASTMCP_TRANSPORT` set to an HTTP transport makes `epics-mcp`
  listen on `FASTMCP_HOST`/`FASTMCP_PORT` instead of speaking stdio. No option of ours does that
  and nothing here sets it, but a deployment that sets it has a second listening service, gated
  only by whatever the framework provides. If you are approving this, confirm that variable is
  unset.
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
