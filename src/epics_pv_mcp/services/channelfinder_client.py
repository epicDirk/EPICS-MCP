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

from epics_pv_mcp.config import EpicsConfig, get_config
from epics_pv_mcp.services._http import get_shared_session, rest_get_json
from epics_pv_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderResponseError,
)
from epics_pv_mcp.services.redact import project_allowlist

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

# DS-PRIVACY: the surfaced ``properties`` were formerly an ALLOW-BY-DEFAULT denylist (only
# ``recceiverID`` popped) — any OTHER property VALUE passed through verbatim. The reccaster
# ENGINEER/LOCATION env-var convention (devIocStats) and CF-web-UI/cfstore custom properties carry a
# person's name in the value. We instead ALLOWLIST the known-technical RecSync property names via
# :func:`~epics_pv_mcp.services.redact.project_allowlist`, so an unknown/new person-bearing property
# is dropped by default (the codebase-wide allowlist principle). ``iocName``/``hostName`` also feed
# the dedicated ``ioc_name``/``host_name`` fields. If a live ESS smoke shows a useful technical
# property missing here, ADD it to this set — do not fall back to a denylist.
_SAFE_PROPERTY_NAMES = frozenset({"iocName", "hostName", "iocid", "pvStatus", "time"})


def _resolve_allowlist(value: str | None, default: frozenset[str]) -> frozenset[str]:
    """Resolve a site-configurable allowlist string to a frozenset.

    Three-way (see :class:`~epics_pv_mcp.config.EpicsConfig`): ``None`` (unset) → the built-in
    *default*; an explicitly EMPTY string → an EMPTY allowlist (redact everything); a comma-
    separated list → those names (trimmed, empties dropped). This is the SINGLE resolution both
    the CF client and the doctor privacy report use, so doctor's report and the client's redaction
    cannot drift.
    """
    if value is None:
        return default
    return frozenset(token.strip() for token in value.split(",") if token.strip())


def resolve_safe_owner_accounts(cfg: EpicsConfig) -> frozenset[str]:
    """Effective ChannelFinder owner allowlist for *cfg* (site override or the ESS default)."""
    return _resolve_allowlist(cfg.channelfinder_safe_owner_accounts, _SAFE_OWNER_ACCOUNTS)


def resolve_safe_property_names(cfg: EpicsConfig) -> frozenset[str]:
    """Effective ChannelFinder property allowlist for *cfg* (site override or the ESS default)."""
    return _resolve_allowlist(cfg.channelfinder_safe_property_names, _SAFE_PROPERTY_NAMES)


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
        self.session = get_shared_session(auth_header=auth_header)
        # DS-PRIVACY: resolve the (site-configurable) allowlists ONCE at construction from config,
        # falling back to the ESS defaults when unset. ``_project`` reads these instance fields —
        # a facility can set its own service accounts / technical properties (or redact everything).
        cfg = get_config()
        self._safe_owner_accounts = resolve_safe_owner_accounts(cfg)
        self._safe_property_names = resolve_safe_property_names(cfg)

    def check_connectivity(self) -> bool:
        """Return True if ChannelFinder is reachable; raise ChannelFinderConnectionError otherwise.

        A HEAD to the service root proves transport + TLS (the CA bundle) — any HTTP response counts
        as reachable (the status is irrelevant here, as in the Naming client). A transport/TLS
        failure is re-raised as ChannelFinderConnectionError with the original requests error as
        ``__cause__``, so a caller (doctor) can inspect it (:func:`is_ssl_error`) to tell a CA
        problem from a plain unreachable host.
        """
        try:
            self.session.head(self.base_url, timeout=self.timeout)
            return True
        except OSError as exc:
            # requests.exceptions.RequestException ⊂ OSError (see naming_client.check_connectivity),
            # so this arm catches Timeout/ConnectionError/SSLError; re-raise as the service error.
            raise ChannelFinderConnectionError(
                f"Failed to connect to ChannelFinder at {self.base_url}: {exc}"
            ) from exc

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
        # S11: a non-dict element must raise — it used to be silently dropped, shrinking the
        # registry answer without a trace (two different malformed payloads both "succeeded").
        channels: list[ChannelInfo] = []
        for channel in data:
            if not isinstance(channel, dict):
                raise ChannelFinderResponseError(
                    f"ChannelFinder returned a non-dict channel record "
                    f"(got {type(channel).__name__}) for '{name_pattern}'"
                )
            channels.append(self._project(channel))
        return channels

    def _project(self, channel: dict[str, object]) -> ChannelInfo:
        """Project a raw channel JSON into a :class:`ChannelInfo`.

        ChannelFinder serializes ``properties`` as a list of ``{name, value, owner}``
        objects (not a flat dict), so the IOC/host live in properties named ``iocName``/
        ``hostName`` (RecSync convention). Deterministic: tags sorted.

        DS-PRIVACY: ``owner`` is kept only when it is on the (site-configurable) owner allowlist
        ``self._safe_owner_accounts`` (ESS default :data:`_SAFE_OWNER_ACCOUNTS`), else redacted to
        ``""`` — a CF-web-UI/cfstore channel is owned by a person's ESS username. ``properties`` is
        reduced to the ``self._safe_property_names`` allowlist (ESS default
        :data:`_SAFE_PROPERTY_NAMES`), so a person-bearing property value (ENGINEER/LOCATION or a
        cfstore custom field) is dropped by default. The per-property ``owner`` is never read.
        IOC/host/tags — the technical provenance — are untouched.
        """
        raw_props = channel.get("properties")
        props: dict[str, str] = {}
        if isinstance(raw_props, list):
            # LENIENT by design and ONLY inside an already-anchored record (the record's
            # identity is guarded below; olog_client._names documents the same rationale):
            # one malformed property must not sink the whole channel. Lenient never means
            # FABRICATING though (QA): a null value used to be str()-minted into the
            # literal string "None" — flowing through the allowlist into host_name and
            # device_lookup's source_host — and a non-str name was stringified into an
            # invented key. Malformed entries are dropped whole instead.
            for prop in raw_props:
                if not isinstance(prop, dict):
                    continue
                prop_name = prop.get("name")
                prop_value = prop.get("value")
                if isinstance(prop_name, str) and prop_name and isinstance(prop_value, str):
                    props[prop_name] = prop_value
        # DS-PRIVACY: allowlist surfaced properties (deny-by-default; see _safe_property_names).
        # isinstance narrows the generic allowlist result back to str — values ARE str here
        # (built above), and narrowing never fabricates (unlike the former str() minting).
        allowlisted = project_allowlist(props, self._safe_property_names)
        props = {k: v for k, v in allowlisted.items() if isinstance(v, str)}
        raw_tags = channel.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                if not isinstance(tag, dict):
                    continue
                tag_name = tag.get("name")
                if isinstance(tag_name, str) and tag_name:
                    tags.append(tag_name)
        raw_owner = str(channel.get("owner", ""))
        owner = raw_owner if raw_owner in self._safe_owner_accounts else ""
        # S11: the identity field is required. A record without a usable name used to become
        # ChannelInfo(name="") — an identity-less phantom that entered downstream sets
        # (crossplane/coverage registered_under) as "". Measured (ESS CF): name is always there.
        raw_name = channel.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            raise ChannelFinderResponseError(
                "ChannelFinder returned a channel record without a usable 'name' "
                f"(got {type(raw_name).__name__}); the record has no identity."
            )
        return ChannelInfo(
            name=raw_name,
            owner=owner,
            ioc_name=props.get("iocName"),
            host_name=props.get("hostName"),
            properties=props,
            tags=tuple(sorted(tags)),
        )
