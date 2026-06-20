"""Exceptions for the ChannelFinder REST client (read-only).

Mirrors ``naming_exceptions.py``: a per-service base plus connection/response errors,
so the client stays decoupled from the MCP-facing ``EpicsError`` hierarchy (the tool
layer translates these into ``EpicsError`` for the ``ToolError`` mapping).
"""


class ChannelFinderError(Exception):
    """Base error for the ChannelFinder client."""


class ChannelFinderConnectionError(ChannelFinderError):
    """Failed to establish a connection to ChannelFinder."""


class ChannelFinderResponseError(ChannelFinderError):
    """Unexpected response (HTTP error / bad payload) from ChannelFinder."""
