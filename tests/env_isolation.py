"""Which environment variables an offline test must not inherit from the shell running it (GQ-399).

``EpicsConfig`` is a ``BaseSettings`` model: every field a test leaves unset is read from the
PROCESS ENVIRONMENT. Before this module, ``tests/conftest.py`` stripped only the six EPICS
search-path variables, so a developer who exported a server setting for their own sandbox measured
their shell along with the code. Measured on EPICS-MCP ``4240267`` (2026-09-13), full suite:
``EPICS_MCP_READ_RATE_LIMIT=1`` turned 46 tests red, and two exported ChannelFinder allowlists
turned 8 red. A handful of files had pinned the gap for themselves, one field or one fixture at a
time, and two of them said in so many words that the fix belonged in conftest.

The decision is a pure function over an INJECTED environment, the same shape as
``tests/live_gate.py``, so it can be pinned offline without mutating process state; the autouse
fixture in ``tests/conftest.py`` is the only consumer that touches ``os.environ``.

TWO NAMED EXCEPTIONS, and the second is the one that carries the live suite:

* **The harness family.** The variables that configure the TEST HARNESS rather than the server.
  Its holder is ``epics_mcp.config._RESERVED_ENV_REMAINDER_PREFIXES``, which reserves exactly that
  family for the unknown-variable warning with the reason beside each member; this module reads the
  tuple and names none of its members, so one list cannot drift from itself.
* **A test marked ``live``.** A live probe measures the CONFIGURED stack by definition, and it reads
  server settings as well as harness ones: the plane URLs at its setup gate
  (``tests/test_channelfinder_live.py`` gates on ``EPICS_MCP_CHANNELFINDER_URL``), and everything
  else through ``get_config()`` when it builds a client. Exempting only the harness family would
  have stripped those. Read across the twelve live modules on 2026-09-13: the modules whose gate
  reads a plane URL inside a fixture would have skipped silently, the ones that read their gate
  variables into module constants at import time would have passed the gate and gone red in the
  body, and ``test_read_live.py`` gates on harness names alone and would have run. What remains of
  the risk, stated rather than hidden: an UNDEMANDED live run whose exemption broke would in part
  skip silently, which is the class ``tests/live_gate.py`` accepts for every undemanded live run; a
  demanded one (``EPICS_MCP_REQUIRE_LIVE=1``) fails loudly at its setup gate.

The EPICS search-path variables are NOT decided here; they carry no ``EPICS_MCP_`` prefix and keep
their own fixture in ``tests/conftest.py`` (BG14), which strips them for live tests as well.
"""

from __future__ import annotations

from collections.abc import Iterable

from epics_mcp.config import _RESERVED_ENV_REMAINDER_PREFIXES, EpicsConfig


def _config_prefix() -> str:
    """The prefix ``EpicsConfig`` binds to, read off the model rather than typed a second time."""
    prefix = EpicsConfig.model_config.get("env_prefix")
    if not prefix:
        raise AssertionError("EpicsConfig declares no env_prefix, so nothing here can be isolated")
    return prefix


def server_config_names(environ: Iterable[str], *, live: bool) -> list[str]:
    """Return the names in *environ* that configure the server and must not reach this test, sorted.

    Matched CASE-INSENSITIVELY, because that is how the model reads them: ``EpicsConfig`` does not
    set ``case_sensitive``, and pydantic-settings defaults it to off, so
    ``epics_mcp_read_rate_limit`` configures the server on a platform whose environment keeps lower
    case. A name that matches no field (a typo) is selected too: it cannot change a field, but it is
    still the shell's, and the unknown-variable warning would otherwise fire inside an unrelated
    test.

    ``live`` is whether the test carries the ``live`` marker; see the module docstring for why a
    live test keeps everything.
    """
    if live:
        return []
    prefix = _config_prefix().upper()
    return sorted(
        name
        for name in environ
        if name.upper().startswith(prefix)
        and not name[len(prefix) :].lower().startswith(_RESERVED_ENV_REMAINDER_PREFIXES)
    )
