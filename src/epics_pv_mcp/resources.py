"""MCP Resources for the EPICS PV MCP Server."""

import importlib.resources
import sys
import time
from functools import lru_cache

from epics_pv_mcp import __version__
from epics_pv_mcp.config import get_config

_start_time = time.monotonic()


@lru_cache(maxsize=1)
def get_guide() -> str:
    """The operational cookbook served as ``epics-pv://guide``.

    Reads the package-data file ``operator_guide.md`` (a sibling of ``py.typed`` inside the
    package, so hatchling ships it in the wheel and ``importlib.resources`` finds it in both an
    editable and an installed layout). Only invoked at resource-read time, so a missing file
    surfaces as a read-time error, never an import crash; ``lru_cache`` does not cache exceptions,
    so a genuinely absent file re-raises on each call.
    """
    return (
        importlib.resources.files("epics_pv_mcp")
        .joinpath("operator_guide.md")
        .read_text(encoding="utf-8")
    )


def get_health() -> dict[str, object]:
    """Server health status."""
    cfg = get_config()
    p4p_version = "unknown"
    try:
        import p4p

        p4p_version = p4p.__version__
    except (ImportError, AttributeError):
        pass

    return {
        "server": "epics-mcp",
        "version": __version__,
        "status": "ok",
        "provider": cfg.provider,
        "write_enabled": cfg.allow_pv_write,
        "write_pattern": cfg.pv_write_pattern or "(none)",
        "write_rate_limit": cfg.write_rate_limit,
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        "python_version": sys.version.split()[0],
        "p4p_version": p4p_version,
        "channelfinder_enabled": bool(cfg.channelfinder_url),
        "archiver_enabled": bool(cfg.archiver_url),
        "alarm_enabled": bool(cfg.alarm_url),
        # olog as an enabled-boolean only (never the URL, an ESS host, name-capable plane).
        "olog_enabled": bool(cfg.olog_url),
    }


def get_epics_config() -> dict[str, object]:
    """Non-secret configuration values."""
    cfg = get_config()
    return {
        "provider": cfg.provider,
        "default_timeout": cfg.default_timeout,
        "max_batch_size": cfg.max_batch_size,
        "max_monitor_duration": cfg.max_monitor_duration,
        "max_monitor_events": cfg.max_monitor_events,
        "allow_pv_write": cfg.allow_pv_write,
        "pv_write_pattern": cfg.pv_write_pattern or "(none)",
        "write_rate_limit": cfg.write_rate_limit,
        "channelfinder_url": cfg.channelfinder_url or "(disabled)",
        "archiver_url": cfg.archiver_url or "(disabled)",
        "alarm_url": cfg.alarm_url or "(disabled)",
    }
