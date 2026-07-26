# Examples

Minimal, self-contained examples. The core ones need nothing but EPICS + this server:
no Phoebus, no display layer.

> New here? Point the assistant at the `epics-pv://guide` resource (also
> [`OPERATING.md`](../OPERATING.md)) for the service landscape, the operational recipes and the
> error signatures. It ships inside the server, so nothing extra to install.

## 1. A live test PV (core: works for any EPICS user)

Serve a single PVAccess PV with the EPICS base `softIocPVA` tool:

```bash
softIocPVA -d test.db
```

Then, in another shell, exercise the core server with no MCP client:

```bash
epics-diagnose TEST:Temperature      # connection diagnosis (exit 0 even on disconnect)
```

or read it from an MCP client (see `mcp.json` for a ready-to-paste config) and ask the
assistant to `get_pv_value("TEST:Temperature")`.

## 2. MCP client config

[`mcp.json`](mcp.json) is a ready-to-use, read-only, localhost configuration. Drop its
`mcpServers` entry into your `.mcp.json` or `claude_desktop_config.json`.

## 3. A sample display (optional, only if you use Phoebus / CS-Studio)

[`sample_display.bob`](sample_display.bob) is a tiny Phoebus `.bob` screen that references
`TEST:Temperature`. It demonstrates the **display-aware** tools:

```bash
# needs the optional [displays] extra: pip install epics-pv-mcp[displays]
epics-crossplane --displays . <path-to-st.cmd>
```

or `validate_pvs(file_path="examples/sample_display.bob")` from an MCP client.

> The `.bob`-handling comes from the `opi_navigation` library (the optional `[displays]`
> extra), **not** from any Claude skill; the server itself runs standalone. If you don't
> use Phoebus/CS-Studio, ignore this example; the core PV tools need none of it.
