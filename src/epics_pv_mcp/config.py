"""Configuration for the EPICS PV MCP Server, loaded from environment variables."""

import threading
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


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
    audit_log_file: str = ""  # path to audit log (empty = stderr)

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
