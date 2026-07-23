"""Configuration for the EPICS PV MCP Server, loaded from environment variables."""

import os
import threading
import warnings
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class UnknownEpicsEnvVarWarning(UserWarning):
    """An ``EPICS_MCP_*`` environment variable matched no ``EpicsConfig`` field and was ignored.

    pydantic-settings' default ``extra="ignore"`` DROPS an unknown ``EPICS_MCP_FOO`` without a
    trace, so a typo (e.g. ``EPICS_MCP_CHANNELFINDR_URL`` for ``…CHANNELFINDER_URL``) silently
    leaves the real setting at its default. This warning surfaces the likely typo. It subclasses
    ``UserWarning`` so a deployment can filter or escalate it.
    """


# ``EPICS_MCP_*`` names deliberately OUTSIDE the config schema: read by the TEST harness / live gate
# (tests/live_gate.py + the *_live.py modules), never by the server. Their stripped, lowercased
# remainders start with one of these prefixes, so the unknown-var guard below does not
# false-positive on a legitimate opt-in live run that sets them.
_RESERVED_ENV_REMAINDER_PREFIXES = ("live_", "olog_test_", "require_live")


class EpicsConfig(BaseSettings):
    """All settings are read from EPICS_MCP_* environment variables.

    Numeric fields carry ``Field`` range constraints and ``provider`` is a
    ``Literal`` — a nonsensical or out-of-range env value is rejected with a
    clear ``ValidationError`` at first ``get_config()`` (fail-fast) instead of
    being silently accepted and producing hidden timeouts or a crashing rate
    limiter (a negative ``write_rate_limit`` used to abort ``SafetyLayer`` via
    ``deque(maxlen=-1)``).
    """

    model_config = {"env_prefix": "EPICS_MCP_"}

    # --- Safety ---
    allow_pv_write: bool = False
    # Regex-Allowlist für Schreib-PVs. Bei aktivem allow_pv_write ist ein nicht-leeres Pattern
    # PFLICHT: ein leeres Pattern mit writes-on wird beim Start als SafetyConfigError abgelehnt
    # (fail-closed, siehe SafetyLayer.__init__) — nicht mehr ein stiller allow-all. Wer wirklich
    # jeden PV schreibbar will, sagt das explizit (z. B. '.*'). Default leer ist sicher, weil
    # allow_pv_write per Default False ist.
    pv_write_pattern: str = ""
    # max writes per minute; ge=1 — "block all" is the allow_pv_write gate, not 0.
    write_rate_limit: int = Field(default=10, ge=1)
    # S3 read throttle (sliding 60 s window): max REST reads before the shared GET chokepoint
    # (rest_get_json/rest_get_bytes) raises ReadRateLimitError (READ_RATE_LIMIT_EXCEEDED — its own
    # code, never the write gates' RATE_LIMIT_EXCEEDED, because this refusal is un-audited and is
    # not a gate verdict) — protects a facility from an unthrottled read burst.
    # ge=0 and default 0 = DISABLED (unlike the write limits' ge=1): the posture is
    # opt-in, so existing read behaviour is unchanged until an operator sets it. Over the limit it
    # RAISES, never blocks — a blocking wait at that sync chokepoint would hold one of the shared
    # worker threads and reintroduce the very starvation the K4 monitor bulkhead removes.
    read_rate_limit: int = Field(default=0, ge=0)
    # Path to the write audit log; empty = stderr (ephemeral, lost on restart). REQUIRED once a
    # write gate is on: a write-enabled server (ALLOW_PV_WRITE / ALLOW_OLOG_WRITE) refuses to start
    # with an empty path (boot check in server.main()), so the ATTEMPT/ALLOW/DENY/READBACK/
    # BOUNDS_DENY trail survives a restart — the record that surfaces a wrong write later.
    audit_log_file: str = ""
    # O3 readback verification (always-on): after a sanctioned write the value is read back and
    # compared against what was written. This is the FALLBACK tolerance — it feeds BOTH the relative
    # and absolute axis of math.isclose — used only when the record carries no usable min_step
    # (control.min_step > 0 is preferred: the IOC's own drive resolution). Combining rel+abs keeps
    # the compare magnitude-safe AND value≈0-safe. There is deliberately NO on/off switch: readback
    # is always-on so a silent wrong-write cannot hide (a switch would reopen that hole).
    readback_tolerance: float = Field(default=1e-6, ge=0)

    # --- Path boundary (opt-in; see paths.resolve_user_path) ---
    # os.pathsep-separated roots that file/dir tool arguments must resolve under.
    # Empty (default) = NO boundary: future-posture optionality, not "secured".
    # It stays dormant because the CALLER is trusted — not because the server is
    # "read-only and localhost-isolated"; neither half is unconditional (there is
    # a gated write surface, and reach is the launcher's decision). See paths.py.
    allowed_roots: str = ""

    # --- p4p ---
    provider: Literal["pva", "ca"] = "pva"  # p4p provider; lowercase only
    default_timeout: float = Field(default=5.0, gt=0)
    max_batch_size: int = Field(default=100, ge=1)
    max_monitor_duration: float = Field(default=60.0, gt=0)
    max_monitor_events: int = Field(default=1000, ge=1)
    # K4 bulkhead: pv_monitor blocks a worker thread up to max_monitor_duration (60 s). Monitors run
    # on a DEDICATED ThreadPoolExecutor of this width (services/_concurrency.py), NOT the shared
    # asyncio default pool (min(32, cpu+4)) — so >= this many concurrent monitors can no longer
    # starve every other to_thread call (REST plane checks, PV reads/writes) into an apparent hang.
    # ge=1: a 0-width pool could never run a monitor. Default 8 is well below the default pool.
    monitor_max_concurrency: int = Field(default=8, ge=1)
    # Live-probe timeout for the diagnose_connection tool (fail-fast; a disconnected PV should not
    # hang the diagnosis). Separate from default_timeout so read latency and diagnosis can differ.
    diagnose_timeout: float = Field(default=5.0, gt=0)

    # --- Optional REST services (read-only; empty URL = disabled, no network call) ---
    # TLS trust for the HTTPS REST planes (ChannelFinder/Archiver/Alarm/Naming). ``ca_bundle`` is a
    # path to a CA-bundle PEM — set it when the REST hosts use a certificate signed by an internal
    # root CA that is NOT in certifi (the default trust store), which otherwise fails with
    # "self-signed certificate in chain". It is applied to EVERY REST session at the single
    # ``build_retrying_session`` chokepoint. When planes present DIFFERENT trust roots (one
    # internal CA, another public), a single-root bundle fails one: combine the internal CA
    # PEM WITH the public roots (certifi's cacert.pem) into ONE PEM — see epics-pv://guide.
    # ``tls_verify=False`` disables verification entirely —
    # an escape hatch for an internal network only, NOT the default. Precedence: ``ca_bundle``
    # (path) > ``tls_verify=False`` > default (certifi). When either is set explicitly the session
    # also pins ``trust_env=False`` so a ``REQUESTS_CA_BUNDLE`` env var cannot silently
    # override it (requests would otherwise let the env value win); on the plain default
    # ``trust_env`` stays on, so the zero-code ``REQUESTS_CA_BUNDLE`` path keeps working.
    ca_bundle: str = ""
    tls_verify: bool = True
    # ChannelFinder service root incl. context path, e.g. "http://host:8080/ChannelFinder".
    channelfinder_url: str = ""
    channelfinder_auth: str = ""  # optional Authorization header value for secured deployments
    # Cap on channels returned per CF prefix query; raise it for a large device prefix (the full
    # mTCA-EVR-300 register set). The CF checker withholds its verdict once a query hits this cap.
    channelfinder_max_results: int = Field(default=500, ge=1)
    # DS-PRIVACY (site-configurable): the ChannelFinder ``owner`` / ``properties`` allowlists decide
    # which values are surfaced vs. redacted (see channelfinder_client._project). Default = the ESS
    # RecSync convention (owner ``recceiver``; properties iocName/hostName/iocid/pvStatus/time).
    # Three-way via ``str | None``: UNSET (None) = built-in ESS default; a comma-separated list =
    # OVERRIDE (a facility's own service accounts / technical property names); an explicitly EMPTY
    # string = redact EVERYTHING (empty allowlist). ``str | None`` lets a SET-but-empty env mean
    # "redact all" as distinct from an UNSET "use the default".
    channelfinder_safe_owner_accounts: str | None = None
    channelfinder_safe_property_names: str | None = None
    # Archiver Appliance MGMT root, e.g. "http://archiver:17665" — serves /mgmt/bpl (is_archived).
    archiver_url: str = ""
    # Archiver Appliance RETRIEVAL root, e.g. "http://archiver:17668" — serves /retrieval/data
    # (get_pv_history). In a single-JVM appliance both webapps share one port, so this may be left
    # empty and get_pv_history falls back to archiver_url. In a split deployment mgmt (:17665) and
    # retrieval (:17668) are SEPARATE Tomcats, so this must point at the retrieval one. (A
    # retrieval-cluster-aware appliance cluster proxies internally — one URL covers all members;
    # see the epics-pv://guide resource.)
    archiver_retrieval_url: str = ""
    archiver_auth: str = ""  # optional Authorization header value for secured deployments
    # Phoebus Alarm Logger REST root, e.g. "http://localhost:8081". Activates is_alarm_configured.
    alarm_url: str = ""
    alarm_auth: str = ""  # optional Authorization header value for secured deployments
    # ESS Naming Service base URL (no built-in default host). Empty (default) = disabled = withheld:
    # the naming plane stays off and makes NO ESS call unless set (no egress by default). BOTH
    # diagnose_connection AND crossplane_check/CLI honour this gate — neither reaches ESS production
    # naming unless this is set.
    naming_url: str = ""
    # Phoebus Olog (electronic logbook) REST root incl. context path, e.g. "http://host:8080/Olog".
    # Empty (default) = disabled: the Olog plane makes NO network call and no ESS egress. Read-only.
    # Output posture: DS-PRIVACY-redacted (author dropped, free text withheld) unless BOTH this url
    # is loopback AND olog_assume_test_data is set — see below. `epics-doctor` prints the effective
    # posture.
    olog_url: str = ""
    olog_auth: str = ""  # optional Authorization header value for secured deployments
    # The operator's EXPLICIT declaration: "the Olog at olog_url holds synthetic test data, so its
    # entries may leave whole". Default false = redact, always.
    #
    # Why a flag AND the loopback url, when the url alone looks like it should do: a loopback
    # ADDRESS does not prove the DATA is synthetic. `ssh -L 8080:olog-prod:8080` or a
    # port-forward make a production logbook answer on localhost:8080 — the url never changes,
    # so binding to it alone would silently un-redact production (demonstrated live, QA
    # 2026-07-15). No url inspection can see through a tunnel. Only a person can assert what
    # the data IS, and this flag is where they do it: loopback stays a NECESSARY condition (it
    # still catches "pointed at the facility and forgot"), and the flag adds the sufficient one.
    olog_assume_test_data: bool = False

    # --- Olog WRITE gate (separate from ALLOW_PV_WRITE; that stays false + untouched) ---
    # Olog write is a deliberately-authorized, SEPARATE logbook surface behind its OWN gate. Unlike
    # PV write (implicitly test-safe via the EPICS address-list localhost isolation), Olog speaks
    # HTTP to an arbitrary URL — so the gate adds a TEST-SERVER URL BOUNDARY: a write is refused
    # unless olog_url resolves to a loopback host (the local Docker sandbox) OR is an allowlisted
    # https URL with allow_remote set (a plain-http remote is refused — creds are cleartext). This
    # prevents an accidental write to a production Olog.
    allow_olog_write: bool = False  # primary on/off gate (default false = every Olog write denied)
    olog_write_user: str = ""  # Basic-auth service account (never a personal login — becomes owner)
    olog_write_password: str = ""  # Basic-auth password for the write service account
    # Comma-separated logbook names a write may target (NOT a regex — names are discrete). EMPTY +
    # gate on = deny-all (fail-closed). (The PV write pattern is also fail-closed but differently:
    # an empty pattern with writes on is refused at startup, see pv_write_pattern above.)
    olog_write_logbooks: str = ""
    # Max Olog writes per 60 s window; ge=1 — the on/off control is allow_olog_write, not 0. Low by
    # default (a logbook is human-paced).
    olog_write_rate_limit: int = Field(default=5, ge=1)
    # Comma-separated EXACT base URLs (== olog_url) allowed as non-loopback write targets; each must
    # be https (a plain-http remote is refused). Only takes effect with olog_write_allow_remote — a
    # production write is a deliberate double action.
    olog_write_url_allowlist: str = ""
    # Permit writes to a non-loopback (allowlisted) https URL. Default false: only loopback is
    # writable. The write session is env-independent (no proxy / REQUESTS_CA_BUNDLE env), so a
    # remote's CA must come from ca_bundle (EPICS_MCP_CA_BUNDLE).
    olog_write_allow_remote: bool = False

    # --- Olog ATTACHMENT surface (OA1) ---
    # A downloaded attachment's raw BYTES and its FILENAME are author-written free text (a person
    # can
    # be named in either) and BYPASS the dict-based redact_record barrier — so they need their own
    # gate at the byte boundary. Raw bytes leave ONLY when the read posture is already whole-mode
    # (loopback url AND olog_assume_test_data, i.e. OlogClient._redact is False) AND this flag is
    # explicitly set. A SECOND, deliberate opt-in on top of the whole-mode signal (defense-in-depth,
    # like the write gate): the by-id endpoint /Olog/attachment/{id} has no server-side per-log
    # authorization, so un-redacted byte egress stays an intentional, auditable choice. Default
    # false
    # = never emit raw attachment bytes. Filenames follow the whole-mode boundary alone (they are a
    # lesser exposure and already visible in a whole-mode entry read); this flag gates only the
    # bytes.
    olog_allow_attachment_download: bool = False
    # Client-side anti-DoS cap on attachment bytes, both directions. On UPLOAD it caps the TOTAL
    # size,
    # checked in the Olog write gate BEFORE the files are read (a stat-sum) AND re-checked while
    # reading (at most one byte over budget is ever read — a file that grew between stat and read
    # is refused, QA/TOCTOU). On DOWNLOAD it caps the body (a Content-Length over it is refused
    # before
    # any
    # read; the stream is accumulated only up to the cap), so a huge attachment never OOMs the
    # process
    # — a base64 download is capped further still (response tokens). ge=1; default 50 MiB. The Olog
    # SERVER enforces its own upload limit (HTTP 413, profile-dependent — 50/100 MB under the docker
    # profile); this is the MCP's own fail-fast, not a mirror of the server value (reading GET /Olog
    # serverConfig is deferred, OA10).
    olog_attach_max_bytes: int = Field(default=52_428_800, ge=1)

    @model_validator(mode="after")
    def _warn_on_unknown_env_vars(self) -> Self:
        """Warn (never fail) for an ``EPICS_MCP_*`` env var that maps to no config field.

        ``env_prefix`` + the default ``extra="ignore"`` means an unknown var is dropped silently,
        so a typo would never surface. This scans the process environment once per construction and
        warns for each ``EPICS_MCP_`` key whose stripped, lowercased remainder is neither a declared
        field nor a reserved test-harness name. A warning, not a ``ValidationError`` — an unknown
        var is a likely mistake but not provably fatal (it may belong to a different tool sharing
        the process).
        """
        prefix = "EPICS_MCP_"
        known_fields = set(type(self).model_fields)
        for name in os.environ:
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :].lower()
            if remainder in known_fields or remainder.startswith(_RESERVED_ENV_REMAINDER_PREFIXES):
                continue
            warnings.warn(
                f"Environment variable {name!r} matches no EpicsConfig setting and was ignored — "
                "check for a typo (the config prefix is EPICS_MCP_).",
                UnknownEpicsEnvVarWarning,
                stacklevel=2,
            )
        return self


_config: EpicsConfig | None = None
_config_lock = threading.Lock()


def get_config() -> EpicsConfig:
    """Return the singleton config, creating it on first call (thread-safe).

    Der Lock verhindert eine Doppel-Initialisierung bei gleichzeitigem
    Erst-Zugriff aus mehreren Threads (analog zum bereits gelockten
    ``get_context()`` des p4p-Clients).
    """
    global _config
    with _config_lock:
        if _config is None:
            _config = EpicsConfig()
    return _config
