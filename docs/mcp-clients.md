# MCP client integration

Ready-to-paste blocks for `.mcp.json` or `claude_desktop_config.json`, from read-only localhost to a write-enabled test setup.

[Back to the README](../README.md)

Add the server to your `.mcp.json` or `claude_desktop_config.json`.

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

**With the REST planes enabled:**

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
        "EPICS_CA_AUTO_ADDR_LIST": "NO"
      }
    }
  }
}
```

The last three are not optional hardening, they are start conditions, and a block without them is
one an MCP client reports only as "server not connected". A write-enabled server refuses to start
unless `EPICS_MCP_AUDIT_LOG_FILE` names a durable path (an audit trail on stderr vanishes on
restart) and unless the EPICS client search reach is loopback-only, so that enabling writes cannot
silently arm a process that reaches a real facility network. Point the audit path somewhere your
server user can write.

