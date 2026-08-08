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

[`mcp.json`](mcp.json) is a ready-to-use, read-only configuration, confined to localhost to match
the local `softIocPVA` above. Drop its `mcpServers` entry into your `.mcp.json` or
`claude_desktop_config.json`.

The confinement is the four `EPICS_*ADDR_LIST` settings, not the absence of configuration: EPICS
defaults the auto-address search to **ON**, so leaving them out would broadcast PV searches into
your local subnets. Drop them when you point this at a real IOC.

`epics-doctor` reports this as `search paths: EPICS_PVA_ADDR_LIST (127.0.0.1); ...` rather than
`localhost-isolated`, and that is correct: it reserves the latter for a server that searches
**nowhere at all** (no list set, auto search off), which would not find the `softIocPVA` above.

## 3. A sample display (optional, only if you use Phoebus / CS-Studio)

[`sample_display.bob`](sample_display.bob) is a tiny Phoebus `.bob` screen that references
`TEST:Temperature`. It demonstrates the **display-aware** tools:

```bash
# needs the opi_navigation engine, which is NOT available from PyPI (private repository);
# from a checkout that has it: uv sync --extra dev --group displays
epics-crossplane --displays . --st-cmd <path-to-st.cmd>
```

or `validate_pvs(file_path="examples/sample_display.bob")` from an MCP client. That default answers
what the file itself declares; add `view="display"` to ask what it resolves to when opened as a
display, fragments included. The two differ as soon as a screen embeds anything, and every
file-mode answer reports `shown_by_display` so you can see which question you asked, next to a
`file_path` echo naming the call it answers. Both belong to file mode: pass a non-empty `pv_names`
instead and they are gone, because no file was opened.

> The `.bob`-handling comes from the `opi_navigation` library (the optional `displays`
> dependency group), **not** from any Claude skill; the server itself runs standalone. If you don't
> use Phoebus/CS-Studio, ignore this example; the core PV tools need none of it.
