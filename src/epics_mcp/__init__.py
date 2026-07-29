"""EPICS MCP: live PV values, connection diagnosis, and cross-plane provenance."""

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("epics-mcp")
except PackageNotFoundError:
    __version__ = "0.4.0"
