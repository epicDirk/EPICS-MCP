"""Per-gate deny-path contract test, what makes the write-gate contract CI-enforced, not prose.

The contract lives in ``docs/write-gate-contract.md`` (CLAUDE.md carries the one-line-per-point
summary). Its six points are: (1) an env on/off gate, default OFF; (2) an allowlist whose
empty-semantics are fail-closed in a deliberate per-surface *shape*; (3) a rate limit where a write
denied by the gate consumes no token; (4) a mandatory, metadata-only, durable audit of every gate
verdict; (5) a fail-closed reach/URL boundary where the surface has network reach; (6) an honest
scope statement plus **every deny path must be red-provable**.

This module is point 6 made executable, and it is the template a THIRD write surface inherits.
It has two halves that must agree:

* **Executable rows**: :data:`DENY_PATHS` lists every way each gate can refuse. Each row is driven
  for real and checked on four axes: the typed exception, its machine-readable ``error_code``,
  exactly one terminal ``event=DENY`` audit line carrying the expected code, and the point-3
  invariant that the denial consumed no rate token.
* **A drift guard**: the AST scan counts the ``_audit_deny`` call sites per gate module and
  compares them against the same canonical map. A new deny path that nobody registered here fails
  the build instead of shipping untested.

The two gates deliberately differ where the contract says the shape is a per-surface choice:
the PV gate *refuses to start* on an empty name-pattern and emits ATTEMPT/UNKNOWN around its
mid-flight-interruptible put; the Olog gate *constructs and denies at runtime* on an empty logbook
allowlist and has no mid-flight window. Neither is fail-open, see contract point 2.

**Why the no-token assertion compares CONTENT, not length.** Two of the nine rows are rate-limit
denials, and a rate-limit denial can only fire once the window is FULL: at the moment of refusal the
token deque holds ``maxlen`` entries, so ``len(...) == 0`` is unreachable there. Comparing the
length before and after is worse than useless, the deque is at ``maxlen``, so a wrongly appended
token *evicts* an old one and the length is unchanged, leaving the guard green under the very bug it
exists to catch. Comparing the deque's *contents* goes red on all nine rows. For the same reason a
config with ``rate_limit=0`` (an empty deque) must never be used to make this assertion look easy:
it would pass without proving anything.

**Deliberately OUT of scope**: three distinct buckets, kept apart because they fail differently:

1. *Boot refuses.* A write-enabled process with no durable audit sink configured refuses to start
   (contract point 4); that is ``test_server.py::test_main_refuses_write_enabled_without_durable_
   audit_sink``, not a per-call denial.
2. *Posture-dependent construction refuses (PV).* An empty write pattern or a non-loopback EPICS
   search reach raise ``SafetyConfigError`` from ``SafetyLayer.__init__``, before an audit logger
   exists, so they emit no DENY line by construction. Covered in ``test_safety.py``.
3. *Lazy construction refuses (Olog).* The same class of failure, but the Olog gate is built on
   first use, so its unwritable-sink refusal surfaces at the first write rather than at boot.
   Covered in ``test_olog_write.py``.

Also out of scope, and for a different reason: ``BOUNDS_DENY`` (``safety.py::audit_bounds_deny``) is
a real audited event but *not* a gate verdict, it fires after the gate admitted the write, so it
legitimately HAS consumed a token and would (correctly) violate the point-3 invariant asserted here.

**Pre-gate refusals, the error-code axis is closed, the audit axis is a reasoned scope limit.**
A refusal can happen *before* a gate is consulted; today's one such site is the read throttle in
``services/_http.py`` (the whole-mode preconditions that used to sit in
``services/checkers_olog.py`` were removed 2026-08-01; the round-tripping tools now consult the
gate's env + URL checks BEFORE their pre-write read, see
:func:`test_round_trip_write_is_gate_denied_before_any_read`). Where this module stands:

* **Error code, closed, and guarded as a RULE.** A pre-gate refusal must not carry a gate's
  audited code, and :func:`test_no_pre_gate_refusal_carries_a_gate_error_code`
  sweeps BOTH layers the contract names, ``services/`` and ``tools/``, resolving a raise through
  aliases, relative imports, attribute callees, locally defined subclasses (minus the ones that
  override the code) and plain ``raise Cls``, so a new case fails the build rather than shipping.
* **Audit line, deliberately none for a pre-gate refusal.** Contract point 4
  clamps the audit promise to *gate verdicts and writes that reach the I/O*; a pre-gate refusal is
  neither, no gate ran, no token was taken, no write was attempted. Emitting a gate ``DENY`` from
  ``services/`` would also put an audit call site OUTSIDE the reach of the ``_audit_deny`` drift
  guard below (which scans the gate modules only), i.e. buy a new blind spot in the name of a fix.
* **The read throttle's own code was the wider decision.** ``ReadThrottle`` guards READS, but it is
  reached from the reads the Olog write tools do before their gate, and it shared the write gates'
  audited ``RATE_LIMIT_EXCEEDED``. Giving it ``READ_RATE_LIMIT_EXCEEDED`` was preferred over
  declaring a named exception to the rule: a rule filed as "hard" that carries a carve-out from day
  one is prose again. The cost is a wider blast radius on the read path (the throttle is opt-in,
  default off) and it was accepted knowingly.

**On live coverage, per surface, the premise is not the same on both.** For the PV gate every deny
path raises before any network I/O, so a live deny test would execute identical code beside an
unused socket; the in-memory rows are the honest coverage, not a lesser substitute. The Olog gate
splits: its env and URL checks also raise before any I/O (since 2026-08-01 the round-tripping tools
run them BEFORE their pre-write read, see
:func:`test_round_trip_write_is_gate_denied_before_any_read`), but its logbook allowlist cannot,
that verdict is keyed on logbooks only the server can supply. So for the allowlist branch these
rows prove the gate *method*, not that the tool path feeds it what the wire actually returned.
Contract point 6 prefers a live deny test over an in-memory one, and for exactly that class it
exists: ``tests/test_write_gate_live.py`` drives ``olog_allowlist_miss`` on both round-tripping
tools against a real Olog, keyed on logbooks the SERVER reported. One path is covered that way,
deliberately, it is the only one whose decision input a mock cannot falsify; that module's
docstring argues each of the other eight out by name. So: **PV = not applicable with a reason,
Olog = one path live, the rest reasoned.** Not "every deny path is proven against the wire".
"""

from __future__ import annotations

import ast
import collections
import importlib
import logging
import re
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
from fastmcp.exceptions import ToolError

import epics_mcp.config as config_module
import epics_mcp.olog_safety as olog_safety_module
import epics_mcp.safety as safety_module
import epics_mcp.services._http as http_module
import epics_mcp.services.checkers_olog as checkers_olog
from epics_mcp.config import EpicsConfig
from epics_mcp.errors import (
    EpicsError,
    OlogWriteDeniedError,
    PVWriteDeniedError,
    RateLimitError,
    ReadRateLimitError,
)
from epics_mcp.olog_safety import OlogWriteGate
from epics_mcp.safety import SafetyLayer

# A writes-ON SafetyLayer asserts its EPICS search reach at construction (E8). The autouse env
# strip in conftest leaves *_AUTO_ADDR_LIST unset, which is parser-faithfully "broadcast ON", so
# without the loopback lane every writes-on build below would raise SafetyConfigError instead.
pytestmark = pytest.mark.usefixtures("loopback_write_env")

_PV_AUDIT_LOGGER = "epics_mcp.audit"
_OLOG_AUDIT_LOGGER = "epics_mcp.olog_audit"
_AUDIT_DENY = "_audit_deny"

# Synthetic placeholders only (facility-agnostic guard): a made-up power-supply setpoint and a
# made-up logbook host. Nothing here names a real device, host or person.
_PV_TARGET = "SIM:PS-01:Cur-SP"
_PV_ALLOWLIST = r"^SIM:PS-01:.*-SP$"
_PV_OFF_ALLOWLIST = "SIM:PS-02:Cur-SP"

#: The remote Olog the URL-boundary row refuses. Deliberately the NASTIEST spelling the boundary can
#: be given rather than a tidy one, so assertion (v) below is a real red-proof: a userinfo, a
#: query-string token, and a mixed-case host. A regression that redacted instead of omitting would
#: print one of `https://logbook-remote:8443/Olog` (scheme kept), `logbook-remote:8443` (host only)
#: or the raw value, and each of the three fails a different half of that assertion. Synthetic host,
#: synthetic password (facility-agnostic guard).
_OLOG_REMOTE_TARGET = "https://svc:s3cr3tP4ss@Logbook-Remote:8443/Olog?token=t0k"

