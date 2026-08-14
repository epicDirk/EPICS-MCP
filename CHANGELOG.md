# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
carry breaking changes).

## [Unreleased]

## [0.6.0] - 2026-08-14

### Added

- **`epics-pv://health` answers the posture questions an approver asks after the write gates.**
  Four additions, every one a boolean or a count and none of them an address: `rest_tls` (whether
  the REST planes verify certificates, whether a CA bundle is configured, and whether any plane
  speaks `https` at all, since verification is on by default and says nothing where there is no
  certificate to verify),
  `rest_read_rate_limit` (the opt-in REST GET throttle), `allowed_roots_set` (whether the opt-in
  file boundary holds a root) and `channelfinder_redaction` (how many ChannelFinder owner accounts
  and property names the redaction discloses, and whether each allowlist came from the site or from
  the built-in default). Each is named for what it MEASURES rather than for the question that
  brings a reader to it, and three of them needed that. `rest_tls.verification_enabled` resolves
  the precedence instead of mirroring `EPICS_MCP_TLS_VERIFY`, because `EPICS_MCP_CA_BUNDLE` wins
  over that switch: a server with the switch off and a bundle set does verify, and a field
  mirroring the switch would have reported it as unverified. The throttle carries its `rest_`
  prefix because a p4p PV read runs past it, so "reads are limited" without the prefix would be an
  all-clear for the reads that load an IOC. And the redaction counters say DISCLOSED, because an
  allowlist is the set of what passes through: zero is the most private posture, not a broken one.
  `allowed_roots_set` is decided by the same predicate the boundary itself asks, so a value of `;`
  or of blanks, which resolves to no root at all, reports false rather than claiming a boundary
  that no file argument is held to. Deliberately absent, because a client keeps this payload: the
  CA-bundle path, the roots, and the allowlist entries. `epics-doctor` prints the allowlist
  entries; the bundle path and the roots are printed by no surface at all and stay in the
  environment the server was started with.

- **`epics-testpv`, a seventh command: a test PV without a control system.** It serves
  `TEST:Temperature`, an analogue reading with a unit, and `TEST:Heater`, a writable switch, over
  PVAccess until Ctrl-C. The quick start previously began with `softIocPVA`, which ships with EPICS
  Base and which no page told you how to obtain, so the promise of a working PV without a facility
  was not keepable. This one needs nothing beyond the install, since p4p is already a dependency.
  It binds **loopback** unless `--interface` says otherwise, because a PVA server is a network
  service and its switch accepts writes, and it prints the port it actually bound, which is not the
  default one when that is already taken.
- **`epics-init --out PATH` writes the configuration file itself**, with `--force` to replace an
  existing one. A shell redirect cannot promise an encoding, and this block is JSON a client has to
  parse: in Windows PowerShell 5.1, `>` writes UTF-16 with a byte-order mark and
  `Set-Content -Encoding utf8` writes UTF-8 with one, and a strict parser rejects both. `--out`
  writes UTF-8 with LF on every platform. It refuses an existing file by default, because a client
  configuration usually holds other servers, and it writes nothing at all while placeholders remain,
  so filling them in and running again still works.
- **`epics-init --absolute-command`** puts the resolved path of the installed server into the block
  instead of a bare command name. A client launched from a desktop icon does not inherit your
  shell's `PATH`, which is the commonest reason a correct-looking configuration reports only that
  the server did not start. An unresolvable command is now an error rather than a silent fallback.
- **`epics-init` warns when the configuration it emits arms a write gate.** The check that follows
  prints the resulting posture (see the entry below) but does not evaluate whether such a server
  would START, and with `--no-check` no check runs at all. The warning also no longer claims that
  the loopback-only search reach is a start condition of both gates: it is one of the PV gate
  alone, while the durable audit path is required by both. It only points at "the check below"
  when one actually follows: the warning is deliberately emitted before the two branches that
  return without running a check (`--no-check`, and a block with placeholders left in it), which
  are the two cases it exists for, and on both of them it used to promise output that never came.
- **`epics-doctor` prints what each write gate would allow, and where a write can go.** A new
  `Write gates` block, in the human report and as a `write_safety` key in `--json`, covering both
  the PV gate and the Olog logbook gate. A gate that is OFF gets one line saying so; an ARMED one
  adds its allowlist (a PV name pattern, or a set of logbook names), its rate limit, and where a
  write would actually go, which is the EPICS search reach for the PV gate and the target URL for
  the logbook gate. The audit log is named either way, with whether it can be appended to, cannot,
  or could not be decided without creating the file. `--json` always carries every field.
  Informative: it changes neither the verdict nor the exit code. It reports the
  environment of the command you ran, not necessarily that of a running server, and the heading
  says so. Four states are spelled out rather than left to the reader, because the obvious reading
  of each is its opposite: an empty PV pattern on an armed gate makes the server refuse to start,
  an empty logbook allowlist denies every write, an allowlisted remote target reaches a real
  logbook rather than a sandbox, and an audit verdict that could not be decided is not a "no". The
  audit check opens an append handle and writes nothing; where a file does not exist yet it says
  the answer cannot be determined rather than guessing. A PV pattern that does not COMPILE is named
  as such instead of being shown like a working allowlist, since that too refuses the start; and
  the line about a pattern's width says what was CHECKED, a comparison against a fixed list of
  allow-everything spellings, rather than calling the pattern narrow, because a pattern can admit
  every name and still be written outside that list.

- **Every `validate_pvs` file-mode answer now names the file it is about.** The `file_path` echo used
  to appear on the empty-result answer only, so one mode came back with two different key sets and a
  client reading both had no stable key to match a result to its call. It now travels with
  `shown_by_display` and `shown_by_display_capped` as one group of file-mode fields, present together
  on both file-mode answers or on neither. Passing a NON-EMPTY `pv_names` still drops all three, and
  that is the point rather than an omission: the list wins, no file is opened, and echoing a path
  there would say the answer came from a file that was never read. An EMPTY list does not win, so
  the file is read as usual and the fields come back. The value is the argument as passed, not the
  resolved path, so it matches what the caller sent; with a symlink the counts describe the target
  while the field names the link. Purely additive: no existing field changes meaning, and the only
  visible difference on the empty-result answer is where the key sits in the object.

- **`validate_pvs` reports the glob cap, so the absence of a lower-bound note stops meaning
  "complete".** The display walk has two ways of running out of budget, and this tool only ever
  named one of them. Beside the per-display context cap there is a glob cap: a `<file>` reference
  that still carries a macro is resolved by globbing the known displays, and past the cap the
  surplus matches are dropped, which removes whole embedded SCREENS rather than instances. It can
  therefore shrink either view while both `capped` verdicts stay `false`, and a caller reading the
  quiet answer as complete was reading it wrong. A separate `notes` entry now names it, worded as a
  statement about the walked dataset rather than about the queried file, because the engine records
  the source display of a capped glob and this tool does not turn that into a per-file verdict.
  `coverage_audit` and `crossplane_check` have reported the same signal since they shipped; this
  brings the third display tool in line and adds the regression test `coverage_audit` never had.
  How much this is worth is measured rather than assumed: on a 2878-display dataset the diagnostic
  holds 16 distinct source/target pairs across 4 source displays, and lifting the cap grows 7
  display views (the largest by 651 channels). On four datasets between 13 and 485 displays it
  never fires at all. Those same figures are why the note names no file: only 3 of the 7 growing
  views appear among the reported sources, so a per-file flag built on that field would miss the
  majority of the damage while looking precise.
  All three notes also stop calling the dropped references "template" ones. Measured against the
  engine, the diagnostic is fed by every glob-resolved `<file>` reference EXCEPT the template
  ones (embedded, navtabs, open_display and the rule edges), which is the opposite of what the
  wording said; the sentence has been wrong in `coverage_audit` and `crossplane_check` since they
  shipped and is corrected in all three.

