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
unused socket; the in-memory rows are the honest coverage, not a lesser substitute. For the Olog
gate that premise does **not** hold: on the attachment/update tool paths a real HTTP round-trip
happens before the gate is consulted, so these rows prove the gate *method*, not the ordering
inside the tool path. Contract point 6 prefers a live deny test over an in-memory one, and for that
one class it now exists: ``tests/test_write_gate_live.py`` drives ``olog_allowlist_miss`` on both
round-tripping tools against a real Olog, keyed on logbooks the SERVER reported. Exactly one path
is covered that way, deliberately, it is the only one whose decision input a mock cannot falsify;
the module's docstring argues each of the other eight out by name. So: **PV = not applicable with a
reason, Olog = one path live, the rest reasoned.** Not "every deny path is proven against the wire".
"""

from __future__ import annotations

import ast
import collections
import importlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

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
) -> EpicsConfig:
    """Olog writes enabled against a loopback sandbox; every value synthetic.

    ``read_rate_limit`` is the READ throttle and stays at its production default (0 = disabled)
    unless a test arms it deliberately: it is not a write gate, it is the one pre-gate refusal
    this module holds against contract point 4.
    """
    return EpicsConfig(
        olog_url=olog_url,
        allow_olog_write=allow_olog_write,
        olog_write_logbooks=olog_write_logbooks,
        olog_write_rate_limit=olog_write_rate_limit,
        olog_attach_max_bytes=olog_attach_max_bytes,
        read_rate_limit=read_rate_limit,
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
    gate = OlogWriteGate(_olog_config(olog_url="http://logbook-remote:8080/Olog"))
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