# Small enough to fill the window in two calls, and >=1 so the deque is never empty, an empty
# deque would make the no-token assertion vacuous (see module docstring).
_RATE_LIMIT = 2


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset the config and both write-gate singletons so every row builds its gate fresh."""
    config_module._config = None
    safety_module._safety = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    safety_module._safety = None
    olog_safety_module._olog_safety = None


class _WriteGate(Protocol):
    """The seam every write gate shares, the sliding-window token deque of contract point 3."""

    _timestamps: collections.deque[float]


@dataclass(frozen=True)
class DenyPath:
    """One way a write gate can refuse, as an executable contract row.

    ``arm`` returns a freshly built gate already in the state where this path fires, together with
    the thunk that triggers the refusal, so the two gates' differently-shaped
    ``check_write_allowed`` signatures stay inside their own closures and the test body stays
    uniform.
    """

    path_id: str
    module: str
    """Gate module filename, ties this row to the AST scan below."""
    audit_logger: str
    arm: Callable[[], tuple[_WriteGate, Callable[[], None]]]
    exception: type[EpicsError]
    exception_error_code: str
    audit_error_code: str
    """The code in the DENY audit line. NOT always the exception's own, see attach_too_large."""
    # No per-row opt-out for contract point 5 on purpose, see assertion (v) in the test below: a
    # field that can be set to "not applicable" is a naming convention, not a guard.


# ======================================================================================
# PV gate (SafetyLayer): env gate, name allowlist, rate limit
# ======================================================================================


def _arm_pv_env_off() -> tuple[_WriteGate, Callable[[], None]]:
    gate = SafetyLayer(EpicsConfig())  # the shipping default: writes OFF (contract point 1)
    return gate, lambda: gate.check_write_allowed(_PV_TARGET)


def _arm_pv_allowlist_miss() -> tuple[_WriteGate, Callable[[], None]]:
    gate = SafetyLayer(
        EpicsConfig(allow_pv_write=True, pv_write_pattern=_PV_ALLOWLIST, write_rate_limit=10)
    )
    return gate, lambda: gate.check_write_allowed(_PV_OFF_ALLOWLIST)


def _arm_pv_rate_limit() -> tuple[_WriteGate, Callable[[], None]]:
    # The window must be FULL for this path to fire at all, hence a NON-empty token deque at the
    # moment of denial, which is exactly why the invariant compares contents and not length.
    gate = SafetyLayer(
        EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=_RATE_LIMIT)
    )
    for _ in range(_RATE_LIMIT):
        gate.check_write_allowed(_PV_TARGET)
    return gate, lambda: gate.check_write_allowed(_PV_TARGET)


# ======================================================================================
# Olog gate (OlogWriteGate): logbooks, env gate, URL boundary, allowlist, size cap, rate limit
# ======================================================================================


def _olog_config(
    *,
    olog_url: str = "http://localhost:8080/Olog",
    allow_olog_write: bool = True,
    olog_write_logbooks: str = "Ops",
    olog_write_rate_limit: int = 5,
    olog_attach_max_bytes: int = 1024,
    read_rate_limit: int = 0,
    olog_write_url_allowlist: str = "",
    olog_write_allow_remote: bool = False,
) -> EpicsConfig:
    """Olog writes enabled against a loopback sandbox; every value synthetic.

    ``read_rate_limit`` is the READ throttle and stays at its production default (0 = disabled)
    unless a test arms it deliberately: it is not a write gate, it is the one pre-gate refusal
    this module holds against contract point 4.

    The two remote-lane fields are pinned CLOSED rather than left out, and that is not tidiness.
    ``EpicsConfig`` is pydantic-settings with ``env_prefix="EPICS_MCP_"``, and ``tests/conftest.py``
    deliberately strips only the ``EPICS_PVA_*``/``EPICS_CA_*`` families, so every field left unset
    here is read from whoever runs pytest. Measured: with
    ``EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST`` and ``EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true`` exported,
    the URL-boundary rows stopped denying and went to the network instead (5.7s to 34.8s). A row
    that silently changes its verdict with the developer's environment proves nothing on either
    outcome.
    """
    return EpicsConfig(
        olog_url=olog_url,
        allow_olog_write=allow_olog_write,
        olog_write_logbooks=olog_write_logbooks,
        olog_write_rate_limit=olog_write_rate_limit,
        olog_attach_max_bytes=olog_attach_max_bytes,
        read_rate_limit=read_rate_limit,
        olog_write_url_allowlist=olog_write_url_allowlist,
        olog_write_allow_remote=olog_write_allow_remote,
        olog_write_user="epics-pv-logbook-svc",
        olog_write_password="pw",
    )


def _arm_olog_empty_logbooks() -> tuple[_WriteGate, Callable[[], None]]:
    # An empty list would slip through the "logbooks ⊆ allowlist" check (set() <= anything), so the
    # gate guards it first.
    gate = OlogWriteGate(_olog_config())
    return gate, lambda: gate.check_write_allowed([])


def _arm_olog_env_off() -> tuple[_WriteGate, Callable[[], None]]:
    gate = OlogWriteGate(_olog_config(allow_olog_write=False))
    return gate, lambda: gate.check_write_allowed(["Ops"])


def _arm_olog_boundary_reject() -> tuple[_WriteGate, Callable[[], None]]:
    # Non-loopback and not exactly allowlisted → refused (contract point 5).
    gate = OlogWriteGate(_olog_config(olog_url=_OLOG_REMOTE_TARGET))
    return gate, lambda: gate.check_write_allowed(["Ops"])


def _arm_olog_allowlist_miss() -> tuple[_WriteGate, Callable[[], None]]:
    gate = OlogWriteGate(_olog_config(olog_write_logbooks="Ops"))
    return gate, lambda: gate.check_write_allowed(["Maintenance"])


def _arm_olog_attach_too_large() -> tuple[_WriteGate, Callable[[], None]]:
    gate = OlogWriteGate(_olog_config(olog_attach_max_bytes=1024))
    return gate, lambda: gate.check_write_allowed(["Ops"], attachment_bytes=2048)


def _arm_olog_rate_limit() -> tuple[_WriteGate, Callable[[], None]]:
    gate = OlogWriteGate(_olog_config(olog_write_rate_limit=_RATE_LIMIT))
    for _ in range(_RATE_LIMIT):
        gate.check_write_allowed(["Ops"])
    return gate, lambda: gate.check_write_allowed(["Ops"])


# ======================================================================================
# The canonical map: the single place a new deny path gets registered
# ======================================================================================

DENY_PATHS: tuple[DenyPath, ...] = (
    DenyPath(
        path_id="pv_env_off",
        module="safety.py",
        audit_logger=_PV_AUDIT_LOGGER,
        arm=_arm_pv_env_off,
        exception=PVWriteDeniedError,
        exception_error_code="PV_WRITE_DENIED",
        audit_error_code="PV_WRITE_DENIED",
    ),
    DenyPath(
        path_id="pv_allowlist_miss",
        module="safety.py",
        audit_logger=_PV_AUDIT_LOGGER,
        arm=_arm_pv_allowlist_miss,
        exception=PVWriteDeniedError,
        exception_error_code="PV_WRITE_DENIED",
        audit_error_code="PV_WRITE_DENIED",
    ),
    DenyPath(
        path_id="pv_rate_limit",
        module="safety.py",
        audit_logger=_PV_AUDIT_LOGGER,
        arm=_arm_pv_rate_limit,
        exception=RateLimitError,
        exception_error_code="RATE_LIMIT_EXCEEDED",
        audit_error_code="RATE_LIMIT_EXCEEDED",
    ),
    DenyPath(
        path_id="olog_empty_logbooks",
        module="olog_safety.py",
        audit_logger=_OLOG_AUDIT_LOGGER,
        arm=_arm_olog_empty_logbooks,
        exception=OlogWriteDeniedError,
        exception_error_code="OLOG_WRITE_DENIED",
        audit_error_code="OLOG_WRITE_DENIED",
    ),
    DenyPath(
        path_id="olog_env_off",
        module="olog_safety.py",
        audit_logger=_OLOG_AUDIT_LOGGER,
        arm=_arm_olog_env_off,
        exception=OlogWriteDeniedError,
        exception_error_code="OLOG_WRITE_DENIED",
        audit_error_code="OLOG_WRITE_DENIED",
    ),
    DenyPath(
        path_id="olog_boundary_reject",
        module="olog_safety.py",
        audit_logger=_OLOG_AUDIT_LOGGER,
        arm=_arm_olog_boundary_reject,
        exception=OlogWriteDeniedError,
        exception_error_code="OLOG_WRITE_DENIED",
        audit_error_code="OLOG_WRITE_DENIED",
    ),
    DenyPath(
        path_id="olog_allowlist_miss",
        module="olog_safety.py",
        audit_logger=_OLOG_AUDIT_LOGGER,
        arm=_arm_olog_allowlist_miss,
        exception=OlogWriteDeniedError,
        exception_error_code="OLOG_WRITE_DENIED",
        audit_error_code="OLOG_WRITE_DENIED",
    ),
    DenyPath(
        path_id="olog_attach_too_large",
        module="olog_safety.py",
        audit_logger=_OLOG_AUDIT_LOGGER,
        arm=_arm_olog_attach_too_large,
        exception=OlogWriteDeniedError,
        # The ONE row where the audit code and the exception code diverge: the gate audits the
        # specific reason while the raised error carries the generic gate code. Pinned here so the
        # divergence is deliberate and visible rather than an accident nobody checks.
        exception_error_code="OLOG_WRITE_DENIED",
        audit_error_code="OLOG_ATTACH_TOO_LARGE",
    ),
    DenyPath(
        path_id="olog_rate_limit",
        module="olog_safety.py",
        audit_logger=_OLOG_AUDIT_LOGGER,
        arm=_arm_olog_rate_limit,
        exception=RateLimitError,
        exception_error_code="RATE_LIMIT_EXCEEDED",
        audit_error_code="RATE_LIMIT_EXCEEDED",
    ),
)


