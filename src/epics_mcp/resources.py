"""MCP Resources for the EPICS MCP server."""

import importlib.resources
import sys
import time
from functools import lru_cache

from epics_mcp import __version__
from epics_mcp.config import get_config
from epics_mcp.write_posture import olog_write_gate_report, pv_write_gate_report

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
        importlib.resources.files("epics_mcp")
        .joinpath("operator_guide.md")
        .read_text(encoding="utf-8")
    )


def get_health() -> dict[str, object]:
    """Server health status, including what this process is permitted to write.

    ⚠️ ``write_enabled`` and ``write_pattern`` are the PV gate ALONE, and reading them as the whole
    write posture is the defect this payload was extended to remove: a server with the Olog gate
    armed reports ``write_enabled: false`` while it can create logbook entries.
    ``any_write_gate_armed`` is the question an approver is actually asking, so it is answered in
    one field.

    The gate blocks are projected field by field from :mod:`epics_mcp.write_posture`, never dumped
    wholesale. That report also carries the raw Olog URL and the search-reach violation strings,
    which name hosts and raw environment values verbatim; those belong in an operator's own terminal
    through ``epics-doctor``, not in a payload a client keeps. Projecting by hand is what makes a
    later field added to that report a deliberate disclosure rather than an automatic one.
    """
    cfg = get_config()
    p4p_version = "unknown"
    try:
        import p4p

        p4p_version = p4p.__version__
    except (ImportError, AttributeError):
        pass

    # Both are pure configuration and touch no file, which is why they are safe in a synchronous
    # resource handler; the doctor's composition of them is not, see write_posture's docstring.
    pv_gate = pv_write_gate_report(cfg)
    olog_gate = olog_write_gate_report(cfg)

    return {
        "server": "epics-mcp",
        "version": __version__,
        "status": "ok",
        "provider": cfg.provider,
        # True iff EITHER gate is armed. Derivable, and here anyway: the reported defect IS that an
        # approver derived it from write_enabled and got it wrong. Armed says the gate is armed,
        # never that a write would succeed.
        "any_write_gate_armed": pv_gate.armed or olog_gate.armed,
        "write_enabled": cfg.allow_pv_write,
        "write_pattern": cfg.pv_write_pattern or "(none)",
        "write_rate_limit": cfg.write_rate_limit,
        # Meaningful while the PV gate is armed; with the gate off the pattern is not "narrow", it
        # is irrelevant. Decided against the closed set of allow-everything spellings rather than by
        # interpreting the expression, see write_posture.
        "write_pattern_allows_every_name": pv_gate.pattern_allows_every_name,
        # The Olog gate, which had NO representation here at all. Its URL is deliberately absent for
        # the reason the olog_enabled comment below gives; the two predicates say what an approver
        # needs from it, and target_allowed is ALSO true for an allowlisted remote https target, so
        # the loopback bit is separate rather than folded in.
        "olog_write": {
            "armed": olog_gate.armed,
            "logbooks": olog_gate.logbooks,
            "rate_limit_per_minute": olog_gate.rate_limit_per_minute,
            "target_allowed": olog_gate.target_allowed,
            "target_is_loopback": olog_gate.target_is_loopback,
        },
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