- **Python 3.14 joins the supported set, and CI tests it.** The trove classifiers, which ship
  in both the wheel and the sdist, now advertise 3.12, 3.13 and 3.14, and the CI matrix runs the
  suite on all three rather than on the two it used to. `requires-python` is unchanged at
  `>=3.12`, so nothing an installer resolves moves; what changes is that the top of the
  advertised range is now tested rather than merely permitted, which is the gap a classifier
  cannot express on its own.

### Fixed

- **A caller could write fabricated records into the audit trail.** Both write gates build their
  audit records out of values a caller chooses (a PV name; logbook and level names), and the trail
  is one record per line, so a newline in such a value ended the record and started the next one.
  Measured on a server with the PV write gate OFF, i.e. a caller permitted to write nothing at all:
  a `set_pv_value` call that was refused before any network access left three lines in the file, the
  middle one a complete, timestamp-bearing `event=ALLOW` record naming a PV nobody wrote. After that
  an `ALLOW` line no longer implies a write happened, which is what the trail exists to say. Every
  record is now escaped as it is FORMATTED, in the audit handler itself, so it also covers any
  field added later and any caller that never heard of the rule. The escape covers every
  character some reader treats as the end of a line, which is wider than it first looks: C0 and
  DEL for a byte-oriented reader such as `grep`, plus C1 (which contains U+0085 NEL) and U+2028
  and U+2029 for `str.splitlines` and any Unicode-aware log reader. **No legitimate record
  changes**: a real record's fields are identifiers, error codes and `repr`-formatted scalars,
  and the non-ASCII characters one does carry (units, accented names) all sit above U+009F. A
  record that DID carry a separator now shows it as `
` rather than breaking the line, so
  nothing a caller sent is lost.
- **`epics-testpv`'s writable switch stopped accepting writes after the first garbage collection.**
  `TEST:Heater` is documented as a writable switch, and `docs/mcp-clients.md` shows a write-enabled
  configuration whose allowlist is `^TEST:.*` so that a documented write has a target. The command
  passed its PV provider to the p4p server as an anonymous temporary, and p4p keeps no Python
  reference to one, so the first cyclic collection deallocated the shared PVs and removed the write
  handler from a server that kept serving. Reads were unaffected, which is why nothing looked
  broken. The refusal also named the wrong operation: the pinned pvxs answers an unhandled write
  with `RPC not implemented by this PV`. Writes to `TEST:Heater` now work for the life of the
  process. ⚠️ **No published version was affected**: the command itself is new in this release
  (see Added), so this repairs it before it ships rather than after. It is recorded because it
  is what a user would have met, and because it is the cause of four red CI runs on this
  repository that had been read as a flaky test.
- **An Olog write refused by the URL boundary no longer hands the caller the configured
  `EPICS_MCP_OLOG_URL`.** That field accepts `https://user:password@host/Olog`, and the refusal
  reaches a caller verbatim, so a credential written into it was disclosed on the gate's most
  ordinary path: "remote and not allowlisted" is the boundary's normal state, and no service has to
  be unreachable for the message to be produced. Measured before the fix, all four write tools
  (`create_log_entry`, `reply_to_log`, `add_log_attachment`, `update_log_entry`) answered with the
  password in clear text. **Wire change:** the message now names the variable instead of its value,
  points at `olog_write.target_allowed` in `epics-pv://health` for the gate's own verdict, and ends
  with an instruction not to route around the refusal; a client matching on the old text must be
  updated. The configured address is disclosed to a caller on no surface, which is the posture
  `epics-pv://health` and `epics-pv://config` already held for this field; an operator reads it from
  `epics-doctor`'s `Write gates` block, which needs that command's own environment to arm the gate.
  The logbook-allowlist refusal still names the logbooks it refused, which cannot carry a credential.
  ⚠️ **This closes the refusal, and the other route out for the same value is closed separately in
  this release.** A credential in `EPICS_MCP_OLOG_URL` also travelled with an ordinary HTTP failure
  of a PERMITTED target, including a loopback sandbox URL spelled with a userinfo; that is the
  shared REST layer, and the entry BELOW, "A credential in a service URL reached the client
  through error text", closes it. The server LOG stays unredacted by decision.

- **`epics-doctor`'s final verdict named one problem and hid the others.** The line an operator
  reads last named only the highest-ranking of the three honest-but-not-healthy categories
  (inconclusive identity probes, degraded planes, unverified planes) and said nothing about the
  rest, so someone who fixed what it named then discovered the next one: two problems read as
  almost finished. Measured over every state the tool can report, eleven of sixteen printed a
  verdict that hid at least one plane NAME. Every category present is now named, with its planes,
  and the earlier `(N other plane(s) also unverified)` clause is gone: it covered one of the two
  categories it outranked and gave a count where the rest of the report gives names. The `PROBLEM`
  verdict, which had named no plane at all, now names the planes that FAILED before it names the
  others, so the last line does not put the harmless ones first. Exit codes, `--json` fields and
  the per-plane block are unchanged; this is the verdict line only.
- **A password in a service URL was handed to the client in the clear.** `epics-pv://config`
  printed `channelfinder_url`, `archiver_url` and `alarm_url` exactly as configured, and those
  fields are unvalidated strings: `https://user:password@host/path` is an ordinary spelling for
  them, so a deployment that used it published its password into whatever transcript the client
  keeps. A userinfo is now removed from each of them. Everything else is kept CHARACTER FOR
  CHARACTER, because the documented use of this resource is to compare the running server's
  configuration with the block in a client's configuration file, and the redaction that already
  existed for the logbook URL cannot serve that: it rebuilds the address from the parse, which
  lower-cases the host, drops a query and a fragment and percent-encodes a space. The boundary is
  urllib3's, the parser `requests` connects through, so a password containing `@` loses its whole
  tail rather than only the part before the first one. An address whose userinfo cannot be removed
  provably is `null` instead of printed: a URL the parser refuses, one with no scheme or no host,
  a spelling in which the `@` is not a userinfo at all, one where an `@` survives the removal, and
  a cut whose result no longer names the same address. `"(disabled)"` still means the plane is not
  configured, and the two are different answers. ⚠️ A token in a QUERY STRING is not removed; that
  is the price of the character-for-character promise, and an error message drops the query for
  exactly the opposite reason (it names an address rather than being compared against one).
  ⚠️ This covers the resource, not every route out of the process; the error route was a second one
  and is closed in the entry below, while the server LOG is deliberately not. Credentials belong in
  the
  `EPICS_MCP_*_AUTH` header variables of the four planes that have one (ChannelFinder, Archiver,
  Alarm, Olog), never in a URL.