# ======================================================================================
# Half 1: every registered deny path, driven for real
# ======================================================================================


@pytest.mark.parametrize("path", DENY_PATHS, ids=lambda path: path.path_id)
def test_deny_path_satisfies_the_contract(path: DenyPath, caplog: pytest.LogCaptureFixture) -> None:
    """Contract points 3, 4 and 6 for ONE deny path of ONE gate.

    RED-PROOF (per row, by deleting the guard that row asserts):
    * (i)/(iv): drop the ``raise`` from that path in ``safety.py`` / ``olog_safety.py`` →
      ``DID NOT RAISE``.
    * (ii): drop that path's ``self._audit_deny(...)`` call → ``expected exactly one DENY line,
      got []``.
    * (iii): the deny branches append no token, so DELETING them proves nothing about the token
      invariant. The mutant that drives it red is the opposite one, append a token on the deny
      path (e.g. ``self._timestamps.append(time.monotonic())`` before the raise) → the content
      comparison fails on every row, including the two rate-limit rows where a length comparison
      would stay green because the full deque merely evicts its oldest entry.
    * (v): interpolate the refused URL back into ``olog_safety``'s boundary refusal (its pre-fix
      shape, ``f"... target {self._config.olog_url!r} ..."``) → the ``olog_boundary_reject`` row
      fails on the structural half; redact it with ``url_without_credentials`` instead and it fails
      on the structural half too (the scheme survives a rebuild); print only the parsed host and it
      fails on the derived half. Restoring ``details={"olog_url": ...}`` alone fails it as well,
      because both carriers are checked. The other eight rows are covered by the same assertion
      rather than excused from it, see the note beside it.
    """
    gate, fire = path.arm()
    tokens_before = list(gate._timestamps)
    caplog.clear()  # priming a rate window must not leak into the assertions below

    with (
        caplog.at_level(logging.INFO, logger=path.audit_logger),
        pytest.raises(path.exception) as excinfo,
    ):
        fire()

    # (i) + (iv) the refusal is typed AND machine-readable
    assert excinfo.value.error_code == path.exception_error_code

    # (ii) exactly ONE terminal DENY line, carrying this path's reason, and no success record
    deny_lines = [record.message for record in caplog.records if "event=DENY" in record.message]
    assert len(deny_lines) == 1, f"expected exactly one DENY line, got {deny_lines}"
    assert f"error_code={path.audit_error_code}" in deny_lines[0]
    assert "event=ALLOW" not in caplog.text

    # (iii) a write denied BY THE GATE consumes no rate token (contract point 3)
    assert list(gate._timestamps) == tokens_before, "a gate denial must not consume a rate token"

    # (v) contract point 5: a gate refusal hands back no service address. Applied to EVERY row with
    # no opt-out, and that shape is the finding rather than the style: a per-row "not applicable"
    # field was tried first and a mutant could switch it off (or let it drift away from the URL the
    # row actually arms) while a credential sat on the wire, and both mutants stayed green. A rule
    # nine rows must satisfy cannot be turned off one row at a time.
    #
    # Two criteria, neither of which needs to know the secret (decision WY). The structural one
    # rejects an address-shaped fragment outright. The derived one is read off the ARMED gate's own
    # config rather than from a literal copied into the row, so a redaction that dropped the scheme
    # and printed a bare host still fails, and no drift between the row and its arm function is
    # possible. Rows whose gate has no service URL (every PV row) skip only the second.
    #
    # This does NOT forbid naming a refused target as such: the PV allowlist miss names its PV and
    # the Olog allowlist miss names its logbooks, and both stay green here, because neither value is
    # address-shaped. Contract point 5 states the rule by data class for exactly that reason.
    refusal = str(excinfo.value)
    details = str(excinfo.value.details)
    for carrier, text in (("message", refusal), ("details", details)):
        # The two service schemes rather than a bare "://", which would also reject an MCP resource
        # URI: the refusal is allowed, and expected, to point a caller at ``epics-pv://health``.
        # What may not appear is a transport address, and those are the two schemes a service URL
        # can have here.
        assert "http://" not in text and "https://" not in text, (
            f"{path.path_id}: the refusal's {carrier} carries a service address"
        )
        assert "@" not in text, f"{path.path_id}: the refusal's {carrier} carries a userinfo mark"
    gate_config = getattr(gate, "_config", None)
    configured_host = http_module.url_host(getattr(gate_config, "olog_url", "") or "")
    if configured_host:
        for carrier, text in (("message", refusal), ("details", details)):
            assert configured_host not in text, (
                f"{path.path_id}: the refusal's {carrier} names the host it refused"
            )


def test_rate_limited_rows_deny_on_a_non_empty_window() -> None:
    """The no-token invariant must not be provable by accident on an empty deque.

    If a future edit set a rate limit of 0 (or primed nothing), the two rate-limit rows would assert
    ``[] == []`` and prove nothing. Pin that they really do deny with a FULL window.
    """
    for path in DENY_PATHS:
        if not path.path_id.endswith("rate_limit"):
            continue
        gate, _ = path.arm()
        assert len(gate._timestamps) == _RATE_LIMIT, (
            f"{path.path_id}: expected a full token window before the denial, "
            f"got {len(gate._timestamps)}"
        )


# ======================================================================================
# Half 2: the drift guard: the code's deny call sites vs. the canonical map
# ======================================================================================

_GATE_PACKAGE_DIR = Path(safety_module.__file__).parent
_REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_DENY_CALL_SITES: dict[str, Counter[str]] = {
    "safety.py": Counter({"PV_WRITE_DENIED": 2, "RATE_LIMIT_EXCEEDED": 1}),
    "olog_safety.py": Counter(
        {"OLOG_WRITE_DENIED": 4, "OLOG_ATTACH_TOO_LARGE": 1, "RATE_LIMIT_EXCEEDED": 1}
    ),
}


def _discover_gate_modules() -> dict[str, ast.Module]:
    """Every package module that defines an ``_audit_deny``, i.e. every in-server write gate.

    Discovered by scanning, not hard-coded: a THIRD gate module enrols itself automatically and
    then fails :func:`test_deny_call_sites_match_the_canonical_map` until someone registers its
    deny paths above. Hard-coding two paths here would let a third gate ship unwatched.
    """
    modules: dict[str, ast.Module] = {}
    for module_path in sorted(_GATE_PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.FunctionDef) and node.name == _AUDIT_DENY
            for node in ast.walk(tree)
        ):
            modules[module_path.name] = tree
    return modules


def _audit_deny_error_codes(module_name: str, tree: ast.Module) -> Counter[str]:
    """Count the ``error_code`` literal of every ``self._audit_deny(...)`` call site in *tree*.

    The parameter position is read from the module's OWN ``_audit_deny`` signature (``self``
    excluded) rather than hard-coded: the two existing gates already disagree, the PV gate takes
    ``pv_name`` first, the Olog gate takes ``error_code`` first, so a fixed index would be a coin
    flip for a third. Keyword form is honoured too, and a call whose code cannot be resolved to a
    string literal fails LOUDLY: an unreadable call site must never be miscounted as absent, which
    would read exactly like "this deny path was removed".
    """
    index: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _AUDIT_DENY:
            parameters = [argument.arg for argument in node.args.args if argument.arg != "self"]
            assert "error_code" in parameters, (
                f"{module_name}: {_AUDIT_DENY} has no error_code parameter ({parameters}), "
                "the drift-guard anchor broke"
            )
            index = parameters.index("error_code")
            break
    assert index is not None, f"{module_name}: no {_AUDIT_DENY} definition, the anchor broke"

    codes: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != _AUDIT_DENY:
            continue
        keyword = next((kw for kw in node.keywords if kw.arg == "error_code"), None)
        if keyword is not None:
            value: ast.expr | None = keyword.value
        else:
            value = node.args[index] if index < len(node.args) else None
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{module_name}:{node.lineno}: {_AUDIT_DENY} is not called with a literal error_code, "
            "so the drift guard cannot read it, pass a literal, or teach the guard to resolve it. "
            "Failing loudly rather than counting this call site as absent."
        )
        codes[value.value] += 1
    return codes


