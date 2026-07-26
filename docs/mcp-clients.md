# MCP client integration

Ready-to-paste blocks for `.mcp.json` or `claude_desktop_config.json`, from read-only localhost to a write-enabled test setup.

[Back to the README](../README.md)

Add the server to your `.mcp.json` or `claude_desktop_config.json`.

## Read-only, localhost

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
      "env": { "EPICS_MCP_PROVIDER": "pva" }
    }
  }
}
```

**With the REST planes enabled:**

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
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
      "command": "epics-pv-mcp",
      "env": {
        "EPICS_MCP_ALLOW_PV_WRITE": "true",
        "EPICS_MCP_PV_WRITE_PATTERN": "^TEST:.*"
      }
    }
  }
}
```