- **A credential in a service URL reached the client through error text, and through `note` fields
  of payloads that were returned SUCCESSFULLY.** Measured 2026-08-13 over every REST-backed tool:
  each one disclosed a userinfo configured into an `EPICS_MCP_*_URL`, most in the error envelope,
  and four in a payload a client keeps (`diagnose_connection`, `lookup_device_name`,
  `list_log_levels`, and `search_logbook` with a `level` filter). A server answering 401 disclosed
  it twice per message, because `requests` keeps the userinfo in the prepared URL that its own
  error text quotes. **Wire change, three shapes:** an address is now printed without its userinfo
  and without its query, or as `(unparseable)` where that cannot be proven; a served status reads
  `HTTP <code> <phrase>` from this client's own table instead of the responding server's words; and
  the three Olog listing labels and the two ChannelFinder ones name their ROUTE (`GET /levels`)
  rather than a full URL. A client matching on the old text must be updated. ⚠️ The transport cause
  is passed through unchanged, deliberately: it is the only place "connection refused", "name not
  resolved", "timed out" and a TLS failure are distinguishable, and it carries no userinfo.
  ⚠️ Two routes are unchanged and one of them has no remedy: the server log is deliberately
  unredacted, and the Naming plane has no `EPICS_MCP_*_AUTH` variable, so a credential it needs can
  only live in its URL. `epics-doctor`'s own pattern-based redaction is tracked separately.
- **`epics-doctor` printed a rebuilt Olog address next to a verdict about the configured one.** The
  `Write gates` block prints the write target with its userinfo, query and fragment removed, which
  also lower-cases the host and percent-encodes a space, while the verdict beside it comes from the
  gate's own comparison, and that one reads `EPICS_MCP_OLOG_URL` exactly and case-sensitively.
  Measured: with the host configured in mixed case the report prints, character for character, the
  string already in `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` and still says the target is not
  permitted, so an operator repairing the allowlist from that line ends up comparing two values
  that read identically and stays denied. Both target verdicts that can rest on the allowlist, the
  refusal and `REMOTE and allowlisted`, now say that the line is shown for reading and that the
  gate works from `EPICS_MCP_OLOG_URL` exactly as configured. The allowlisted one is not
  decoration: an operator tidying a working allowlist to match the printed address turns the gate
  into a deny-all, since the gate keeps comparing the mixed-case original. The note deliberately
  claims neither a repair nor that a comparison took place, and the second half of that had to be
  learned: seven states reach the refusal without the allowlist deciding anything, five of them
  vetoed as unparseable, one short-circuited by an unset `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE` and
  one denied by the `https` rule after the comparison had SUCCEEDED. The loopback verdict gets no
  note, because being loopback is a property of the address itself. The printed address is
  unchanged, and so are the exit code, `--json` and `epics-pv://health`: this is two lines of the
  human report.
- **Editing a logbook entry you had just read destroyed it, and nothing said so.** A read gives a
  body back in two shapes, the raw `source` its author wrote and the `description` the server
  rendered from it, and nothing marked which of the two `update_log_entry` wants. Since that
  tool's `description` REPLACES the whole body, the obvious sequence, read the entry, add a line,
  write it back, replaced the raw body with its own rendering, and whatever the rendering had
  dropped was gone for good: this server cannot reach the archived version. All three tool
  descriptions now name `source` as the field a round trip reads. `update_log_entry` additionally
  returns a `warnings` entry when the new body starts with the entry's own rendering and NOT with
  its `source`, which covers writing the read value straight back and appending to it. A body
  rewritten in the middle, or prepended to, is the same mistake and is not detectable that way, so
  the warning is a safety net rather than a gate; and it says nothing about how MUCH was lost,
  since the renderer rewrites plain text too.
- **A configured value could still forge a line of the `epics-doctor` report, outside the write
  block.** Control characters were escaped in the write block and nowhere else, while the report
  builds four more lines from values it did not author: the two ChannelFinder redaction allowlists
  and a plane's detail, which carries the raw EPICS search-path values. A newline in one of them
  put a complete second `Write gates` block, reading `PV write:   OFF`, above the real one reading
  `ARMED`, together with a second `Overall:` line; a raw escape byte reached the terminal, where a
  conceal sequence hides everything printed after it. Every line the report builds from a
  configured value is escaped now. Long values keep their full text where they are instructions: a
  remedy is no longer at risk of being cut in the name of this.

- **The shipped operator guide listed four of the Olog gate's six checks.** The list had already
  been corrected once, from "four" to "SIX", with a sentence saying the two easily-missed ones
  "are listed here"; the two were not added, so the sentence announcing the correction sat above
  the incomplete list it was correcting. The non-empty-target-logbooks check and the attachment
  size cap are bullets of their own now, in the order the gate applies them.

- **An ABSOLUTE audit path was reported as relative.** The `Write gates` block warned that the
  server would resolve the path against a different working directory, for paths whose meaning does
  not depend on one. The test was whether the resolved string differs, and `os.path.abspath` also
  normalises, so an absolute path written with forward slashes, or a POSIX one containing a `/./`
  segment, took the warning. It now asks whether the path is absolute; a merely respelled one is
  named as the same file.

- **A broken `EPICS_MCP_AUDIT_LOG_FILE` could kill a write-enabled server with a bare
  traceback.** Both write gates promise that an unusable audit path fails as a named
  configuration error, and two inputs escaped that promise because the clause caught only
  `OSError`: a NUL byte in the path raises `ValueError`, and a non-string raises `TypeError`.
  Both now produce the named refusal. Not reachable through the environment, since an
  environment value cannot carry a NUL, and reachable through a configuration built in
  process.

- **The documented `epics-doctor --probe-pv` example could only ever fail.** It named
  `SIM:PS-01:Cur-RB`, a synthetic placeholder that looks exactly like a real PV name, so following
  the deployment guide produced `disconnected`, exit 1, and a remedy pointing at the IOC of a PV
  that never existed.
- **The ChannelFinder privacy example switched the default off while appearing to extend it.** It
  showed `EPICS_MCP_CHANNELFINDER_SAFE_OWNER_ACCOUNTS=recsync,ioc-svc`; the built-in default is
  `recceiver`, and a value REPLACES the default rather than adding to it, so copying that line
  redacted every real owner. The example now repeats the default beside the addition and the
  replace-not-merge semantics are stated.
- **The quick start's second step told you to run the server by hand**, which is the one thing its
  own `--help` says not to do: started that way it waits silently for JSON-RPC, which reads as a
  hang, and nothing consumed it, because the client starts the server itself.
- **The quick start described the wrong output for `epics-diagnose`.** It promised "four lines
  beginning `PV:` and ending in `connected, value=21.5`", and neither half holds: a connected PV
  always gets a next-step line as well, so the report cannot stop at the live line, and where the
  PV reports an alarm severity that line carries it after the value. Measured against a real PV,
  the connected case prints seven lines. The page now describes the shape of the report rather than
  counting its lines, including the per-plane line every consulted service adds, so the next reader
  compares the right thing.
