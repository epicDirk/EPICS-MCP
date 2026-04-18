"""Configuration for the EPICS PV MCP Server, loaded from environment variables."""

from pydantic_settings import BaseSettings


class EpicsConfig(BaseSettings):
    """All settings are read from EPICS_MCP_* environment variables."""

    model_config = {"env_prefix": "EPICS_MCP_"}

    # --- Safety ---
    allow_pv_write: bool = False
    pv_write_pattern: str = ""  # regex allowlist (empty = deny all writes)
    write_rate_limit: int = 10  # max writes per minute
    audit_log_file: str = ""  # path to audit log (empty = stderr)

    # --- p4p ---
    provider: str = "pva"  # "pva" or "ca"
    default_timeout: float = 5.0
    max_batch_size: int = 100
    max_monitor_duration: float = 60.0
    max_monitor_events: int = 1000


_config: EpicsConfig | None = None


def get_config() -> EpicsConfig:
    """Return the singleton config, creating it on first call."""
    global _config
    if _config is None:
        _config = EpicsConfig()
    return _config
