# EPICS PV MCP Server

MCP server for EPICS Process Variable (PV) access via [p4p](https://mdavidsaver.github.io/p4p/) — supporting both PVAccess and Channel Access protocols.

Based on a fork of [Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server), extended with [FastMCP](https://github.com/jlowin/fastmcp), the p4p library, a write-safety layer, batch operations, PV monitoring, and OPI file validation.

## Tools

| Tool | Description |
|------|-------------|
| `get_pv_value` | Read a single PV's current value |
| `get_pvs` | Batch-read multiple PVs in one call |
| `set_pv_value` | Write a value to a PV (requires safety gate) |
| `get_pv_info` | Connection state, data type, alarm status, limits |
| `monitor_pv` | Subscribe to PV updates for a given duration |
| `validate_pvs` | Extract PV names from a `.bob` display file and check connectivity |
| `discover_pvs` | Search for PVs matching a glob/regex pattern |
| `crossplane_check` | Cross-plane PV provenance: display PVs ↔ e3 IOC `st.cmd` (+ optional `.db`) ↔ Naming Service (read-only) |
| `find_device` | Find which operator screens show a device, read its channels live (capped), and join the serving IOC |
| `find_channels` | ChannelFinder lookup: which IOC/host serves a PV + tags/properties (disabled until `EPICS_MCP_CHANNELFINDER_URL` set) |
| `is_archived` | Whether a PV is archived (EPICS Archiver Appliance; disabled until `EPICS_MCP_ARCHIVER_URL` set) |
| `get_pv_history` | Archived samples for a PV over an ISO-8601 window (disabled until `EPICS_MCP_ARCHIVER_URL` set) |

## Resources

| URI | Description |
|-----|-------------|
| `health://status` | Server health: version, uptime, provider, p4p version |
| `config://epics` | Non-secret configuration values |

## Prompts

| Prompt | Description |
|--------|-------------|
| `diagnose_pv` | Step-by-step PV diagnosis workflow (info, read, monitor) |
| `compare_machine_state` | Compare current PV values against expected state |

## Safety

Writes are **disabled by default** and require explicit opt-in:

- **Environment gate** — `EPICS_MCP_ALLOW_PV_WRITE=true` must be set
- **Regex allowlist** — `EPICS_MCP_PV_WRITE_PATTERN` limits which PVs can be written (e.g. `^TEST:.*`)
- **Rate limit** — `EPICS_MCP_WRITE_RATE_LIMIT` caps writes per minute (default: 10)
- **Audit log** — every write *attempt* is logged with timestamp, PV name and an `event` (`ALLOW` succeeded, `DENY` rejected by gate/allowlist/rate-limit, `FAILED` errored during the put), plus old/new value (on `ALLOW`/`FAILED`), `error_code`, and `caller` (the MCP tool — a stdio server has no authenticated end-user)

## Installation

```bash
pip install -e ".[dev]"
```

Run the server:

```bash
epics-pv-mcp
```

## Configuration

All settings are read from environment variables with the `EPICS_MCP_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_PROVIDER` | `pva` | Protocol provider: `pva` (PVAccess) or `ca` (Channel Access) |
| `EPICS_MCP_DEFAULT_TIMEOUT` | `5.0` | PV operation timeout in seconds |
| `EPICS_MCP_MAX_BATCH_SIZE` | `100` | Maximum PVs per batch read |
| `EPICS_MCP_MAX_MONITOR_DURATION` | `60.0` | Maximum monitor subscription duration in seconds |
| `EPICS_MCP_MAX_MONITOR_EVENTS` | `1000` | Maximum events per monitor subscription |
| `EPICS_MCP_ALLOW_PV_WRITE` | `false` | Enable PV writes |
| `EPICS_MCP_PV_WRITE_PATTERN` | _(empty)_ | Regex allowlist for writable PV names |
| `EPICS_MCP_WRITE_RATE_LIMIT` | `10` | Maximum writes per minute |
| `EPICS_MCP_AUDIT_LOG_FILE` | _(empty)_ | Path to audit log file (empty = stderr) |

## Claude Code Integration

Add to your `.mcp.json` or `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
      "env": {
        "EPICS_MCP_PROVIDER": "pva",
        "EPICS_MCP_ALLOW_PV_WRITE": "false"
      }
    }
  }
}
```

To enable writes for test PVs:

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
      "env": {
        "EPICS_MCP_PROVIDER": "pva",
        "EPICS_MCP_ALLOW_PV_WRITE": "true",
        "EPICS_MCP_PV_WRITE_PATTERN": "^TEST:.*"
      }
    }
  }
}
```

## Development

Run tests:

```bash
pytest tests/ --tb=short
```

Lint and format:

```bash
ruff check src/ tests/
ruff format src/ tests/
```

## License

MIT

## Credits

- Original server by [Jacky1-Jiang](https://github.com/Jacky1-Jiang/EPICS-MCP-Server)
- Extended with FastMCP, p4p, safety layer, batch ops, monitoring, and OPI validation by epicDirk