- **Two surfaces overstated what a shell redirect breaks.** The quick start and the setup prompt
  said it produces "bytes no JSON parser accepts". Measured on the three files in question:
  Python's `json.loads` reads all of them from raw bytes, because `detect_encoding` recognises the
  UTF-16 and UTF-8 byte-order marks; Node and Python in text mode reject the two that carry one.
  Both surfaces now say what the four other places describing this already said, that a STRICT
  JSON parser rejects them, and they name the shell it applies to: Windows PowerShell 5.1, not
  Windows. Measured on the same machine, a redirect in PowerShell 7 and in `cmd.exe` writes
  BOM-free UTF-8 that every parser reads. The remedy is unchanged: let `--out` write the file.
- **The refusal on an install without the display engine named three of the five commands that
  still work.** `epics-crossplane` and `epics-coverage` are the only two that need the
  `opi_navigation` engine; asked to run without it they refuse and tell the reader what else is
  available. That sentence still said "the other three commands" and omitted `epics-init` and
  `epics-testpv`, both added after it was written, so a reader on a published install was told
  that two commands they can actually run are unavailable. `docs/tools.md` carried a different
  incomplete set, omitting `epics-mcp`. Both now name all five.

- **Writing a switch by its LABEL is verified again: landed and not-landed no longer give the same
  answer.** `set_pv_value` reads every write back, but on an enum PV the value read back is the
  numeric index while the label rides in a separate block, so a written label such as `On` matched
  neither comparison: a landed and a not-landed write both came back `verified: null` with a
  `READBACK_UNVERIFIED` audit line, which is one answer for opposite facts. A written label is now
  resolved against the record's own choices, case-sensitively and first match wins, and compared by
  index: `verified: true` when it landed, `verified: false` when it did not. Writing the index
  instead was already correct and is unchanged, and `readback` still carries the index, exactly as
  `get_pv_value` reports it. This matters most on a command or reset record, which declares no drive
  limits: the pre-write bounds check does not cover it either, so the readback was its only value
  safety net. One consequence to expect there: such a record often clears itself after the pulse,
  and if it has already cleared when the readback arrives it reads back its idle state, so the write
  is reported as a mismatch rather than as unverified. That is what the readback saw; judge the
  effect of such a command from the record's own status.

- **The display tools no longer miss what sits inside a tabbed widget.** All four display-aware
  tools (`validate_pvs`, `coverage_audit`, `crossplane_check`, `find_device`) read the display tree
  through the shared navigation engine, and that engine walked a display's widgets flatly. A tabbed
  widget does not hang its content as a direct child: the display format nests it one level deeper,
  under the tab container. Everything inside a tab was therefore invisible, so navigation targets
  reached only from a tab were reported as unreferenced, and PVs that live only on a tab page were
  absent from the answer. Measured on a 97-display set, restoring the descent raises the edge count
  from 1493 to 1605 and the `open_display` edges from 92 to 204; across a larger corpus it recovers
  253 `open_display` actions and 36 navigation widgets spread over 23 files, one of them an operator
  entry point. Nothing warned about it, and that is the part worth knowing: a target made
  unreachable this way still counts as having no incoming link, and a display with no incoming link
  is seeded as an entry point, so the reachability ratio stayed at a clean 1.0 while the edges were
  missing. Expect a display set to report MORE references and MORE PVs than before, not fewer.
  Two further engine fixes ride along: a display whose glob-resolved reference matched its own file
  no longer loses its entry-point status over that guessed self-link, and the inventory walk itself
  got substantially faster on large sets. Server behaviour and every wire field are unchanged; only
  the completeness of the underlying analysis improves.

### Changed

- **BREAKING: `epics-init` refuses `--probe-pv` together with `--no-check`.** That call used to
  exit `0` and emit the block; it now exits `2` with a usage error, so a script passing both
  breaks. It was accepted in silence while `--probe-pv` was never read, because the run returns
  before the check that would have used it. `--probe-pv` is the option that turns "nothing is
  misconfigured" into "something actually works", and the quick start recommends it for that
  reason, so a user who passed both believed a PV had been probed when nothing had. Nothing else
  is lost: the emitted block is byte-identical with and without `--probe-pv` under `--no-check`.
  Drop one of the two options; without `--no-check` the PV is read as before.
- **BREAKING: `epics-init --list` refuses every option it used to swallow.** `--list --probe-pv
  NAME`, `--list --no-check`, `--list --set NAME=VALUE`, `--list --absolute-command` and
  `--list --out ""` used to exit `0` and print the preset listing while the option they carried was
  never read, because `--list` returns before any of those values is used. All five now exit `2`
  with a usage error, so a script passing one breaks. Nothing is taken away: the listing is built
  from the presets alone, so none of them could have changed it. The fifth is the one an earlier
  draft of this entry got wrong: `--list --out PATH` was already refused, but the older rule tested
  `--out` for TRUTH, so an empty value slipped past it and printed the listing. It keeps its own
  sentence, which says something the general one cannot, whenever `--out` is the only dead option
  in the call. The refusal is ONE rule that holds the parsed options against their defaults rather
  than a named rule per option, so an option added later is covered the day it is added, which is
  the gap this closes: these had accumulated one at a time. Calls that already exited `2` may say
  why differently. `--list --force` used to be answered with "add `--out`", a repair `--list`
  refuses as well; `--list --no-check --probe-pv` used to be answered as if those two were the
  problem; and `--list --out PATH` alongside another dead option now names all of them at once
  instead of sending you back for one more round per rule.
- **A failing `archiver_retrieval` plane no longer sends you to the variable that just passed.**
  With `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` empty the plane probes the MGMT URL, which is right for a
  single-JVM appliance, but a finding then opened with `EPICS_MCP_ARCHIVER_URL` and the remedy
  promises that the variable to edit is the one named at the start. On a split deployment that is
  the variable which had just been reported healthy on the line above, so following the advice
  broke the working half and left the broken half broken, while the setting that actually helps was
  never named. The finding now opens with the empty retrieval variable and says that the plane fell
  back; the MGMT variable is still named, because that URL really was the one probed. It says so
  only where the host ANSWERED and just the webapp is in doubt: when nothing answered at all, the
  MGMT plane has failed on the line above too and its address is what needs repairing, so that
  finding reads as before. Only the fallback case changes, and only its `detail` text: a plane with
  its own retrieval URL is untouched. `--json` consumers matching the old opening words of this one
  finding will not find them.
- **The `api_error` remedy no longer names one webapp as the right one for every plane.** It ended
  "for an Archiver Appliance the mgmt port and not retrieval", and the remedy table is keyed by
  status and read by every plane, so on `archiver_retrieval` it recommended the endpoint the probe
  had just failed against, which is the signature of a split deployment. It now states which
  question to ask (which webapp does this plane read, and from which variable) instead of answering
  it for one plane. Affects the `detail` text of every `api_error` finding.
