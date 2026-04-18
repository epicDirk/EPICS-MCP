"""EPICS PV MCP Server — PV access via p4p (PVAccess + Channel Access)."""

try:
    from importlib.metadata import version

    __version__ = version("epics-pv-mcp")
except Exception:
    __version__ = "0.2.0"
