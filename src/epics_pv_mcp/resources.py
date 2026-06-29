"""MCP Resources for the EPICS PV MCP Server."""

import sys
import time

from epics_pv_mcp import __version__
from epics_pv_mcp.config import get_config

_start_time = time.monotonic()


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
        "server": "epics-pv-mcp",
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