- **`epics-pv://health` now says whether the server may write, and which planes it has.** It
  described the PV write gate only, so a server whose LOGBOOK gate was armed, with a service
  account and an allowlist behind it, reported `write_enabled: false` and nothing anywhere
  contradicted it. New fields: `any_write_gate_armed` (the whole write answer in one field, because
  deriving it from `write_enabled` is the mistake this fixes), an `olog_write` block with that
  gate's allowlist, rate limit and target predicates,
  `write_pattern_is_a_known_allow_all_spelling` (named for its METHOD, since true means
  certainly wide while false does NOT mean narrow: it compares the pattern against a closed
  set of spellings rather than reading the expression), `naming_enabled` and `archiver_retrieval_enabled` (the payload named four of the seven planes the
  doctor probes), and a `pv_search` block saying whether PV searches broadcast into the local
  subnets. Deliberately absent, because a client keeps this payload: the Olog URL, the audit path,
  and the raw address lists. Those stay with `epics-doctor`.
  ⚠️ **Breaking, in one field each:** `write_pattern` (health) and `pv_write_pattern` (config) are
  now `null` when no pattern is set, where they used to be the string `"(none)"`. That string
  claimed a state the server refuses to start in, since an armed gate with an empty allowlist
  raises at construction, and nothing distinguished it from a pattern whose text is that word.
- **The deployment guide answers the questions that come AFTER it starts.** Its troubleshooting
  section was entirely bring-up; it now also covers an instance that runs and may be pointed at the
  wrong facility, how to stop one (you do not, directly: an stdio server belongs to the client that
  launched it), what a runtime write refusal says, and the honest answer to "what has it read out
  of my facility", which is that no read is logged anywhere on this side. A new section covers
  uninstall and downgrade, including the four things an uninstall leaves behind: the block in your
  client configuration, the audit log on the path you chose, the framework's update-check cache,
  and any logbook entry a sanctioned Olog write created. And there is a plain statement about
  service versions, with the reasoning for not publishing a tested-versions table: what the server
  expects is documented, and `epics-doctor` plus `get_appliance_info` measure what YOUR services
  actually are.
- **`set_pv_value` no longer reads as the authority over whether a write lands.** Its description
  said "the load-bearing, client-independent guard" and closed by calling the safety layer "what
  actually gates the write". Both are now scoped to what this server decides, which is whether it
  ATTEMPTS the put; the IOC's own access security decides whether the value lands, and this server
  neither reads nor models it. The description points at `epics-pv://guide` for the detail rather
  than growing, which is this repository's own convention for that. Nothing about the gates or the
  wire changed. ⚠️ What a refusal at the IOC looks like from here is stated in the guide as **not
  measured**: no live test in this project has an IOC decline a write. What is measured, and what
  the guide sends you to instead, is the always-on readback: where the server can read the value
  back and compare it, a value that did not land comes back `verified=false`. Where it cannot,
  `verified` is `null` with a note saying why, which is the answer on three measured paths (the
  readback `pv_get` itself failed, the readback carried no live reading, or the written value is
  not comparable with what came back). `false` is a measurement and `null` is the absence of one;
  neither is a statement about the IOC's reason.
- **The alarm reply says which of its two levels answers "why".** `get_pv_info` now states in its
  own description that `alarm.status_text` is the coarse pvData NT category of the alarm SOURCE
  (DEVICE, RECORD, DB and the like) while the fine CA STAT condition (HIHI, LOLO, UDF, SIMM) is
  plain text in `alarm.message`. Nothing about the payload changed; what changed is that an
  assistant reading a PV over its HIHI threshold no longer has to guess why it says
  `status_text=RECORD`, and no longer reports a threshold breach as a record fault. `get_pv_value`,
  `get_pvs` and `monitor_pv` point at the same paragraph, since their descriptions already delegate
  the alarm block to `get_pv_info`.
- **A refused PV write now says what NOT to do next.** Both `PVWriteDeniedError` messages, the
  disabled gate and the allowlist miss, carry an instruction not to route around the refusal by
  writing a different PV or taking another route, and to report it to the operator on duty. The
  allowlist miss got it too, although only the gate was asked for: that message names a PV, and
  naming one PV is what invites trying its neighbour. The gate message still says which variable
  arms it, the escalation stands beside that remedy rather than replacing it.
- **Documentation reorganised around getting it running.** The deployment guide gains a
  troubleshooting section (symptom first: the client says nothing, the tools do not appear, the
  saved file is rejected, a write-enabled block will not start), a layout of what an install puts on
  your machine versus what only exists in a checkout, and the fact that `epics-doctor` reads the
  environment of the process YOU start rather than a client configuration file. The MCP client page
  now says where that file lives per platform, that the `command` is a bare name something has to
  resolve, and that the client must be restarted afterwards, which no page previously mentioned.
- **`docs/safety.md` states what leaves your machine for every plane**, not just for logbook reads,
  including one outbound call that is not ours: the MCP framework checks `pypi.org` for a newer
  version of itself when it prints its startup banner. Set `FASTMCP_CHECK_FOR_UPDATES=off` to stop
  it, which matters in a segmented network where it has nowhere to go.

- **`validate_pvs` accepts a `.plt` Data Browser trend, where it used to refuse one.** The
  display-PV engine collects two kinds of file, `.bob` operator screens and `.plt` trends, and this
  server was pinned to a revision from before that second kind existed. While that pin stood, the
  refusal "the inventory reads `.bob` files only, so this call can only come back empty" was simply
  true. The pin has now moved, and with it the refusal would have become a malfunction wearing the
  clothes of a safety check: a rejection whose stated reason had stopped being the case. Passing a
  trend now returns its trace channels with their connectivity, exactly like a display.
  **Nothing that worked before changes**: a `.bob` behaves as it always did, and every other suffix
  is still refused up front with `INVALID_INPUT`, before the inventory walk, for the same reason as
  before. The refusal message now names both readable kinds instead of one, so a client that
  guessed wrong learns what else it could have passed.

  **Which view finds a trend depends on how the trend is REACHED, and that is worth knowing before
  the call.** A trend embedded in a screen through a `databrowser` widget has its traces attributed
  to that screen, so only the trend's own `view="file"` finds them here; a trend opened by an
  `open_file` button is a top level in its own right and answers under either view. A trend is not
  a screen and is not reported as one: the inventory carries the kind as its own field.

  Two consequences for the other display-aware tools, neither of them a change to those tools:
  `coverage_audit` and `crossplane_check` now see the trace PVs of trends under their
  `displays_dir` root, and `find_device` can return a button-opened trend among the screens that
  show a device.

- **The four display tools now call the inventory walk's context cap by ONE name, and two of them
  stop hiding its second limit.** The same cap was named three ways across the four
  (`per-display context cap`, `per-instance context cap`, and the bare `the context cap`), and a
  fourth way turned up inside `crossplane_check` beside its own, so one service named it two ways.
  This breaks a reader rather than a computation: an assistant that has read a tool's `context_cap`
  description and then searches the notes for that wording did not find it, on the very tool it had
  just read. One of the four was also wrong. `per-instance` is what `crossplane_check` calls the
  INVENTORY, while the cap counts reachability contexts per FILE, so that note named the thing the
  cap shortens instead of the cap. Every note now reads `hit the per-display context cap`, the
  wording all four argument descriptions, both CLI help texts and the shipped operator guide
  already used. Separately, the `context_cap` descriptions of `crossplane_check` and
  `coverage_audit` now also name the GLOB cap: both tools emit a note about it, but neither
  description mentioned it, so the only two limits a caller could learn about from those two tools
  were one each. No behaviour change, no field changed, and the counts are untouched: this is the
  wording beside the numbers that were pinned together in the previous release entry. Wording is
  now pinned too, across every place a tool names the cap.

