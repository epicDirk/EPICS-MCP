# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/epicDirk/EPICS-MCP/security/advisories/new)
on this repository. Please do not open a public issue for a suspected vulnerability.

Include what you can: the tool or CLI involved, a configuration that reproduces it (with any
site-specific host or PV name replaced by a placeholder), and what an attacker would gain. This is
a pre-1.0 project with a single maintainer, so expect an acknowledgement rather than a
service-level guarantee.

## Supported versions

Only `main` is supported. There is no backport branch, and pre-1.0 minor versions may carry
breaking changes.

## Security posture

The server is built to be safe to point at a control system without a separate review of every
setting.

- **Read-only by default.** Every mutating tool is off unless explicitly enabled. `set_pv_value`
  needs `EPICS_MCP_ALLOW_PV_WRITE=true` **and** a non-empty regex allowlist of writable PV names
  **and** a per-minute rate limit. Writes enabled with an empty pattern makes the server refuse to
  start, rather than silently permitting every PV.
- **Two independent write gates.** The Olog logbook gate (`EPICS_MCP_ALLOW_OLOG_WRITE`) is separate
  from the PV gate, with its own allowlist, its own rate limit and its own URL boundary. Enabling
  one never enables the other.
- **Writes require a loopback-only search reach.** A write-enabled server whose EPICS client search
  environment can reach beyond loopback refuses to start. "Read the facility and write the facility"
  is a start-time impossibility here, not a matter of discipline. The check reads the reach with the
  same parser the real client uses and never trusts a hostname as loopback.
- **Every sanctioned write is bounds-checked and read back.** The value is checked against the
  record's own drive limits before the put; an out-of-range value is refused before it reaches the
  IOC. After the put, the server reads the value back and reports whether it landed, so a silent
  wrong write surfaces as `verified=false` plus an audit line.
- **Mandatory, metadata-only audit.** A write-enabled server refuses to start unless a durable audit
  path is configured, because an audit nobody can read after the process exits is a promise rather
  than a record. The audit carries identifiers, the writing principal and bounded scalars. It never
  carries free text (a title/description body, a filename).
- **No network reach without configuration.** Each optional REST plane (ChannelFinder, Archiver,
  Alarm, Naming, Olog) stays disabled until its `*_URL` is set: unset means no client and no network
  call. PV reach follows the standard EPICS search environment, which the launcher controls and this
  server does not. Run `epics-doctor` to see what an instance actually reaches.
- **Output redaction (ChannelFinder only).** ChannelFinder owners and property values pass a
  site-configurable allowlist. Olog entries come back WHOLE (title, text, author, attachments):
  the former Olog read redaction was removed 2026-08-01 as a deliberate prototype decision, see
  `docs/safety.md` for the stated consequences.

## What this is not

The write gates are a **guardrail on the sanctioned path, not a security boundary.** This is stated
plainly because the word "gate" invites a category error:

- A gate guards writes **through this server**. Anyone with a shell, or with the same EPICS client
  library this server depends on in order to run, can reach the same target without passing the
  gate. That path is outside the gate's reach by construction. It is the gate's shape, not a hole to
  be patched inside the server.
- The real boundary, where one is wanted, lives outside this process: network reach, account
  privileges, an external reconciliation watchdog. This server does not claim to be one, and no
  reader should mistake it for one.
- The audit's promise is therefore *every gate verdict, and every write through this server that
  reaches the I/O*. It is not *every write*.

If you are deciding whether to deploy this in a facility, that distinction is the one to carry into
the review. The full contract every in-server write gate must satisfy, including the deny paths and
their evidence, is in [docs/write-gate-contract.md](docs/write-gate-contract.md).

## Dependencies

The EPICS client is [p4p](https://mdavidsaver.github.io/p4p/), distributed as prebuilt wheels that
bundle the EPICS Base libraries, so no separate EPICS Base build takes part. REST access uses
`requests`. Secret scanning and push protection are enabled on this repository; dependency updates
are watched by Dependabot.
