"""Read-only client for the EPICS ChannelFinder REST API.

ChannelFinder is the runtime PV directory: which IOC/host serves a PV, plus the tags and
properties RecSync/recceiver report. This client issues **GET queries only** — it never
writes. Verified against the ChannelFinder REST docs (channelfinder.readthedocs.io):

  GET {root}/resources/channels?~name={glob}&~size={limit}   — query channels by name glob

The configured ``channelfinder_url`` is the ChannelFinder **service root including any
context path**, e.g. ``http://cf-host:8080/ChannelFinder``; ``/resources/channels`` is
appended. Querying needs **no authentication** ("No authentication or encryption is
required to query the service"); an optional ``Authorization`` header is forwarded for
proxied/secured deployments. Results are capped (``~size``) to avoid pulling a whole
directory on a broad pattern like ``*``.

Structure mirrors :mod:`epics_pv_mcp.services.naming_client` (Session + Retry on
502/503/504 + typed projection + per-service exceptions).
"""

from __future__ import annotations

import logging
from typing import TypedDict

import requests

from epics_pv_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderResponseError,
)

logger = logging.getLogger(__name__)

# Default upper bound on returned channels — a broad glob (``*``) can match a whole site.
DEFAULT_MAX_RESULTS = 500


class ChannelInfo(TypedDict):
    """Projected, read-only view of one ChannelFinder channel."""

    name: str
    owner: str
    ioc_name: str | None
    host_name: str | None
    properties: dict[str, str]
    tags: tuple[str, ...]


class ChannelFinderClient:
    """Read-only client for the EPICS ChannelFinder REST API. GET-only."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        auth_header: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json"})
        if auth_header:
            self.session.headers.update({"authorization": auth_header})

        # Retry transient failures (502/503/504) with exponential backoff (as naming_client).
        from requests.adapters import HTTPAdapter

        try:
            from urllib3.util.retry import Retry

            retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        except ImportError:
            pass  # urllib3 retry unavailable — proceed without

    @property
    def channels_url(self) -> str:
        return f"{self.base_url}/resources/channels"

    def find_channels(
        self,
        name_pattern: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[ChannelInfo]:
        """Query channels by name glob (``*``/``?``), capped at *max_results*.

        Returns the projected channels (possibly empty). Raises
        :class:`ChannelFinderConnectionError`/:class:`ChannelFinderResponseError` on
        network/HTTP failures so the tool layer can surface them.
        """
        params = {"~name": name_pattern, "~size": str(max_results)}
        try:
            resp = self.session.get(self.channels_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data: object = resp.json()
        except requests.exceptions.ConnectionError as exc:
            raise ChannelFinderConnectionError(
                f"Failed to connect to ChannelFinder at {self.base_url}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ChannelFinderResponseError(
                f"ChannelFinder query for '{name_pattern}' failed: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise ChannelFinderResponseError(
                f"ChannelFinder returned a non-list payload for '{name_pattern}'"
            )
        return [self._project(channel) for channel in data if isinstance(channel, dict)]

    @staticmethod
    def _project(channel: dict[str, object]) -> ChannelInfo:
        """Project a raw channel JSON into a :class:`ChannelInfo`.

        ChannelFinder serializes ``properties`` as a list of ``{name, value, owner}``
        objects (not a flat dict), so the IOC/host live in properties named ``iocName``/
        ``hostName`` (RecSync convention). Deterministic: tags sorted.
        """
        raw_props = channel.get("properties")
        props: dict[str, str] = {}
        if isinstance(raw_props, list):
            for prop in raw_props:
                if isinstance(prop, dict) and "name" in prop:
                    props[str(prop["name"])] = str(prop.get("value", ""))
        raw_tags = channel.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            tags.extend(
                str(tag["name"]) for tag in raw_tags if isinstance(tag, dict) and "name" in tag
            )
        return ChannelInfo(
            name=str(channel.get("name", "")),
            owner=str(channel.get("owner", "")),
            ioc_name=props.get("iocName"),
            host_name=props.get("hostName"),
            properties=props,
            tags=tuple(sorted(tags)),
        )