- **`find_device` no longer claims its screen list is complete, because it cannot know that.** Three
  notes and the tool description said "the screen list is complete" beside a capped live read. What
  they meant is true and now says so: the LIVE cap does not shorten the screen list. What they
  claimed is not: the screen list comes from the same inventory walk as `validate_pvs`, that walk
  has two caps of its own, and this tool reads neither, so a screen dropped by the glob cap is
  missing with nothing saying so. Wording only, no behaviour change; the caps themselves are now
  reported, see the next entry.

- **`find_device` now reports the two caps of the inventory walk, like its three sibling display
  tools.** Its screen list comes from the same macro-aware walk as `validate_pvs`,
  `coverage_audit` and `crossplane_check`, and that walk has a per-display context cap and a glob
  cap. `find_device` was the only one of the four that read neither, so a screen left out by
  either cap was simply absent from the answer. Two new `notes` entries name the count and state
  that the screen list is a lower bound. Both are statements about the run rather than a verdict
  on the query, because neither cap records the screen a device lookup returns, and the absence of
  a note means no cap fired on that run, never "complete".

- **`validate_pvs` no longer calls a file's PV list a lower bound when that list cannot grow.**
  Under the default `view="file"`, the `notes` entry warning that the macro expansion hit the
  per-display context cap now also requires that the file declares a **macro-templated** PV of its
  own. A PV carrying no macro resolves to the same channel under every binding and is already
  enumerated at every cap, so no larger budget can add anything through it and the file's answer is
  exact. On a 257-display dataset the note stops firing on four files and no file whose list can
  actually grow loses it. **Note for anyone comparing against 0.5.0:** that release said no file
  which carried the note would lose it. Those four do, and losing it is the point.
  Unchanged, and deliberately so: `shown_by_display_capped`, and the same note under
  `view="display"`, carry the DISPLAY verdict, which gets no such test and stays the more cautious
  of the two. Read a `true` there as "cannot be ruled out" rather than "known to be incomplete".

## [0.5.0] - 2026-08-02

### Added

- **`validate_pvs` can answer the other question about a display: `view="file" | "display"`.** The
  tool has always reported what a `.bob` file itself declares, attributed across every display that
  embeds it. That is the right answer for a fragment and the wrong one for a screen: a display that
  only composes embedded fragments declares nothing of its own, so it answered `total: 0` while
  resolving thousands of channels. `view="display"` asks the second question, what the file
  resolves to when opened as a display, fragments included. Measured on a 257-display dataset, the
  two views disagree on 54 files, 42 of which answered `total: 0` under the default, the largest
  hiding 5846 channels. The default stays `"file"`, so nothing changes for existing callers.
  Both views come out of the same inventory walk, so the second one is free.
- **Every `file_path` result now carries `shown_by_display` and `shown_by_display_capped`**, and
  under `view="file"` a `notes` entry says how many channels the display view adds. The counts let
  a caller see which of the two questions was answered without running the other one. The capped
  flag is computed separately from the file view's own cap verdict, which asks a different question
  (see the `Fixed` entry below).

- **`epics-init`, a sixth console command: the configuration you need, without reading the
  reference first.** It prints the MCP client-configuration block for one of four deployment shapes
  (`sandbox`, `ioc-only`, `ioc-archiver`, `full`; `--list` describes them) and then runs
  `epics-doctor` against exactly that block, so generating a configuration and checking it are one
  step. The block goes to stdout and everything else to stderr, so `epics-init --preset X >
  .mcp.json` yields a usable file. `--set NAME=VALUE` fills or adds a variable, `--probe-pv NAME` is
  passed to the doctor, `--no-check` emits only. It introduces no new `EPICS_MCP_*` variable: the
  presets set existing ones. Exit codes are the doctor's own (`0`/`1`/`3`), with `2` for a usage
  error. Three behaviours worth knowing: a value still carrying a placeholder makes it REFUSE the
  check and name it rather than report an unactionable failure (it reads the value in any case,
  `<ARCHIVER-HOST>` as well as `<archiver-host>`, and does not fire on a named regex group such as
  a write-gate pattern); a shape with no REST plane probed without `--probe-pv` is reported as
  confirming nothing, because the live plane makes no network call in that case; and `sandbox`
  searches BOTH ways a PVA client can, UDP broadcast to `127.0.0.1` and TCP unicast to
  `127.0.0.1:5075`, so it reaches a soft IOC running natively on the host as well as one in a
  container, which typically publishes only its PVA TCP port and no UDP search port.
- **`setup_epics_mcp`, a third MCP prompt.** The same walkthrough conversationally: it asks about
  each service plane in turn and ends by naming the `epics-init` command to run.
- **`monitor_pv` says whether the channel was reachable.** A new `connection` field
  (`connected` / `disconnected` / `unknown`) travels with the events, and `connection_detail`
  adds one sentence whenever there is something to explain. Zero events used to be ambiguous
  between "the PV was quiet" and "the PV was never there", and `get_pv_value` contradicted this
  tool on the second case by raising `PV_TIMEOUT`, so the two disagreed about one fact.
  `connected` is claimed only where a delivered value proves it; a stream the server ended, or
  one that reported an error, is `unknown` rather than a guess. A subscription `RemoteError` now
  reaches the caller as well, where before it only went to the log and left an unexplained empty
  result. The state is subscribed for rather than probed separately, so it describes the same run
  as the events. Purely additive: existing fields are unchanged.

### Removed

- **The Olog read redaction is gone: every logbook read returns the whole entry.** `search_logbook`,
  `get_log_entry`, the create/reply/update echoes and `list_log_attachments` now carry `title`,
  `description`, `owner` (the author), `source`, `properties` and the raw attachments list for every
  server, and `download_log_attachment` hands the bytes back (size-capped) without an opt-in. The
  redaction had been built against an assumed privacy rule that was never specified for this server,
  and it cost the logbook its point: a search returned ids whose content the caller could not judge.
  A deliberate prototype decision (2026-08-01), consequences stated in `docs/safety.md`; if a real
  facility privacy specification ever arrives, it will be rebuilt against that specification. With
  it go, user-visibly: the env vars `EPICS_MCP_OLOG_ASSUME_TEST_DATA` and
  `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` (now unknown, the config warns if still set), the
  error codes `OLOG_WHOLE_MODE_REQUIRED` and `OLOG_ATTACHMENT_DOWNLOAD_DENIED`, the `withheld`
  fields of the download/list results, and the doctor's `Olog free-text:` line together with the
  `olog_freetext_withheld` field its privacy report carried, which `epics-doctor --json` therefore
  no longer emits. The whole-mode preconditions of `add_log_attachment` / `update_log_entry` are
  replaced by the write gate itself:
  its env + URL checks now run BEFORE the pre-write read, so a target the gate refuses is never
  even read. Both write gates, the ChannelFinder redaction and the withheld-is-not-no semantics of
  the other planes are unchanged.
