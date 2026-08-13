# MCP client integration

Ready-to-paste blocks for `.mcp.json` or `claude_desktop_config.json`, from read-only localhost to a write-enabled test setup.

[Back to the README](../README.md)

## Which file, and where it lives

Add the server to the configuration file your client reads. Which file that is, and where it sits,
belongs to the client rather than to this server, so treat the following as the common cases and
your client's own documentation as the authority.

A **project-scoped** `.mcp.json` sits in the directory you opened, and is the easy case: it is right
there. A **desktop application** keeps a file per user instead, `claude_desktop_config.json` under
`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS, and
`~/.config/Claude/` on Linux. ⚠️ Two things about that Windows path: `%APPDATA%` is cmd.exe syntax,
so write `$env:APPDATA` in PowerShell, and nothing expands either form inside a JSON value, where a
path has to be written out in full with its backslashes doubled.

⚠️ **Check the shape your client expects before pasting.** The blocks below use `mcpServers` with a
`command` and an `env`, which is what the clients this server was tested against read. A client that
expects a different key, or an `args` array, will ignore a correct-looking block in exactly the way a
missing one is ignored: silently. If nothing appears after a restart and the file is where you think
it is, compare its shape against your client's own example before debugging anything here.

> `epics-init --preset <shape>` prints a block of its own for each of four shapes and then runs
> `epics-doctor` against what it printed, so you can skip the copying. `epics-init --list` names the
> shapes, and `--out PATH` writes the block to a file with an encoding a client can read, which a
> shell redirect cannot promise. The write-enabled block at the bottom is deliberately NOT a preset:
> turning a write gate on is a decision to make deliberately, not one to inherit from a flag.

⚠️ **`command` is a bare name, and something has to resolve it.** Every block below says
`"command": "epics-mcp"`, which works only if the process that launches the server can find that name
on its own PATH. A client started from a desktop icon, a menu or a service manager usually does NOT
inherit the PATH of your interactive shell, so a byte-correct block can still fail, and the client
will report no more than that the server did not start. When that happens, put the absolute path into
`command` instead. `epics-init --preset <shape> --absolute-command` writes that path for you and
errors out if it cannot resolve one, which is the shorter route and the one to prefer. By hand:
`which epics-mcp` prints it on Linux and macOS, `where.exe epics-mcp` on Windows. In JSON on
Windows, remember that every backslash in that path has to be doubled.

## Read-only (the default posture)

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-mcp",
      "env": { "EPICS_MCP_PROVIDER": "pva" }
    }
  }
}
```

Read-only is what this block sets: both write gates stay off, and every REST plane stays disabled
until its `*_URL` is set. It does **not** confine the server to localhost, and this heading used to
say it did. EPICS defaults the auto-address search to **ON**, so a configuration that sets no
search variables broadcasts PV searches into the local subnets. To be reached-nothing rather than
merely read-only, disable the auto search explicitly:

```json
"env": {
  "EPICS_MCP_PROVIDER": "pva",
  "EPICS_PVA_AUTO_ADDR_LIST": "NO",
  "EPICS_CA_AUTO_ADDR_LIST": "NO"
}
```

Run `epics-doctor` to see what your instance actually reaches; it reports `localhost-isolated` only
when every search list is unset **and** the auto search is off.

**With the REST planes enabled**, which assumes you RUN those three services, on this machine, on
these ports. Replace the URLs with your own, or delete the lines for services you do not have: an
unset URL disables that plane with no network call, whereas a wrong one produces four failing lines
in the next self-check (four, not three: the archiver is probed as two planes, mgmt and retrieval).
The one URL an unset value does NOT switch off is `EPICS_MCP_ARCHIVER_RETRIEVAL_URL`, which falls
back to the mgmt URL.

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-mcp",
      "env": {
        "EPICS_MCP_CHANNELFINDER_URL": "http://localhost:8080/ChannelFinder",
        "EPICS_MCP_ARCHIVER_URL": "http://localhost:17665",
        "EPICS_MCP_ALARM_URL": "http://localhost:8081"
      }
    }
  }
}
```

**Writes enabled for test PVs only** (triple-gated: the pattern is required, and an empty one refuses to start):

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-mcp",
      "env": {
        "EPICS_MCP_ALLOW_PV_WRITE": "true",
        "EPICS_MCP_PV_WRITE_PATTERN": "^TEST:.*",
        "EPICS_MCP_AUDIT_LOG_FILE": "/var/log/epics-mcp/audit.log",
        "EPICS_PVA_AUTO_ADDR_LIST": "NO",
        "EPICS_CA_AUTO_ADDR_LIST": "NO",
        "EPICS_PVA_ADDR_LIST": "127.0.0.1",
        "EPICS_CA_ADDR_LIST": "127.0.0.1"
      }
    }
  }
}
```

The two address lists are what makes this block able to REACH anything: with the auto search off and
no list set, the client searches nowhere at all, so a write-enabled server would start and then fail
to find the very PV its allowlist permits. `127.0.0.1` keeps it loopback-only, which the write gate
requires, while still finding a local test PV such as the one `epics-testpv` serves.

The three write variables are not optional hardening, they are start conditions, and a block without
them is one an MCP client reports only as "server not connected". A write-enabled server refuses to start
unless `EPICS_MCP_AUDIT_LOG_FILE` names a durable path (an audit trail on stderr vanishes on
restart) and unless the EPICS client search reach is loopback-only, so that enabling writes cannot
silently arm a process that reaches a real facility network.

⚠️ Only the FIRST of those carries over to the LOGBOOK write gate,
`EPICS_MCP_ALLOW_OLOG_WRITE`, which is a separate decision with its own variables. Measured: the
loopback-reach refusal and the empty-pattern refusal both hang off `EPICS_MCP_ALLOW_PV_WRITE`, so an
Olog-write-enabled server starts with the subnet broadcast search on. Its own boundary is a
different one, on the write TARGET rather than the search reach: loopback, or an exactly
allowlisted https URL with remote writes enabled. `epics-doctor` prints which of the two applies to
your configuration in its `Write gates` block.

Point the audit path somewhere your server user can write, and note two things about it. Its parent
directory has to exist already, since the server opens the file rather than building a tree, and a
missing directory is one more way to meet that same silent "not connected". On Windows, choose a
machine-wide location rather than a user profile and write the path out in full: this is a JSON
value, so nothing expands a `%VARIABLE%` reference in it and every backslash has to be doubled.

## Then restart the client (this applies to every block above)

A client reads this file when it starts and launches the server itself, so **a block you just
pasted changes nothing until the client reloads it**. Restart the client, or use its reconnect
command if it has one. Until then the tools are simply absent, and most clients report nothing at
all rather than "not loaded yet", which is indistinguishable from a configuration that is wrong.
So make the restart the step you take before you start diagnosing.

