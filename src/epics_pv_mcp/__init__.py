"""EPICS PV MCP Server — PV access via p4p (PVAccess + Channel Access)."""

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("epics-pv-mcp")
except PackageNotFoundError:
    __version__ = "0.2.0"
