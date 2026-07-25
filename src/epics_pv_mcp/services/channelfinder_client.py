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


def _named_list(data: object, endpoint: str) -> list[str]:
    """STRICT name extraction for the top-level ``/resources/properties`` / ``/resources/tags``
    listings (S11 — the ChannelFinder sibling of :func:`olog_client._named_list`).

    Both routes return a list of ``{name, owner, …}`` structs (``PropertyDto``/``TagDto``). The
    listing IS the answer to "what can I filter on", so an unreadable payload must never collapse to
    ``[]`` — that would fabricate "there are no properties/tags", indistinguishable from a genuinely
    empty server, and tell anyone validating a filter name "this one does not exist". A non-list, or
    an item that is not a dict with a string ``name``, RAISES; an EMPTY list is valid (returns
    ``[]``). Only ``name`` is read — the DS-privacy ``owner`` (a person's ESS username on a
    CF-web-UI/cfstore object) and ``value`` are dropped.
    """
    if not isinstance(data, list):
        raise ChannelFinderResponseError(
            f"ChannelFinder {endpoint} returned an unreadable payload "
            f"(expected a list, got {type(data).__name__}); the listing is not readable."
        )
    names: list[str] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ChannelFinderResponseError(
                f"ChannelFinder {endpoint} returned an unreadable item "
                f"(expected a dict with a string 'name', got {type(item).__name__})."
            )
        names.append(item["name"])
    return names


# MA-2 CF-Query-Fläche. The reserved ChannelFinder query params (the ``switch`` cases in the vendor
# ``ChannelRepository.getBuiltQuery``). A caller-supplied property/tag NAME must never collide with
# one of these, and — critically — a trailing ``!`` (the vendor's negation marker on the KEY) must
# never be synthesised on ``~name``: the server strips the ``!`` and filters ``~name`` POSITIVELY,
# so ``~name!`` is a silent broaden, not a negation.
_RESERVED_QUERY_KEYS = frozenset(
    {"~name", "~tag", "~size", "~from", "~search_after", "~track_total_hits"}
)
# ChannelFinder splits a value on any of these into an OR (``valueSplitPattern`` ``[|,;]``).
_VALUE_SEPARATORS = ("|", ",", ";")


def _validate_filter_name(name: str, *, kind: str) -> str:
    """Return the trimmed property/tag name, or raise ``ValueError``.

    Rejects a blank name, a leading ``~`` (reserved-param collision) and a trailing ``!`` (the
    vendor negation marker) — the three ways a caller string could smuggle in a reserved or negated
    key such as the forbidden ``~name!``. Run on EVERY property-name-accepting surface BEFORE a
    ``!`` is appended, so no code path can emit ``~name!``.
    """
    clean = name.strip()
    if not clean:
        raise ValueError(f"{kind} name must not be blank")
    if clean.startswith("~"):
        raise ValueError(f"{kind} name {name!r} must not start with '~' (reserved query param)")
    if clean.endswith("!"):
        raise ValueError(f"{kind} name {name!r} must not end with '!' (reserved for negation)")
    return clean