def test_deny_call_sites_match_the_canonical_map() -> None:
    """The gates' audited deny call sites are exactly the ones registered in this module.

    Counts, not a set of codes: nine paths share only five codes (four Olog paths are all
    ``OLOG_WRITE_DENIED``), so set equality would stay green when a tenth path reuses an existing
    code, the most likely way this drifts, given the Olog gate's numbered check list grows in
    place.

    Honest limit: counting call sites cannot see a path that WIDENS an existing condition instead of
    adding a new ``_audit_deny``, nor a refusal that never calls the gate at all (see the known gap
    in the module docstring).

    RED-PROOF: add or delete any ``self._audit_deny("...")`` call in either gate, or change one of
    its code literals, and this fails with the differing Counter, including the case where the new
    path reuses an existing code, which a set comparison would have missed.
    """
    discovered = _discover_gate_modules()
    assert discovered, "no write-gate module found, the AST anchor broke"
    assert set(discovered) == set(EXPECTED_DENY_CALL_SITES), (
        "the set of write-gate modules changed, register the new gate's deny paths in DENY_PATHS "
        f"and EXPECTED_DENY_CALL_SITES. found={sorted(discovered)} "
        f"expected={sorted(EXPECTED_DENY_CALL_SITES)}"
    )
    for module_name, tree in discovered.items():
        codes = _audit_deny_error_codes(module_name, tree)
        assert codes, f"{module_name}: no {_AUDIT_DENY} call sites, the AST anchor broke"
        assert codes == EXPECTED_DENY_CALL_SITES[module_name], (
            f"{module_name}: audited deny call sites drifted from the canonical map. "
            f"code={dict(codes)} map={dict(EXPECTED_DENY_CALL_SITES[module_name])}"
        )


class _NeverConstructedOlogClient:
    """An ``OlogClient`` stand-in whose construction FAILS.

    The refusal under test must happen before any client exists, so any HTTP round-trip is
    structurally impossible; a service path that builds a client anyway dies loudly here instead
    of passing for the wrong reason.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError(
            "OlogClient must not be constructed for a write target the gate refuses"
        )


class _RawEntryNotFoundOlogClient:
    """An ``OlogClient`` stand-in whose round-trip read reports "no such entry".

    Reaching :meth:`get_raw_entry` proves the call got PAST the gate's env + URL checks and
    performed the read, the positive control for the deny-before-read ordering.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def get_raw_entry(self, log_id: str) -> dict[str, object] | None:
        return None


