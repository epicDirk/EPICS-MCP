# Quick start: from an install to a first answer

Three commands, and nothing to obtain beyond the install: `epics-testpv` serves the PV for you, so
there is no facility, no IOC, no EPICS Base, no ChannelFinder and no archiver in the way.

[Back to the README](../README.md)

**This page starts after installation.** It assumes the package is installed and its commands
answer on your PATH; if `epics-doctor --version` does not print a version, install it first
(README, "Installation") and come back.

1. **Serve a test PV**, in a terminal of its own. It runs until Ctrl-C and binds loopback only:

   ```bash
   epics-testpv
   ```

2. **Write the client configuration, and check it in the same step.** `--out` writes the file
   itself, which a shell redirect cannot do reliably: in Windows PowerShell 5.1 it produces bytes
   a strict JSON parser rejects.

   ```bash
   epics-init --preset sandbox --out .mcp.json --probe-pv TEST:Temperature
   ```

   The block goes to stdout, the check to stderr, and a `live ok` line means the server reached the
   PV from step 1. Without `--probe-pv` no PV is contacted, so a clean report would say only that
   nothing is misconfigured.

   ⚠️ **Already have a `.mcp.json` with other servers in it?** Then this refuses rather than
   overwrite it, which is the point. Write to a new file (`--out epics-pv.json`) and paste the one
   entry into your existing `mcpServers` object, or pass `--force` if the file is yours to replace.

3. **Point your MCP client at that file and restart it**, see
   [MCP client integration](mcp-clients.md): a client reads its configuration at startup, so
   until it is restarted the tools are simply absent. Then ask the assistant to read
   `TEST:Temperature`. Or skip the assistant entirely:

   ```bash
   epics-diagnose TEST:Temperature
   ```

   which prints a short report: the PV, its state and the likely cause, then one line for the live
   result (`connected, value=21.5`, plus the alarm severity where the PV reports one) and one for
   each service plane that was consulted, and last the next steps, any notes, and any plane that
   was asked for but is unavailable.

   ⚠️ **That command reads YOUR shell, not the file step 2 just wrote.** The block in `.mcp.json`
   configures the server your MCP CLIENT launches; a command you run yourself sees the environment
   of your terminal, and a fresh terminal points at no PV at all. Measured: run exactly as shown in
   a shell with no `EPICS_*` variables, it answers `disconnected (PV_TIMEOUT)` while the test PV is
   serving perfectly well. Two ways round it, neither of which edits anything. Set in that terminal
   the search path the `sandbox` preset sets, which for the test PV is
   `EPICS_PVA_AUTO_ADDR_LIST=NO` plus `EPICS_PVA_ADDR_LIST=127.0.0.1`; or let step 2's own check
   answer, since it applies the preset itself. Without `--out` it writes no file, printing the
   block on stdout and the check on stderr:

   ```bash
   epics-init --preset sandbox --probe-pv TEST:Temperature
   ```

   ⚠️ **If the client reports only that the server did not start**, the likeliest cause is that
   `"command": "epics-mcp"` is a bare name and a client launched from a desktop icon does not
   inherit your shell's `PATH`. Rerun step 2 with `--absolute-command`, which writes the resolved
   path into the block instead and refuses rather than guessing when it cannot find one. Since
   step 2 already wrote that file, add `--force` to replace it, or write a new one and copy the
   entry across.

⚠️ Note what step 1 is: a PVAccess server, and its second PV accepts writes. It binds loopback
unless you pass `--interface`, and it says which port it got, which is not the default one when that
is already taken.

## Now point it at a real control system

The [deployment guide](deployment.md) is the page for that, and it starts where this one ends:
`epics-init` prints a client-configuration block for one of four deployment shapes and checks it in
the same step, and the guide then walks the variables plane by plane, the CA-bundle recipe for
internal HTTPS and the documented assumptions. What every facility shape needs is the PV search
path, which is why a preset leaves one placeholder per protocol for it; section 1 explains the
placeholder mechanics and `--set`, and section 5 explains why a containerised IOC usually needs
`EPICS_PVA_NAME_SERVERS` alongside an address list rather than instead of it.