def _build_query_params(
    name_pattern: str,
    max_results: int,
    *,
    has_properties: dict[str, str] | None = None,
    lacks_properties: list[str] | None = None,
    not_property_values: dict[str, str] | None = None,
    has_tags: list[str] | None = None,
    lacks_tags: list[str] | None = None,
    allowed_properties: frozenset[str],
    include_size: bool = True,
) -> dict[str, str]:
    """Build the ChannelFinder GET query params from a name glob + optional filters.

    Pure and deterministic (no I/O) so it is unit-testable in isolation, and shared by
    :meth:`ChannelFinderClient.find_channels` and ``count_channels`` so both filter identically.

    Vendor grammar (``ChannelRepository.getBuiltQuery``, verified): a non-``~`` key is a property
    filter; negation is a trailing ``!`` on the KEY (``prop!``); ``prop!=*`` (value literally ``*``)
    means "lacks the property"; ``prop!=value`` means "has the property, value != value" (a channel
    lacking it does NOT match); tag values are OR / any-of; distinct property keys are AND. Unknown
    params are NOT silently ignored (unlike Olog/Alarm) — the server treats any non-``~`` key as a
    property filter, so a typo narrows the result to ~0 rather than being a no-op.

    DS-PRIVACY: the property-filter axis is GATED to *allowed_properties* — the SAME allowlist the
    response projection uses (:meth:`ChannelFinderClient._project`). Filtering on a redacted
    property (e.g. ``accessGroup``) would reconstruct the exact name->value partition the projection
    hides, so a non-allowlisted property name is refused (an empty allowlist disables property
    filtering entirely). Tags are not redacted and are not gated.
    """
    params: dict[str, str] = {"~name": name_pattern}
    if include_size:
        params["~size"] = str(max_results)

    # A property name may appear in at most one of the three property args (contradictory otherwise;
    # this subsumes the ``prop!`` key collision between lacks_properties and not_property_values).
    claimed: dict[str, str] = {}

    def _claim_property(name: str, arg: str) -> str:
        clean = _validate_filter_name(name, kind="property")
        if clean not in allowed_properties:
            raise ValueError(
                f"property {name!r} is not on the ChannelFinder safe-property allowlist "
                "(filtering is limited to surfaced technical properties — DS-privacy)"
            )
        if clean in claimed:
            raise ValueError(
                f"property {name!r} is contradictory: it appears in both "
                f"{claimed[clean]!r} and {arg!r}"
            )
        claimed[clean] = arg
        return clean

    for name, value in (has_properties or {}).items():
        clean = _claim_property(name, "has_properties")
        if not value.strip():
            raise ValueError(f"has_properties[{name!r}] value must not be blank")
        params[clean] = value  # value '*' => present; a |,; separator is the intended OR

    for name in lacks_properties or []:
        clean = _claim_property(name, "lacks_properties")
        params[f"{clean}!"] = "*"  # prop!=* => lacks the property (value MUST be '*')

    for name, value in (not_property_values or {}).items():
        clean = _claim_property(name, "not_property_values")
        if not value.strip():
            raise ValueError(f"not_property_values[{name!r}] value must not be blank")
        if any(sep in value for sep in _VALUE_SEPARATORS):
            raise ValueError(
                f"not_property_values[{name!r}] must not contain a value separator (| , ;) — it "
                "would flip the single negation into an OR-of-negations tautology"
            )
        params[f"{clean}!"] = value  # prop!=value => has prop whose value != value

    clean_has_tags = [_validate_filter_name(t, kind="tag") for t in (has_tags or [])]
    clean_lacks_tags = [_validate_filter_name(t, kind="tag") for t in (lacks_tags or [])]
    overlap = sorted(set(clean_has_tags) & set(clean_lacks_tags))
    if overlap:
        raise ValueError(f"tag(s) {overlap} are contradictory: in both has_tags and lacks_tags")
    if clean_has_tags:
        params["~tag"] = "|".join(clean_has_tags)  # OR / any-of (vendor: split on [|,;])
    if clean_lacks_tags:
        params["~tag!"] = "|".join(clean_lacks_tags)

    return params


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

    @property
    def count_url(self) -> str:
        # MA-2: ChannelFinder's dedicated count endpoint (IChannel.java @GetMapping("/count")),
        # returning an EXACT match count as a bare JSON number, independent of ~size.
        return f"{self.base_url}/resources/channels/count"

    @property
    def properties_url(self) -> str:
        # MA-2 CF-Query-Fläche: the vendor property-definition list route
        # (PropertyController.list → GET {root}/resources/properties). The list endpoint returns
        # every PropertyDto with an empty ``channels`` (no join) — only ``name`` is meaningful here.
        return f"{self.base_url}/resources/properties"

    @property
    def tags_url(self) -> str:
        # MA-2 CF-Query-Fläche: the vendor tag-definition list route
        # (TagController.list → GET {root}/resources/tags).
        return f"{self.base_url}/resources/tags"

    def find_channels(
        self,
        name_pattern: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        *,
        has_properties: dict[str, str] | None = None,
        lacks_properties: list[str] | None = None,
        not_property_values: dict[str, str] | None = None,
        has_tags: list[str] | None = None,
        lacks_tags: list[str] | None = None,
    ) -> list[ChannelInfo]:
        """Query channels by name glob (``*``/``?``) + optional property/tag filters, capped at
        *max_results*.

        The optional filters (MA-2) narrow the search server-side; property filtering is gated to
        the same DS-privacy allowlist as the projection (see :func:`_build_query_params`). Returns
        the projected channels (possibly empty). Raises
        :class:`ChannelFinderConnectionError`/:class:`ChannelFinderResponseError` on network/HTTP
        failures, and :class:`ValueError` on an invalid/redacted filter (the tool layer maps it to
        an ``INVALID_INPUT`` error).
        """
        params = _build_query_params(
            name_pattern,
            max_results,
            has_properties=has_properties,
            lacks_properties=lacks_properties,
            not_property_values=not_property_values,
            has_tags=has_tags,
            lacks_tags=lacks_tags,
            allowed_properties=self._safe_property_names,
        )
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

    def count_channels(
        self,
        name_pattern: str,
        *,
        has_properties: dict[str, str] | None = None,
        lacks_properties: list[str] | None = None,
        not_property_values: dict[str, str] | None = None,
        has_tags: list[str] | None = None,
        lacks_tags: list[str] | None = None,
    ) -> int:
        """Return the EXACT number of channels matching the name glob + filters (MA-2).

        Uses ChannelFinder's ``/resources/channels/count`` endpoint, which returns a true full-match
        count as a bare JSON number — independent of ``~size`` and never window-capped — so it
        answers "how many PVs match" without pulling the matches. Same filter grammar and the same
        DS-privacy allowlist gate as :meth:`find_channels`. Raises
        :class:`ChannelFinderResponseError` if the payload is not a numeric scalar (a JSON boolean
        is rejected explicitly — ``bool`` is an ``int`` subclass).
        """
        params = _build_query_params(
            name_pattern,
            0,
            has_properties=has_properties,
            lacks_properties=lacks_properties,
            not_property_values=not_property_values,
            has_tags=has_tags,
            lacks_tags=lacks_tags,
            allowed_properties=self._safe_property_names,
            include_size=False,
        )
        data = rest_get_json(
            self.session,
            self.count_url,
            params,
            self.timeout,
            conn_exc=ChannelFinderConnectionError,
            resp_exc=ChannelFinderResponseError,
        )
        # bool ⊂ int → guard it first, or a stray JSON `true` would parse as the count 1.
        if isinstance(data, bool) or not isinstance(data, (int, str)):
            raise ChannelFinderResponseError(
                f"ChannelFinder /count returned a non-numeric payload (got {type(data).__name__})"
            )
        try:
            return int(data)
        except ValueError as exc:
            raise ChannelFinderResponseError(
                f"ChannelFinder /count returned a non-numeric string: {data!r}"
            ) from exc

    def list_properties(self) -> list[str]:
        """The ChannelFinder property NAMES a caller can filter ``find_channels`` on, sorted.

        Fetches ``/resources/properties`` and reduces it to the DS-privacy
        ``self._safe_property_names`` allowlist — the SAME gate ``_project`` applies to per-channel
        properties and ``_build_query_params`` enforces on ``has_properties``. So this lists exactly
        the property keys that are both present in this instance AND accepted as a filter; a
        non-allowlisted, person-bearing property (ENGINEER/LOCATION, a cfstore custom field) is
        never surfaced and would be refused as a filter anyway. Raises
        :class:`ChannelFinderResponseError` on an unreadable payload (never ``[]``); an empty CF
        yields ``[]``.
        """
        data = rest_get_json(
            self.session,
            self.properties_url,
            {},
            self.timeout,
            conn_exc=ChannelFinderConnectionError,
            resp_exc=ChannelFinderResponseError,
        )
        names = _named_list(data, f"GET {self.properties_url}")
        return sorted(name for name in names if name in self._safe_property_names)

    def list_tags(self) -> list[str]:
        """The ChannelFinder tag NAMES a caller can filter ``find_channels`` on, sorted.

        Fetches ``/resources/tags``. Tags are UNGATED (as in ``_project`` and
        ``_build_query_params``, which validate but do not allowlist tag names), so every tag name
        is returned — the per-tag ``owner`` is dropped (name-only). Raises
        :class:`ChannelFinderResponseError` on an unreadable payload (never ``[]``); an empty CF
        yields ``[]``.
        """
        data = rest_get_json(
            self.session,
            self.tags_url,
            {},
            self.timeout,
            conn_exc=ChannelFinderConnectionError,
            resp_exc=ChannelFinderResponseError,
        )
        return sorted(_named_list(data, f"GET {self.tags_url}"))

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
