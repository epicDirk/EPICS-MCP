"""Configuration for the EPICS PV MCP Server, loaded from environment variables."""

import threading

from pydantic_settings import BaseSettings


class EpicsConfig(BaseSettings):
    """All settings are read from EPICS_MCP_* environment variables."""

    model_config = {"env_prefix": "EPICS_MCP_"}

    # --- Safety ---
    allow_pv_write: bool = False
    # Regex-Allowlist für Schreib-PVs. Leer = KEIN zusätzlicher Filter: bei
    # aktivem allow_pv_write sind dann alle PVs schreibbar (das env-Gate ist die
    # primäre Kontrolle, das Pattern eine optionale Verschärfung).
    pv_write_pattern: str = ""
    write_rate_limit: int = 10  # max writes per minute
    audit_log_file: str = ""  # path to audit log (empty = stderr)

    # --- p4p ---
    provider: str = "pva"  # "pva" or "ca"
    default_timeout: float = 5.0
    max_batch_size: int = 100
    max_monitor_duration: float = 60.0
    max_monitor_events: int = 1000

    # --- Optional REST services (read-only; empty URL = disabled, no network call) ---
    # ChannelFinder service root incl. context path, e.g. "http://host:8080/ChannelFinder".
    channelfinder_url: str = ""
    channelfinder_auth: str = ""  # optional Authorization header value for secured deployments
    # Archiver Appliance root, e.g. "http://archiver:17665".
    archiver_url: str = ""
    archiver_auth: str = ""  # optional Authorization header value for secured deployments


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
