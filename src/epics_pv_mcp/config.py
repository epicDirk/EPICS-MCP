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
    # Regex-Allowlist für Schreib-PVs. Leer = KEIN zusätzlicher Filter: bei
    # aktivem allow_pv_write sind dann alle PVs schreibbar (das env-Gate ist die
    # primäre Kontrolle, das Pattern eine optionale Verschärfung).
    pv_write_pattern: str = ""
    # max writes per minute; ge=1 — "block all" is the allow_pv_write gate, not 0.
    write_rate_limit: int = Field(default=10, ge=1)
    audit_log_file: str = ""  # path to audit log (empty = stderr)

    # --- Path boundary (opt-in; see paths.resolve_user_path) ---
    # os.pathsep-separated roots that file/dir tool arguments must resolve under.
    # Empty (default) = NO boundary (future-posture optionality, not "secured" —
    # the server is read-only + localhost-isolated with a single trusted caller).
    allowed_roots: str = ""

    # --- p4p ---
    provider: Literal["pva", "ca"] = "pva"  # p4p provider; lowercase only
    default_timeout: float = Field(default=5.0, gt=0)
    max_batch_size: int = Field(default=100, ge=1)
    max_monitor_duration: float = Field(default=60.0, gt=0)
    max_monitor_events: int = Field(default=1000, ge=1)

    # --- Optional REST services (read-only; empty URL = disabled, no network call) ---
    # ChannelFinder service root incl. context path, e.g. "http://host:8080/ChannelFinder".
    channelfinder_url: str = ""
    channelfinder_auth: str = ""  # optional Authorization header value for secured deployments
    # Cap on channels returned per CF prefix query; raise it for a large device prefix (the full
    # mTCA-EVR-300 register set). The CF checker withholds its verdict once a query hits this cap.
    channelfinder_max_results: int = Field(default=500, ge=1)
    # Archiver Appliance MGMT root, e.g. "http://archiver:17665" — serves /mgmt/bpl (is_archived).
    archiver_url: str = ""
    # Archiver Appliance RETRIEVAL root, e.g. "http://archiver:17668" — serves /retrieval/data
    # (get_pv_history). In a single-JVM appliance both webapps share one port, so this may be left
    # empty and get_pv_history falls back to archiver_url. In the ESS 4-instance topology mgmt
    # (:17665) and retrieval (:17668) are SEPARATE Tomcats, so this must point at the retrieval one.
    archiver_retrieval_url: str = ""
    archiver_auth: str = ""  # optional Authorization header value for secured deployments
    # Phoebus Alarm Logger REST root, e.g. "http://localhost:8081". Activates is_alarm_configured.
    alarm_url: str = ""
    alarm_auth: str = ""  # optional Authorization header value for secured deployments


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