- **The alarm twin of that redaction is gone too.** `is_alarm_configured` returns the authored
  fields (`description`/`guidance`/`displays`/`commands`/`actions` and the serialized `config_msg`)
  with their values, exactly the handling instruction the withholding used to blank, and
  `get_alarm_history` events carry `user`/`host`/`command`/`config_msg` again. The known-field
  allowlists stay as structure: an unknown field a future logger version adds is still dropped.

### Changed

- **`validate_pvs` refuses an unusable `file_path` at once instead of walking the whole dataset
  first.** A path that is not a `.bob`, or that lies outside `displays_dir`, used to run the full
  display-PV inventory, tens of seconds on a large dataset, and only then return an empty result
  (or, in the second case, the error it could have given straight away). Neither input can produce
  a PV: the inventory reads `.bob` files only and resolves embedded targets against that same
  collected set, so the outcome was settled before the first file was opened. Both are now
  `INVALID_INPUT`, and the message names the way out (`pv_names` for a plain list of PVs).
  **A client that passed a non-`.bob` path today received a successful `total: 0`, and will now
  get an error.** The suffix comparison folds case, so `UPPER.BOB` is still a display; and a
  genuine `.bob` that declares no real `ca`/`pva` channels is unchanged, that stays an honest
  `total: 0`, not a refusal. Passing `pv_names` as well makes the list win, and the file path is
  neither read nor refused.
- **`coverage_audit` reports a missing alarm tree before doing the work, not after.** Asking for
  the alarm plane without naming a tree (`alarm_config`) was already an `INVALID_INPUT`, but the
  refusal arrived only once the display-PV walk had finished, tens of seconds on a large dataset,
  for a verdict the arguments alone decide. Same defect as the `validate_pvs` one above, in the
  sibling tool. The error itself is unchanged.
- **The `compare_machine_state` prompt no longer suggests `validate_pvs` for a non-display file.**
  With a `reference_file` such as a CSV or a JSON snapshot it now tells the client to read the file
  itself, because naming the tool would hand over a call the server is now certain to refuse. A
  `.bob` reference file (in any capitalisation) is unaffected and still uses the tool.
- **Five capped arguments now reject a non-positive value instead of answering emptily.**
  `monitor_pv.max_events`, `find_channels.max_results` and the `context_cap` of
  `crossplane_check`, `coverage_audit` and `find_device` require `>= 1`, and `monitor_pv.duration`
  requires `> 0`. A cap of `0` did not fail before: it succeeded and returned nothing, which a
  client cannot tell from "the thing you asked about does not exist". `find_channels` was the
  sharpest case, returning an empty channel list together with `capped: true`, so the answer
  claimed there was more while showing none. **A client passing `0` today will now get a
  validation error** rather than an empty result. The four sibling caps that already carried this
  bound stated the reason at the call site; it now holds everywhere, watched by a test over the
  live tool registry rather than a list, so a future tool is covered the day it is registered.
- **Ten `timeout` arguments now reject zero and below.** `get_pv_value`, `get_pvs`, `get_pv_info`,
  `set_pv_value`, `discover_pvs`, `diagnose_connection`, `find_channels`, `is_archived`,
  `validate_pvs` and `find_device` require `> 0`, which the other nineteen timeouts already did.
  **A client passing `timeout=0` today will now get a validation error** naming the argument,
  before any request exists. This was deferred once as a mere inconsistency, on the assumption
  that a zero timeout fails honestly rather than fabricating an answer. Measured over all ten,
  that assumption held for five and was wrong for the other five, which returned a
  plausible-looking result instead: `find_device` answered "No operator-facing screen references
  this device", `validate_pvs` reported the PV as disconnected, `diagnose_connection` named a
  cause, `discover_pvs` and `get_pvs` came back empty. Two of the five that did raise pointed at
  the wrong thing: `PV_TIMEOUT` blames the device rather than the argument, and `is_archived`
  surfaced `INTERNAL` with a server-side traceback for what is a caller input error. The registry
  guard that watches this now covers every numeric argument rather than only the integer half,
  and for a `number` it requires an EXCLUSIVE lower bound, so a future `ge=0` cannot reintroduce
  the same defect.
- **`epics-crossplane --help` and `epics-coverage --help` now answer on a core-only install.** They
  used to report the missing display engine and exit `2` instead, so on any install from a package
  index, where that engine is never present, the first answer to `--help` was an instruction to
  install something you do not need in order to read a help text. Both commands now parse their
  arguments before asking for the engine, and `--version` answers there too, so every console
  command explains itself everywhere. They still need the engine to do their work and still say
  so when asked to do it.
- **A usage error on those two commands reports the engine, and its exit code is the engine's.**
  `epics-coverage --nope` on a core-only install answers with the missing engine rather than with
  `the following arguments are required`, because supplying the argument would not make the command
  run either. The code is `2` where the engine is absent, as before, and `1` where the engine is
  installed and fails to load, which is the code the same command returns on that install with
  correct arguments.
- **The source distribution declares what it contains.** It did not, and an undeclared sdist is not
  "the tracked tree": the backend packs the working tree minus what version control ignores, so it
  also packs any untracked file that happens to be lying there at build time. Measured on the 0.4.0
  artifact: 192 files, which was every tracked file plus a stray log file, and it included all 81
  test modules, `scripts/`, `.github/` and `CLAUDE.md` (instructions meant for an assistant working
  in the repository, not for anyone installing the package). The sdist now carries the package, the
  documentation and the standard metadata files, 96 files, and a test builds it and compares its
  contents against version control in both directions, so neither a stray file nor a silently
  dropped directory can reach a package index again. The wheel is unchanged.

### Fixed

- **`validate_pvs` answered `total: 0` without a word when the macro expansion had been cut short.**
  The honesty note that says the PV list is a lower bound was raised inside the loop that filters a
  display's PVs down to this file's own resolved channels, so on a file where nothing survived that
  filter the loop body never ran and the note could never appear. That is exactly the answer a
  reader is most likely to take at face value: measured on a 257-display dataset, 9 files hold a
  file view that provably grows with a larger context budget, and the 2 the tool stayed silent on
  were both answering `total: 0`, one of them resolving 5576 channels once the budget allowed it.
  The verdict now also fires when the contexts reaching the file itself were dropped, on an empty
  and a non-empty result alike, and it is guarded: a file that declares no PV at all keeps its
  silent, exact `total: 0`, because calling that a lower bound would be a false statement. No file
  that carried the note before loses it. The `view="display"` verdict is unchanged.
- **The shipped status legend explained 9 of the 12 states `epics-doctor` can print, and two of its
  marks not at all.** `disabled`, `info` and `disconnected` were never named by their status name
  anywhere in the guide, and the marks `·` and `i` had no legend entry, so an operator could see a
  character the document travelling with the server did not explain. All twelve states now have a
  row, every mark is explained, and the legend is held TOTAL against `PlaneStatus`: a state that is
  not documented is a failing test rather than a decision. Still not documented, and now recorded as
  such: the `Overall:` verdict line and the privacy block.
