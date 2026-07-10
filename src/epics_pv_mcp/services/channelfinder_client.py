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

from typing import TypedDict

from epics_pv_mcp.services._http import build_retrying_session, rest_get_json
from epics_pv_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderResponseError,
)

# Default upper bound on returned channels — a broad glob (``*``) can match a whole site.
DEFAULT_MAX_RESULTS = 500

# DS-PRIVACY: ChannelFinder ``owner`` is the account that owns the channel. For RecSync-populated
# channels it is the ``recceiver`` SERVICE account (audit-observed) — not a person. But a channel
# created via the CF web UI / cfstore is owned by the logged-in ENGINEER'S ESS username — a personal
# name — and the two are indistinguishable from the value alone. So we keep ``owner`` ONLY when it
# is on this conservative service-account allowlist and redact any other value to ``""`` (unknown →
# redacted, the safe default). The Batch-3 redactor at the ``services/checkers`` chokepoint is the
# durable form; this is the Batch-1 interim guard.
_SAFE_OWNER_ACCOUNTS = frozenset({"recceiver"})


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
        self.session = build_retrying_session(auth_header=auth_header)

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
        data = rest_get_json(
            self.session,
            self.channels_url,
            params,
            self.timeout,
            conn_exc=ChannelFinderConnectionError,
            resp_exc=ChannelFinderResponseError,
        )
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

        DS-PRIVACY: ``owner`` is kept only when it is a known service account
        (:data:`_SAFE_OWNER_ACCOUNTS`) and redacted to ``""`` otherwise — a CF-web-UI/cfstore
        channel is owned by a person's ESS username. ``properties['recceiverID']`` (an opaque
        receiver-instance id) is dropped as belt-and-braces. IOC/host/tags — the technical
        provenance — are untouched.
        """
        raw_props = channel.get("properties")
        props: dict[str, str] = {}
        if isinstance(raw_props, list):
            for prop in raw_props:
                if isinstance(prop, dict) and "name" in prop:
                    props[str(prop["name"])] = str(prop.get("value", ""))
        props.pop("recceiverID", None)  # DS-PRIVACY: opaque receiver-instance id, not surfaced
        raw_tags = channel.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            tags.extend(
                str(tag["name"]) for tag in raw_tags if isinstance(tag, dict) and "name" in tag
            )
        raw_owner = str(channel.get("owner", ""))
        owner = raw_owner if raw_owner in _SAFE_OWNER_ACCOUNTS else ""
        return ChannelInfo(
            name=str(channel.get("name", "")),
            owner=owner,
            ioc_name=props.get("iocName"),
            host_name=props.get("hostName"),
            properties=props,
            tags=tuple(sorted(tags)),
        )