@pytest.mark.parametrize(
    ("tool", "call"),
    [
        (
            "add_log_attachment",
            lambda: checkers_olog.query_olog_add_attachment("17", attachments=["/probe.bob"]),
        ),
        ("update_log_entry", lambda: checkers_olog.query_olog_update("17", title="probe")),
    ],
)
async def test_round_trip_write_is_gate_denied_before_any_read(
    tool: str,
    call: Callable[[], Awaitable[object]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write protection of the round-tripping tools is the GATE, checked before the read.

    Since the whole-mode preconditions were removed (2026-08-01), ``add_log_attachment`` and
    ``update_log_entry`` are guarded by the write gate alone. Two claims:

    1. Against a URL the gate does not permit, the call is refused with the gate's own audited
       ``OLOG_WRITE_DENIED`` (an ``event=DENY`` line exists), and NO ``OlogClient`` is even
       constructed, so no HTTP request of any kind was made (no entry-existence oracle for a
       denied caller).
    2. POSITIVE CONTROL, same test: against a permitted (loopback) URL the same call passes the
       gate's env + URL checks and REACHES the round-trip read (surfacing that read's definitive
       404), so claim 1 cannot pass vacuously.

    RED-PROOF (mutant): make ``OlogWriteGate._url_write_allowed`` return True (suspend the URL
    boundary) → claim 1's denial vanishes and the never-constructed client detonates.
    """
    caplog.set_level(logging.INFO, logger=_OLOG_AUDIT_LOGGER)

    # --- claim 1: a non-permitted URL is gate-denied before any client exists ---
    config_module._config = _olog_config(olog_url="http://logbook-remote:8080/Olog")
    olog_safety_module._olog_safety = None  # rebuild the gate from this config
    monkeypatch.setattr(checkers_olog, "OlogClient", _NeverConstructedOlogClient)

    with pytest.raises(OlogWriteDeniedError) as excinfo:
        await call()

    assert excinfo.value.error_code == "OLOG_WRITE_DENIED", tool
    assert [r for r in caplog.records if "event=DENY" in r.message], (
        f"{tool}: the URL-boundary refusal must be an audited gate DENY"
    )
    caplog.clear()

    # --- claim 2 (positive control): a permitted URL reaches the round-trip read ---
    config_module._config = _olog_config()
    olog_safety_module._olog_safety = None
    monkeypatch.setattr(checkers_olog, "OlogClient", _RawEntryNotFoundOlogClient)

    with pytest.raises(EpicsError) as read_excinfo:
        await call()
    assert read_excinfo.value.error_code == "OLOG_HTTP_404", (
        f"{tool}: the permitted call must get past the gate and perform the read"
    )


# The URL-boundary refusal exactly as a CALLER receives it, tag included. Spelled out rather than
# imported from the code it guards: a wire string compared against itself proves nothing. Its
# gate-level twin lives in ``tests/test_olog_write.py`` beside the raise; both must be edited
# together, which is the point of writing a wire contract down twice.
_BOUNDARY_DENY_ANSWER = (
    "[OLOG_WRITE_DENIED] Olog write refused: the configured EPICS_MCP_OLOG_URL is not a permitted "
    "write target. Only a loopback host, or an https URL that is in "
    "EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST with EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true, may be "
    "written to. This gate's own verdict is olog_write.target_allowed in epics-pv://health; the "
    "target address is never disclosed to a caller. Do NOT work around this by repointing the "
    "server or by writing to another logbook. Report the refusal to the operator on duty."
)

#: A credential-bearing, non-allowlisted remote Olog. Synthetic host, synthetic password.
_CREDENTIALED_OLOG_URL = "https://svc:s3cr3tP4ss@logbook-remote:8181/Olog"

#: The four Olog write tools with the minimal arguments each one's schema requires. Names and
#: arguments, not callables: this table exists to drive the REGISTERED tools, so anything resolved
#: ahead of time would move the assertion off the boundary it is about.
_WRITE_TOOL_CALLS: tuple[tuple[str, dict[str, object]], ...] = (
    ("create_log_entry", {"title": "probe", "logbooks": "Ops"}),
    ("reply_to_log", {"log_id": "17", "title": "probe", "logbooks": "Ops"}),
    ("add_log_attachment", {"log_id": "17", "attachments": "probe.png"}),
    ("update_log_entry", {"log_id": "17", "title": "probe"}),
)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    _WRITE_TOOL_CALLS,
    ids=[name for name, _ in _WRITE_TOOL_CALLS],
)
async def test_url_boundary_refusal_never_discloses_the_target(
    tool_name: str, arguments: dict[str, object]
) -> None:
    """Contract point 5 at the REAL tool boundary: the refusal hands back no service URL.

    Driven through ``mcp.get_tool(...).run(...)``, i.e. the registered tool with its
    ``translate_epics_errors`` decorator, because that decorator is what turns the gate's exception
    into the single string a caller receives. A gate-object assertion cannot see that string, and a
    client-class double cannot either.

    Two kinds of assertion, and their ORDER is load-bearing rather than cosmetic. The structural
    pair runs FIRST because it is the security criterion and it is secret-agnostic (decision WY):
    a check built from the CONFIGURED password would be blind exactly where the transport re-encodes
    it, so what is asserted instead is that no address-shaped fragment survives at all. The exact
    answer runs SECOND as the wire contract. Written the other way round the structural pair would
    be unreachable: a leak breaks the equality first, and while the equality holds the pair cannot
    fail, so it would assert nothing at all. This way each half has its own mutant, a restored leak
    reddens the structural pair and any rewording reddens the equality.

    That it needs no Olog and no network holds by CONSTRUCTION rather than by hope, and the reason
    it had to be made to is measured: two of the three fields deciding the remote lane used to be
    read from whoever ran pytest, and with them exported this row stopped denying and went to the
    network. ``_olog_config`` now pins all three, so the refusal is the only reachable outcome.
    A ``_NeverConstructedOlogClient`` double would say the same thing a second time and was left
    out deliberately: it would add a client-class double to the population that
    ``scripts/guard_audit.py`` pins, i.e. move a guard number to restate a fact the config fixes.

    RED-PROOF: revert ``olog_safety.check_write_env_and_url``'s URL branch to its pre-fix shape
    (``f"... target {self._config.olog_url!r} is not a permitted write target. ..."``) and all four
    rows fail on the structural pair.
    """
    from epics_mcp.server import mcp  # imported here: only this test needs the whole server built

    config_module._config = _olog_config(olog_url=_CREDENTIALED_OLOG_URL)
    olog_safety_module._olog_safety = None  # rebuild the gate from this config

    tool = await mcp.get_tool(tool_name)
    # Not type appeasement: the four Olog write tools are registered unconditionally (unlike the
    # display-gated ones), so a None here is a registration regression, not a missing extra.
    assert tool is not None, f"{tool_name} is not a registered tool"
    with pytest.raises(ToolError) as excinfo:
        await tool.run(arguments)

    answer = str(excinfo.value)
    # The two service schemes, not a bare "://": the answer deliberately names the MCP resource
    # ``epics-pv://health``, and a rule that rejected every scheme would forbid its own remedy.
    assert "http://" not in answer and "https://" not in answer, (
        f"{tool_name}: the answer still carries a service address"
    )
    assert "@" not in answer, f"{tool_name}: the answer still carries a userinfo separator"
    assert answer == _BOUNDARY_DENY_ANSWER, tool_name


@pytest.mark.parametrize(
    ("tool", "call"),
    [
        (
            "add_log_attachment",
            lambda: checkers_olog.query_olog_add_attachment("17", attachments=["/probe.bob"]),
        ),
        ("update_log_entry", lambda: checkers_olog.query_olog_update("17", title="probe")),
    ],
)
async def test_pre_gate_refusal_is_coded_apart_and_writes_no_audit_line(
    tool: str,
    call: Callable[[], Awaitable[object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The behavioural half of contract point 4, held against today's pre-gate refusal.

    A refusal can happen BEFORE a gate is consulted. Until 2026-08-01 the site this test used was
    the whole-mode precondition; the redaction removal deleted it (decision PI) and with it the
    behavioural pin, while the contract kept making the promise and this module's prose kept
    claiming it was pinned. The site that remains is the READ THROTTLE, reached from the round-trip
    read these two tools perform after the gate's env + URL checks but BEFORE its verdict on the
    target's logbooks. Two claims:

    1. It reports ``READ_RATE_LIMIT_EXCEEDED``, its OWN code, never the write gates'
       ``RATE_LIMIT_EXCEEDED``, while staying catchable as ``RateLimitError``.
    2. It writes **no audit line at all**. That is what makes its own code NECESSARY rather than
       cosmetic, and it is the promise the contract scopes ("a refusal raised before the gate is
       consulted writes no audit line").

    A POSITIVE CONTROL runs first, through the same ``caplog`` at the same level: a real gate DENY
    on the same logger IS captured. Without it, "no DENY line" would also pass if the audit logger
    were simply not being captured, and ``caplog.set_level`` is therefore set EXPLICITLY here
    instead of relying on the root level.

    RED-PROOF (mutant): give ``ReadRateLimitError`` the write gates' ``RATE_LIMIT_EXCEEDED`` back
    -> claim 1 fails; add a ``get_olog_safety()._audit_deny("OLOG_WRITE_DENIED", caller)`` beside
    the throttle's raise -> claim 2 fails.
    """
    caplog.set_level(logging.INFO, logger=_OLOG_AUDIT_LOGGER)

    # --- positive control: this logger, at this level, DOES capture a gate DENY ---
    control_gate, fire_control = _arm_olog_env_off()
    with pytest.raises(OlogWriteDeniedError):
        fire_control()
    assert [r for r in caplog.records if "event=DENY" in r.message], (
        "the audit logger is not being captured, the no-audit assertion below could never go red"
    )
    assert control_gate is not None
    caplog.clear()

    # --- the pre-gate refusal itself: an armed read throttle with its single token spent ---
    config_module._config = _olog_config(read_rate_limit=1)
    olog_safety_module._olog_safety = None  # rebuild the gate from this config
    http_module.reset_read_throttle()
    try:
        http_module.get_read_throttle().check()  # spend the only token, the next read is refused

        with pytest.raises(ReadRateLimitError) as excinfo:
            await call()

        assert excinfo.value.error_code == "READ_RATE_LIMIT_EXCEEDED", tool
        assert excinfo.value.error_code != "RATE_LIMIT_EXCEEDED"
        assert isinstance(excinfo.value, RateLimitError), tool  # still catchable as the family
        assert caplog.records == [], (
            f"{tool}: a pre-gate refusal must leave NO audit record, got "
            f"{[r.message for r in caplog.records]}"
        )
    finally:
        # A throttle of 1 leaking into the next test would refuse every read in this session.
        http_module.reset_read_throttle()


# ======================================================================================
# Half 3: the RULE the drift guard above cannot see: no PRE-GATE refusal wears a gate code
# ======================================================================================
#
# Half 2 counts ``_audit_deny`` call sites INSIDE the two gate modules. It is blind to the other
# half of contract point 4: a refusal raised ABOVE the gate, which emits no audit line at all and
# must therefore not carry the gate's error code. The contract names that layer explicitly, "a
# precondition in the TOOL or service layer above it", so both are scanned here, and neither is a
# directory the gate-module scan opens.
#
# This half asserts the RULE, not the five known symptoms: a SIXTH pre-gate refusal that reuses a
# gate code fails here the day it is written, in any of the spellings resolved below.

_SERVICES_DIR = _GATE_PACKAGE_DIR / "services"
_TOOLS_DIR = _GATE_PACKAGE_DIR / "tools"

#: The flat packages ABOVE the gates, as ``(directory, package name)``. The package name is carried
#: along because a RELATIVE import (``from ..errors import X``) can only be resolved against it.
_SCANNED_PACKAGES: tuple[tuple[Path, str], ...] = (
    (_SERVICES_DIR, "epics_mcp.services"),
    (_TOOLS_DIR, "epics_mcp.tools"),
)

# The modules that DEFINE this repo's coded exceptions. Two conventions coexist and both are read:
# ``errors.py`` sets ``error_code`` in the CONSTRUCTOR, ``*_exceptions.py`` as a ClassVar.
_EXCEPTION_MODULE_NAMES: tuple[str, ...] = (
    "epics_mcp.errors",
    *(
        f"{package}.{path.stem}"
        for directory, package in _SCANNED_PACKAGES
        for path in sorted(directory.glob("*_exceptions.py"))
    ),
)


def _class_error_code(exception_class: type[BaseException]) -> str | None:
    """The ``error_code`` of *exception_class*, read through whichever convention it uses.

    ClassVar first (``services/*_exceptions.py``): a class-level ``error_code`` is visible on the
    CLASS. Constructor second (``errors.py``): there the attribute is set on the instance, so the
    class attribute lookup misses and the class has to be built once with a probe message. Anything
    that carries no code at all (``OlogFilterValueError`` is a plain ``ValueError``) yields None and
    is simply not part of the map.
    """
    class_level = getattr(exception_class, "error_code", None)
    if isinstance(class_level, str):
        return class_level
    try:
        probe = exception_class("probe")
    except Exception:  # noqa: BLE001 (a class with a different signature is just not in the map)
        return None
    instance_level = getattr(probe, "error_code", None)
    return instance_level if isinstance(instance_level, str) else None


def _exception_codes_by_origin() -> dict[tuple[str, str], str]:
    """``(defining module, class name) -> error_code`` for every coded exception in this repo.

    Keyed on the DEFINING module, not on the bare name, so the AST scan below can resolve a raised
    name through the importing module's own ``from X import Y [as Z]``, alias-proof, and immune to
    two hierarchies happening to reuse a class name.
    """
    codes: dict[tuple[str, str], str] = {}
    for module_name in _EXCEPTION_MODULE_NAMES:
        module = importlib.import_module(module_name)
        for name, member in vars(module).items():
            if not isinstance(member, type) or not issubclass(member, BaseException):
                continue
            if member.__module__ != module_name:
                continue  # re-exported from elsewhere; it is mapped at its own origin
            code = _class_error_code(member)
            if code is not None:
                codes[(module_name, name)] = code
    return codes


def _gate_audit_codes() -> frozenset[str]:
    """The codes the gates emit in their DENY audit lines, DERIVED from the canonical table.

    Never hard-coded: a third gate registering rows with a new audit code widens this guard in the
    same edit, instead of leaving it silently scoped to the two gates that existed when it was
    written.
    """
    return frozenset(path.audit_error_code for path in DENY_PATHS)


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """The module an ``ImportFrom`` refers to, spelled absolutely.

    ``node.module`` alone is a trap: for ``from ..errors import X`` it is the bare ``"errors"`` and
    ``node.level`` (here 2) carries the rest. Looking it up unresolved simply misses, silently, the
    worst failure mode for a guard. Level 1 means "this package", level 2 "the parent", and so on.
    """
    if node.level == 0:
        return node.module
    parts = package.split(".")
    kept = len(parts) - (node.level - 1)
    if kept <= 0:
        return None  # climbs above the top-level package, not importable, nothing to resolve
    base = ".".join(parts[:kept])
    return f"{base}.{node.module}" if node.module else base


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """``spelled prefix -> absolute module`` for every plain ``import`` in this module.

    Covers both ``import epics_mcp.errors as errs`` (prefix ``errs``) and the un-aliased
    ``import epics_mcp.errors`` (prefix ``epics_mcp.errors``), so an attribute-style raise can
    be resolved back to its defining module.
    """
    return {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def _dotted_name(node: ast.expr) -> str | None:
    """``a.b.C`` for a Name/Attribute chain, else None (a subscript or call in the path)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _defines_own_error_code(class_node: ast.ClassDef) -> bool:
    """Does this class set its OWN ``error_code``, rather than inheriting the base's?

    Both repo conventions count: a ClassVar assignment, and an ``__init__`` that passes an
    ``error_code=`` keyword on (the ``PVWriteBoundsError`` shape, calling ``EpicsError.__init__``
    directly). Without this check the inheritance walk below would flag exactly the classes that
    exist BECAUSE of contract point 4: ``ReadRateLimitError``
    subclasses a gate-code carrier and overrides the code. That false positive is the whole
    risk of resolving inheritance at all, so it is checked first.
    """
    for statement in class_node.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        elif isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, ast.FunctionDef) and statement.name == "__init__":
            if any(
                keyword.arg == "error_code"
                for call in ast.walk(statement)
                if isinstance(call, ast.Call)
                for keyword in call.keywords
            ):
                return True
            continue
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "error_code" for target in targets):
            return True
    return False


def _local_names_carrying_gate_codes(
    tree: ast.Module,
    package: str,
    codes_by_origin: dict[tuple[str, str], str],
    gate_codes: frozenset[str],
) -> dict[str, str]:
    """The names, AS SPELLED IN THIS MODULE, that denote an exception carrying a gate audit code.

    Three sources, because a name can reach a gate code three ways:

    * ``from X import Y [as Z]``: absolute or relative, both resolved to the defining module.
    * ``import X [as Z]``: the dotted spelling ``Z.Y`` is registered too, so an attribute-style
      raise resolves.
    * a class DEFINED here that inherits a gate-code carrier without overriding the code, the
      shape a future pre-gate refusal is most likely to take, since the repo has just grown three
      such subclasses (all of which DO override, and are therefore correctly left out).

    The inheritance pass repeats to a fixpoint so a grandchild is caught as well.
    """
    local: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _absolute_module(node, package)
        if module is None:
            continue
        for alias in node.names:
            code = codes_by_origin.get((module, alias.name))
            if code in gate_codes:
                local[alias.asname or alias.name] = code

    for prefix, module in _module_aliases(tree).items():
        for (origin, class_name), code in codes_by_origin.items():
            if origin == module and code in gate_codes:
                local[f"{prefix}.{class_name}"] = code

    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    grew = True
    while grew:
        grew = False
        for class_node in classes:
            if class_node.name in local or _defines_own_error_code(class_node):
                continue
            for base in class_node.bases:
                base_name = _dotted_name(base)
                if base_name is not None and base_name in local:
                    local[class_node.name] = local[base_name]
                    grew = True
                    break

    return local


def _classvar_gate_code_findings(
    module_name: str, tree: ast.Module, gate_codes: frozenset[str]
) -> list[str]:
    """Classes DEFINED in a scanned package that pin a gate audit code as their ClassVar.

    This is the shape the client-side backstops use, and it is how a pre-gate refusal acquires a
    gate code without a single ``raise`` mentioning the string.
    """
    findings: list[str] = []
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for statement in class_node.body:
            targets: list[ast.expr] = []
            if isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                value = statement.value
            elif isinstance(statement, ast.Assign):
                targets = list(statement.targets)
                value = statement.value
            else:
                continue
            if not any(isinstance(t, ast.Name) and t.id == "error_code" for t in targets):
                continue
            if isinstance(value, ast.Constant) and value.value in gate_codes:
                findings.append(
                    f"{module_name}:{statement.lineno}: class {class_node.name} pins the gate "
                    f"audit code {value.value!r} as its error_code"
                )
    return findings


def _names_bound_to_gate_code_instances(
    tree: ast.Module, local_names: dict[str, str]
) -> dict[str, str]:
    """``variable -> code`` for ``exc = <gate-code class>(...)``, so ``raise exc`` is still seen.

    Building the exception first and raising the variable is ordinary Python, not a dodge, and it
    would otherwise slip past the ``raise`` scan entirely.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        callee = _dotted_name(node.value.func)
        if callee is None or callee not in local_names:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = local_names[callee]
    return bound


def _raise_gate_code_findings(
    module_name: str,
    tree: ast.Module,
    local_names: dict[str, str],
    gate_codes: frozenset[str],
) -> list[str]:
    """``raise`` statements above the gates that carry a gate audit code.

    Four spellings are resolved, because the rule is about the CODE that reaches the caller, not
    about one way of writing a raise:

    * ``raise OlogWriteDeniedError(...)``: the class carries the code (also as ``errs.Cls(...)``,
      via the module-alias map, and as a locally defined subclass that does not override it).
    * ``raise EpicsError(..., error_code="OLOG_WRITE_DENIED")``: a literal keyword on a generic
      error; an explicit code wins over the class default, so this is checked first.
    * ``raise OlogWriteDeniedError``: no parentheses. Python instantiates it for you; the caller
      cannot tell the difference, and it is the cheapest way to slip past a callee-only scan.
    * ``exc = OlogWriteDeniedError(...); raise exc``: via the assignment map.

    A bare ``raise`` (re-raising what was caught) is NOT a finding: it propagates an already
    classified code rather than minting one.
    """
    findings: list[str] = []
    bound_names = _names_bound_to_gate_code_instances(tree, local_names)
    for node in (n for n in ast.walk(tree) if isinstance(n, ast.Raise)):
        raised = node.exc
        if raised is None:
            continue  # a bare `raise` re-raises what was caught, not a new refusal
        if isinstance(raised, ast.Call):
            keyword = next((kw for kw in raised.keywords if kw.arg == "error_code"), None)
            if isinstance(keyword, ast.keyword) and isinstance(keyword.value, ast.Constant):
                if keyword.value.value in gate_codes:
                    findings.append(
                        f"{module_name}:{node.lineno}: raises with a literal "
                        f"error_code={keyword.value.value!r}"
                    )
                continue  # an explicit code wins over the class default, gate code or not
            callee = _dotted_name(raised.func)
            if callee is not None and callee in local_names:
                findings.append(
                    f"{module_name}:{node.lineno}: raises {callee} "
                    f"(error_code={local_names[callee]!r})"
                )
            continue
        spelled = _dotted_name(raised)
        if spelled is None:
            continue
        code = local_names.get(spelled) or bound_names.get(spelled)
        if code is not None:
            findings.append(f"{module_name}:{node.lineno}: raises {spelled} (error_code={code!r})")
    return findings


def test_no_pre_gate_refusal_carries_a_gate_error_code() -> None:
    """Contract point 4: "a refusal raised outside the gate must not carry the gate's error code."

    Nothing under ``services/`` or ``tools/`` is a write gate, the two gates live in ``safety.py``
    and ``olog_safety.py`` one directory up. So every refusal raised in those two layers happens
    BEFORE a gate is consulted, writes no audit line, and must be reportable apart from an audited
    gate DENY. Otherwise the audit's coverage claim is unfalsifiable from outside: a caller seeing
    ``OLOG_WRITE_DENIED`` cannot know whether a DENY line exists for it.

    ``tools/`` is scanned because the contract names it: *"a precondition in the TOOL or service
    layer above it"*. It is clean today, so adding it is a no-op with a proof, which is the point:
    the guard's reach should match the rule's reach, not the location of the last five findings.

    RED-PROOF (measured on ``f954cc6``, the commit before the fix): four findings, among them the
    since-removed whole-mode preconditions in ``checkers_olog.py`` (which raised
    ``OlogWriteDeniedError`` directly) and
    ``_http.py`` raising the write gates' ``RateLimitError`` from the read throttle; a fifth
    (``olog_attachments.py``) came from the literal-keyword branch. Re-provable at any time by
    pointing a pre-gate raise at a gate's class or code, or by dropping a probe module with
    any of the spellings :func:`_raise_gate_code_findings` resolves into either scanned package.

    Honest limits, so nobody reads more into a green run than it proves:
    * It sees both packages FLAT, a future sub-package would need its own sweep, and it does not
      look at the src ROOT, where ``errors.py`` and the two gate modules legitimately carry these
      codes and an exclusion list would be needed.
    * A code computed at runtime (``error_code=_olog_error_code(exc)``, the re-raise wrappers) is
      out of static reach. Those propagate an already-classified code rather than minting one:
      but note that a *constructed* literal (``"OLOG_" + "WRITE_DENIED"``) would mint one and is
      equally invisible here. Static analysis buys the common shapes, not every shape.
    * A star-import (``from epics_mcp.errors import *``) and a factory (``raise make_denial()``)
      both hide the name this scan resolves.
    * It proves the CODE axis of point 4 only. The AUDIT axis, that these refusals write no line,
      is a deliberate scope decision documented at the raise sites, and is pinned behaviourally by
      :func:`test_pre_gate_refusal_is_coded_apart_and_writes_no_audit_line`.
    """
    gate_codes = _gate_audit_codes()
    assert gate_codes, "no gate audit codes derived from DENY_PATHS, the anchor broke"
    codes_by_origin = _exception_codes_by_origin()
    assert codes_by_origin, "no coded exceptions discovered, the import anchor broke"

    findings: list[str] = []
    for directory, package in _SCANNED_PACKAGES:
        module_paths = sorted(directory.glob("*.py"))
        assert len(module_paths) > 10, (
            f"only {len(module_paths)} modules found under {directory}, the scan anchor broke"
        )
        for module_path in module_paths:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            local_names = _local_names_carrying_gate_codes(
                tree, package, codes_by_origin, gate_codes
            )
            findings += _classvar_gate_code_findings(module_path.name, tree, gate_codes)
            findings += _raise_gate_code_findings(module_path.name, tree, local_names, gate_codes)

    assert not findings, (
        "a refusal raised OUTSIDE a write gate carries one of the gates' audit error codes "
        f"{sorted(gate_codes)}, give it its own code (write-gate contract point 4):\n  "
        + "\n  ".join(findings)
    )


def test_canonical_map_covers_every_audited_deny_call_site() -> None:
    """The executable rows and the AST truth describe the SAME deny paths.

    Without this, the two halves could drift apart silently: someone could add a row here that no
    gate implements, or bump a count in ``EXPECTED_DENY_CALL_SITES`` without adding the row that
    actually drives the new path red.

    RED-PROOF: delete any entry from ``DENY_PATHS`` (or add one for a path no gate implements) and
    the per-module Counters differ.
    """
    from_rows: dict[str, Counter[str]] = {}
    for path in DENY_PATHS:
        from_rows.setdefault(path.module, Counter())[path.audit_error_code] += 1
    assert from_rows == EXPECTED_DENY_CALL_SITES, (
        "DENY_PATHS and EXPECTED_DENY_CALL_SITES disagree, every audited deny call site needs "
        f"exactly one executable row. rows={ {k: dict(v) for k, v in from_rows.items()} }"
    )


# ======================================================================================
# Half 3: the NUMBERS the prose names vs. the deny paths the code has
# ======================================================================================
#
# Three different numbers circulate about the gates, and every one of them has been written down
# wrongly at least once: this contract's SIX requirements, the PV gate's THREE per-write checks,
# and the logbook gate's SIX. The measured audit of 2026-08-04 recorded five documents calling
# `set_pv_value` "triple-gated" and filed them as WRONG, on the premise that the drive-limit bounds
# check is a fourth gate. Re-measured 2026-08-15 against this very contract (point 4, and the
# scope note in this module's own docstring): it is not, it fires AFTER admission and has spent a
# token, so the five were right all along. What was actually wrong was the OTHER direction, half
# the repository's Olog enumerations silently dropped check 0.
#
# Prose cannot be kept honest by re-reading it, so the two number-bearing FORMS are pinned to the
# AST truth above. Everything else stays prose on purpose: a guard that tried to parse every
# sentence about a gate would be the kind of over-fitted checker this repository keeps deleting.
#
# ⚠️ [GQ-126] NARROWED THAT "everything else", and deliberately rather than by accident. The bare
# `<word> checks` form, excluded below for lacking `gate`, carries four of the seven gate-width
# sentences in `server.py` and the shipped guide. Those seven are now derived by
# `tests/test_prose_counters.py`, off the same `_audit_deny_error_codes` count this module uses, and
# what makes that not over-fitting is that it never parses a sentence: it pairs a number with a noun
# and is told by file plus enclosing scope which gate the block speaks about. The rule here is
# unchanged for THIS module, which still reads whole files and still may not.

#: The single spelling a GATE-SIZE claim uses, so one regex can find every one of them. A count of
#: something else ("the two checks split out of this one") deliberately does not match: it lacks
#: the word `gate`. Markdown emphasis around that word is tolerated because the shipped guide
#: bolds it.
_GATE_SIZE_RE = re.compile(r"\b([a-z]+)\s+\*{0,2}gate\*{0,2}\s+checks\b", re.IGNORECASE)

#: `<word>-gated`, but only for words that are a COUNT. `display-gated`, `config-gated` and
#: `un-gated` are about a condition, not a number, and are none of this guard's business.
_NUMERIC_GATED_RE = re.compile(
    r"\b(single|double|triple|quadruple|quintuple|sextuple)-gated\b", re.IGNORECASE
)

_COUNT_WORDS: dict[int, str] = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
_GATED_WORDS: dict[int, str] = {
    1: "single",
    2: "double",
    3: "triple",
    4: "quadruple",
    5: "quintuple",
    6: "sextuple",
}

#: Which gate each number-bearing file speaks about. A file that talks about BOTH gates is not in
#: here: this guard reads whole files, so a mixed one would need sentence-level attribution, which
#: is exactly the over-fitting the header rejects.
#:
#: ⛔ THIS COMMENT USED TO END "Their numbers are covered by the executable DENY_PATHS rows
#: instead", AND THAT WAS FALSE. ``DENY_PATHS`` pins the call sites in the CODE; nothing read the
#: SENTENCES in ``server.py`` or the shipped guide, and ``test_the_numeric_gated_phrase...`` sweeps
#: the whole tree only for the ``triple-gated`` shape, which neither file uses. Measured for
#: [GQ-126]: seven such sentences stood there, five of them stating the same number, none of them
#: compared to anything. They are now derived in ``tests/test_prose_counters.py`` at
#: ``_GATE_SIZE_SCOPES``, which reads a BLOCK at a time and can therefore do the attribution this
#: comment calls impossible for a whole-file sweep.
#:
#: ⚠️ THE TWO GUARDS DO NOT READ THE SAME THING, and the first version of this note said they did.
#: :func:`_gate_check_count` below reads ``EXPECTED_DENY_CALL_SITES``, the hand-kept map; the prose
#: guard calls :func:`_audit_deny_error_codes` on the parsed module. What holds them together is
#: :func:`test_deny_call_sites_match_the_canonical_map`, which pins the map to that same counter.
#: Drift is therefore impossible THROUGH THE MAP, not through a shared call, and the difference is
#: observable: widen a gate in the code alone and the prose guard goes red at once while this
#: module's size check stays GREEN until the map is corrected. Measured on exactly that mutant by
#: an adversarial pass. A wrong sentence about a guard is what the note above exists to record, so
#: this one records its own.
_SINGLE_GATE_FILES: dict[str, str] = {
    "src/epics_mcp/safety.py": "safety.py",
    "src/epics_mcp/olog_safety.py": "olog_safety.py",
}


def _gate_check_count(module: str) -> int:
    """How many checks a gate has, from the audited deny call sites, never from a literal."""
    return sum(EXPECTED_DENY_CALL_SITES[module].values())


def _tracked_files_for_gate_numbers() -> list[Path]:
    """Every tracked text file, so a NEW document repeating the phrase is covered the day it lands.

    ``git ls-files`` rather than a declared list, the same reasoning (and the same failure policy)
    as ``test_guide.py``: only a MISSING git binary may skip, a git call that RAN and failed must
    fail loudly, because a silent skip switches the guard off in exactly the environments where
    something is already wrong. Binary files are read with ``errors="replace"``, the phrase is
    ASCII and cannot be hidden by a replacement character.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except FileNotFoundError as exc:
        pytest.skip(f"git binary unavailable, cannot enumerate tracked files: {exc}")
    except subprocess.SubprocessError as exc:
        pytest.fail(f"git ls-files failed ({exc}), the gate-number sweep did NOT run")
    this = Path(__file__).resolve()
    return [
        path
        for rel in listing.splitlines()
        if (path := _REPO_ROOT / rel).is_file() and path.resolve() != this
    ]


def test_each_gate_module_states_its_own_size_correctly() -> None:
    """A `<word> gate checks` claim inside a gate module names that gate's measured size.

    RED-PROOF: change either count in ``EXPECTED_DENY_CALL_SITES`` (or write "four gate checks"
    into ``safety.py``) and this fails naming the file, the word found and the word expected.
    """
    findings: list[str] = []
    for rel, module in _SINGLE_GATE_FILES.items():
        expected = _COUNT_WORDS[_gate_check_count(module)]
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for match in _GATE_SIZE_RE.finditer(text):
            found = match.group(1).lower()
            if found in _COUNT_WORDS.values() and found != expected:
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{rel}:{line}: says {found!r}, measured {expected!r}")
    assert not findings, "a gate module miscounts its own checks:\n  " + "\n  ".join(findings)


def test_the_numeric_gated_phrase_matches_the_measured_pv_gate() -> None:
    """Every `triple-gated`-shaped claim in the tracked tree names the PV gate's measured size.

    The phrase is only ever used about ``set_pv_value`` (measured: six occurrences, all of it),
    which is why one expected word serves the whole tree. Should it ever be borrowed for the
    logbook gate, this guard goes red and that is the right answer: the two sizes differ, so the
    borrowed word would be wrong.

    RED-PROOF: bump ``EXPECTED_DENY_CALL_SITES["safety.py"]`` to four and every occurrence is
    reported against ``quadruple``.
    """
    expected = _GATED_WORDS[_gate_check_count("safety.py")]
    findings: list[str] = []
    for path in _tracked_files_for_gate_numbers():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _NUMERIC_GATED_RE.finditer(text):
            found = match.group(1).lower()
            if found != expected:
                line = text[: match.start()].count("\n") + 1
                rel = path.relative_to(_REPO_ROOT).as_posix()
                findings.append(f"{rel}:{line}: says {found!r}-gated, measured {expected!r}")
    assert not findings, (
        "a counted -gated claim disagrees with the PV gate's audited deny paths:\n  "
        + "\n  ".join(findings)
    )


# ======================================================================================
# Half 4: how many conditions make a gate REFUSE TO START
# ======================================================================================
#
# A fourth number circulates about the gates, and the page that keeps the other three apart
# (`docs/write-gate-contract.md`) names it as a fourth category without pinning it. Measured for
# [GQ-130]: three places stated it, two said FOUR and one said FIVE, and nothing compared them.
#
# ⭐ THEY WERE NOT COUNTING THE SAME POPULATION, which is why "which number is right" was the wrong
# question. The five are every `SafetyConfigError` the gate raises; the four are the ones an
# OPERATOR CAN REACH THROUGH A CONFIGURATION, which is also exactly the set the `epics-doctor`
# block has lines for. The fifth guards `write_rate_limit`, and `EpicsConfig` constrains that field
# to `ge=1`, so no environment can trigger it, only a caller that bypassed validation with
# `model_construct`.
#
# THE SPELLING RULE, and it is what makes ONE regex safe over the whole tree. The PLURAL phrase
# `<word> start conditions` always names the TOTAL; a subset is written "N of the five start
# conditions". The SINGULAR ("the third start condition", "a FOURTH start condition") says WHICH
# one a passage is about, and stays out of this guard deliberately. Measured over the tracked tree
# when the rule was written: 2 plural hits carried a count word (both under repair), 10 carried
# none and are skipped, and 7 singular ones are out of scope. Mixing the two would have put every
# ordinal in front of a total and made the guard useless on its first run.

#: A claim about HOW MANY conditions make a gate refuse to start. Plural only, see the rule above.
_START_CONDITIONS_RE = re.compile(r"\b([a-z]+)\s+start conditions\b", re.IGNORECASE)

#: Which module the counted claims speak about. One entry, and the list rather than a bare constant
#: because a second gate stating its own total is the case this should not silently mis-judge:
#: `OlogWriteGate` is built LAZILY, so its equivalents surface at the first write rather than at
#: boot, and no tracked text states them as a total today.
_START_CONDITION_MODULE = "safety.py"


def _start_conditions(module_name: str, tree: ast.Module) -> int:
    """Count the ``SafetyConfigError`` raises of the gate class in *tree*: its refusals to start.

    Read off the code for the same reason the deny paths are: a total written into prose is
    re-typed by hand at every place that repeats it, and this one had already drifted into two
    values before anything compared it.

    A raise that cannot be attributed to the gate class fails LOUDLY rather than being skipped, the
    same policy :func:`_audit_deny_error_codes` follows: an unreadable site miscounted as absent
    reads exactly like "this refusal was removed".
    """
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    gates = [node for node in classes if any(_raises_safety_config_error(node))]
    assert len(gates) == 1, (
        f"{module_name}: expected exactly one class raising SafetyConfigError, found "
        f"{[node.name for node in gates]}. The anchor for the start-condition count broke."
    )
    return len(list(_raises_safety_config_error(gates[0])))


def _raises_safety_config_error(node: ast.AST) -> Iterator[ast.Raise]:
    """Every ``raise SafetyConfigError(...)`` inside *node*, however deeply nested."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Raise)
            and isinstance(child.exc, ast.Call)
            and isinstance(child.exc.func, ast.Name)
            and child.exc.func.id == "SafetyConfigError"
        ):
            yield child


def test_every_start_condition_count_matches_the_gate() -> None:
    """Every `<word> start conditions` claim in the tracked tree names the gate's measured total.

    ⛔ HONEST SCOPE, because this is a NUMBER guard and the thing that historically went wrong with
    this estate's gate prose was a LIST. It compares the figure to the code and never counts the
    enumeration that figure summarises.

    ⭐ THE LIST IS NO LONGER UNCOUNTED, and the sentence that stood here said it was. Until
    [GQ-132] this docstring stated that removing one item from the five listed in
    `docs/write-gate-contract.md` while leaving the word "five" standing kept every test green.
    That was measured and true when it was written, and it is now false:
    `tests/test_gate_lists.py` reads that list and compares it to `_start_conditions` below, the
    same measurement this guard uses, so the two halves cannot disagree about how wide the gate is.
    Proven the same way the claim was: cut a start condition and it reports
    `the list has 4 items, safety.py:start-conditions measures 5`.

    ⚠️ WHAT IS STILL THIS GUARD'S ALONE. The list guard reads a TABLE of registered enumerations,
    not a sweep, so it sees a passage only once somebody anchors it; this guard sweeps every
    tracked file and therefore covers a counted claim the day it lands. The two populations are
    different on purpose and neither contains the other.

    The failure message still names the list, so whoever corrects the figure is told where the
    words that go with it live.

    RED-PROOF: add a sixth ``SafetyConfigError`` raise to ``SafetyLayer`` and every counted claim
    is reported against 'six'.
    """
    tree = ast.parse((_GATE_PACKAGE_DIR / _START_CONDITION_MODULE).read_text(encoding="utf-8"))
    expected = _COUNT_WORDS[_start_conditions(_START_CONDITION_MODULE, tree)]
    findings: list[str] = []
    for path in _tracked_files_for_gate_numbers():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _START_CONDITIONS_RE.finditer(text):
            found = match.group(1).lower()
            if found in _COUNT_WORDS.values() and found != expected:
                line = text[: match.start()].count("\n") + 1
                rel = path.relative_to(_REPO_ROOT).as_posix()
                findings.append(f"{rel}:{line}: says {found!r}, measured {expected!r}")
    assert not findings, (
        "a counted start-condition claim disagrees with the PV gate's refuse-to-start raises:\n  "
        + "\n  ".join(findings)
        + "\n  The plural phrase names the TOTAL; write a subset as 'N of the <total> start "
        "conditions'. And the figure is only half of it: the five are LISTED in "
        "docs/write-gate-contract.md, and nothing checks that list against the code, so correct "
        "the words there in the same change."
    )