- **The guide explained what a report line SAYS but never what it is CALLED, and one plane had no
  entry at all.** The guide's plane bullets are grouped by service and count six; `epics-doctor` is
  grouped by check and prints seven, because the Archiver's management root and its retrieval root
  are separate planes. `archiver_retrieval` therefore appeared under no bullet, and a reader seeing
  that line had nothing to look it up by. Both orderings were individually correct, which is why
  prose alone never caught it. The guide now names all seven planes in the spelling the report uses,
  and says which `--json` field carries them.
- **The guide's advice for sizing `EPICS_MCP_READ_RATE_LIMIT` was wrong for one of the two tools it
  named, and unusable for the other.** It said a multi-GET tool such as `coverage_audit` **or**
  `crossplane_check` spends "several tokens per audited PV". Measured at the throttle itself,
  `crossplane_check` is not per-PV at all: it spends **2 tokens in total**, the same for one display
  PV or a thousand, and **3** when the IOC device name turns out not to be registered, which is the
  finding it exists to report. Anyone who sized a limit from the old sentence over-provisioned for
  that tool by whatever their PV count was, and would still have been caught out on the one run that
  finds something. `coverage_audit` is per-PV, and now says how much: **1 + 2N** with both per-PV
  planes requested, rising to **1 + 3N** when the audited PVs are not alarm-configured, because a
  missed alarm lookup re-asks for the bare tree. There is no single per-PV figure, and the guide now
  says so instead of implying one.

### Internal

- The typography guard sees the doubled hyphen at the START and the END of a line, not only between
  two spaces. That is where the form lands when a sentence wraps, and it was the one place the rule
  could not look. `scan` pads each line with one space per side, so the rule table is unchanged; the
  two occurrences this uncovered in the tree were corrected with it. The reach, the four legitimate
  forms the widening also catches and the blind spots that remain are recorded in
  `docs/known-limits.md` section 10.
- Two guards hold the shipped guide's new plane inventory: the region against a real `run_doctor`
  run, and the plane-name literals in `services/doctor.py` against that same run rather than against
  the guide, so a name spelled in the module that never reaches a report is reported as the dead
  branch it is instead of as a documentation gap. The literal scan derives which call shapes carry a
  plane name from the parameter name rather than from a list, after a hand-kept list was measured to
  miss six positional literals. What neither guard holds is recorded in `docs/known-limits.md`
  section 14.

## [0.4.0] - 2026-07-30

### Added

- **Every `epics-doctor` line that reports a problem now names its remedy.** Three statuses already
  did (`ca_error` named the CA bundle variable, `api_error` the mgmt/retrieval mix-up, `config_error`
  the variable to set) and four did not: `unreachable` printed only the transport error,
  `backend_down` named the consequence rather than the fix, and `disconnected` and
  `identity_probe_failed` said nothing about what to do. The remedies now live in one table keyed by
  status, appended to the observation and never replacing it, so `--json` readers get them in the same
  `detail` field. An `unreachable` plane also names the variable it reads its URL from, which is the
  one case where the reader cannot tell from the message which variable to look at. `unverified`
  and `no_ingest` deliberately stay without one: the first already carries the specific clue it
  measured, and the second is a fault inside the appliance, not in this configuration.
- **`--version` on all five console commands** (`epics-mcp` had it; `epics-doctor`,
  `epics-diagnose`, `epics-coverage` and `epics-crossplane` did not), from one shared helper, so the
  version source and the command-name prefix cannot drift apart. Each prints its own name and the
  package version. Note: on the two display-aware commands the engine check still runs before the
  arguments are parsed, so on a core-only install those two report the missing engine instead; that
  limit is pinned by a test rather than left to be rediscovered.

- **`epics-doctor` notices an Archiver appliance that is not ingesting.** `getApplianceInfo` proves
  that an appliance is answering and nothing more: one whose engine has never spoken to its IOCs
  answers it exactly like a healthy one, so the doctor printed `ok` for a deployment that was
  archiving nothing. That is a wiring fault, the class this tool exists to catch. The identified
  appliance is now also asked `getApplianceMetrics`, and two things make it a finding: channels held
  with **none** connected, or the appliance's own `status` reporting a stopped webapp. The second is
  the worse fault and is invisible in the counts, because when the engine webapp does not reply,
  mgmt cannot merge its numbers and they vanish from the row while `pvCount` survives, with the
  payload still served as HTTP 200.
- **New plane status `no_ingest` (`~`), exit `0`.** Deliberately not a failure: a freshly
  commissioned or fully paused appliance is legitimately in this state, and failing the run would
  make `epics-doctor` cry wolf in every CI job that calls it. It is not silent either, and that is
  what the next entry is for.
- **New `--json` field `degraded_planes`.** The planes that proved their identity and are measurably
  not doing their job. It exists because every other signal stays clean for such a plane: `ok` and
  `verification_complete` remain `true`, `unverified_planes` and `inconclusive_identity_planes`
  remain empty, and `identified_planes` even lists it, since its identity IS proven. A script
  written against the documented field list would otherwise read a non-archiving archiver as
  positively confirmed. Purely additive; no existing field changes meaning and no exit code moves.

### Changed

- The archiver plane now issues **three** requests on a healthy run rather than two, and the ingest
  probe uses a single attempt with a 15 s floor instead of the shared retrying session. urllib3
  applies the timeout per attempt, and this route fans out to three internal requests per cluster
  member: measured against a 16-member cluster it answers in 7.3 s but takes 23.3 s to fail under
  the default 3-retry policy, which would have made the check blind there while slowing every run.
- The archiver identity probe goes through the same shared beacon fetcher as every other plane. It
  was the one plane building its request inline, which already made that helper's "the one place
  every identity probe issues its request" contract untrue.
- **`epics-doctor`'s `config_error` line now names the variable to set at its START, and the
  remedy points at that position.** Measured against the last published release: 0.3.0 ended the
  observation with "Set EPICS_MCP_ARCHIVER_URL (the MGMT webapp URL)", the remedy table then took
  that instruction over, and it now reads "Set the variable named at the start of this finding",
  the construction `unreachable` already used. This matters beyond wording because `detail` is the
  field `--json` readers are told to use, so a consumer matching the older text will not find it.
  The position is guarded; the wording deliberately is not.
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
- **The core server no longer dies over an OPTIONAL capability.** The display-capability probe called
  `importlib.util.find_spec` bare, and it runs at module level, so an import hook that RAISES for
  `opi_navigation` took the whole `epics_mcp.server` import down: exit 1 with a traceback, on a
  server whose PV tools never needed the display engine at all. The probe now answers instead of
  propagating. A module a finder reports as genuinely absent is the supported core-only state and
  stays silent, as before; a finder that could not answer is a different claim and is logged loud
  rather than wearing the "not installed" message. The sibling CLIs already behaved this way, so one
  package was answering the same question two ways.
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
