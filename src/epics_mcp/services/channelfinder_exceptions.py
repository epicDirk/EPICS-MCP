"""Exceptions for the ChannelFinder REST client (read-only).

A per-service base plus connection/response errors, deriving from the shared
:mod:`epics_mcp.services.rest_exceptions` roots. The client stays decoupled from the MCP-facing
``EpicsError`` hierarchy (the tool layer translates these for the ``ToolError`` mapping);
``except RestClientError`` catches every plane, ``except ChannelFinderError`` just this one.
"""

from epics_mcp.services.rest_exceptions import (
    RestClientError,
    RestConnectionError,
    RestResponseError,
)


class ChannelFinderError(RestClientError):
    """Base error for the ChannelFinder client."""


class ChannelFinderConnectionError(ChannelFinderError, RestConnectionError):
    """Failed to establish a connection to ChannelFinder."""


class ChannelFinderResponseError(ChannelFinderError, RestResponseError):
    """Unexpected response (HTTP error / bad payload) from ChannelFinder."""
