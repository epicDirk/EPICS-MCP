"""The ONE write-gate deny path that a live run proves and an in-memory row cannot.

Contract point 6 (``CLAUDE.md``, write-gate contract) prefers a *live* deny test over an in-memory
assertion: "an in-memory-only deny test proves the branch, not that the gate fires against the
wire." ``tests/test_write_gate_contract.py`` drives all nine deny paths of both gates in memory.
This module adds the one that is genuinely different against a real server, and says plainly why
the other eight are not worth a live probe, so nobody re-derives that later.

**The one with live value: ``olog_allowlist_miss``** on ``add_log_attachment`` /
``update_log_entry``.
It is the only deny path whose *decision input* is produced by the SERVER. Both tools take a log
**id** and nothing else about the target; the logbook names the gate is keyed on are read out of the
server's own answer (``raw["logbooks"]`` → ``[{"name": ...}]``). A mock cannot falsify that class:
it supplies the very shape the code assumes, so a wrong assumption about the payload passes in
memory and denies (or fails to deny) against the real thing. Here the deny message must name a
logbook this test **never passed to the service**, it can only have travelled id → HTTP GET → gate.

**Not probed live, with reasons (so this list is a decision, not an omission):**

* ``pv_env_off`` / ``pv_allowlist_miss`` / ``pv_rate_limit``, the PV gate refuses before the first
  network I/O. A live run would execute byte-identical code beside an unused socket.
* ``olog_env_off`` / ``olog_boundary_reject``, on these two tool paths they are unreachable: the
  whole-mode precondition (loopback URL + declared test data) is checked *before* them and shadows
  both. A live run that satisfies whole-mode has already satisfied the URL boundary.
* ``olog_empty_logbooks``: unreachable here too: the logbook list is derived from a server entry,
  and an entry always lives in at least one logbook.
* ``olog_attach_too_large``: the size cap is decided from local ``stat`` data; the server never
  sees the request. In-memory is the honest coverage.
* ``olog_rate_limit``: would need N real *mutating* writes to fill the window. The cost is real
  entries on a real service to prove a counter that has no server-side input at all.

**Deliberately self-sufficient, but never self-directed.** The deny *entry* is created here, by
this module, through the RAW client, which talks to Olog *without* passing the gate (contract
point 6: the gate guards writes through the tool path, not the library). Hanging the test on a
pre-existing corpus entry would make it depend on an artifact no code in this repo declares; it
would rot into a silent skip the day that artifact is gone. The *logbook* that entry goes into is
the opposite case: it must be **named by the operator**, because only they know which logbook on
this server is scratch and which one another consumer has frozen.

**The trap this module is built against.** All four Olog gate branches raise the same exception with
the same ``error_code`` and a byte-identical audit line. With one env var missing
(``EPICS_MCP_ALLOW_OLOG_WRITE``) the gate would deny at branch 1 instead of branch 3, and a test
asserting only "it denied" would be GREEN while proving nothing the in-memory rows do not already
prove. Countermeasures, all of them:

1. Match the allowlist branch's OWN message (``not in the write allowlist``), the single
   discriminator between the four branches.
2. Assert the deny names the exact logbook the SERVER reported for the target entry.
3. Assert the exact ``error_code``, after the pre-gate-code fix this also separates a gate DENY
   from the un-audited whole-mode refusal above it.
4. Demand all SIX env vars at setup through ``assert_live_available``, not a one-variable gate, so a
   demanded run (``EPICS_MCP_REQUIRE_LIVE=1``) goes red instead of skipping.
5. Route every "this server cannot host the probe" condition, an unnamed deny logbook, a name that
   does not exist, a name inside the allowlist, through the same live gate, never a bare
   ``pytest.skip``: a bare skip would silently defeat ``EPICS_MCP_REQUIRE_LIVE``.
6. Run a POSITIVE CONTROL per tool: the same call against an ALLOWLISTED entry must get past the
   gate and change something. Without it, an allowlist that denies everything (an empty or wrong
   ``EPICS_MCP_OLOG_WRITE_LOGBOOKS``) would look exactly like a working gate.

**Mutation, honestly scoped, and this module is NOT idempotent.** A full green run leaves **four**
entries on the server: each of the two DENY tests creates its own target in the deny logbook first
(``_create_entry``, through the raw client) and only then trips the gate, it is the *gated call*
that mutates nothing, not the test. Each of the two positive controls creates an entry in the
allowlisted logbook and then really writes to it (an attachment, a title), that is what makes them
controls. Every entry carries a title marking it as a test artifact, because **Olog has no delete**:
an artifact can be labelled, never removed. That is also why the deny logbook must be named
explicitly (see :func:`denied_logbook`), an earlier revision derived it and deposited six entries
in another window's frozen fixture logbook.

Run::

    EPICS_MCP_REQUIRE_LIVE=1 uv run pytest -m live tests/test_write_gate_live.py

with ``EPICS_MCP_OLOG_URL`` / ``_ALLOW_OLOG_WRITE`` /
``_OLOG_WRITE_LOGBOOKS`` / ``_OLOG_WRITE_USER`` / ``_OLOG_WRITE_PASSWORD`` set, and with
``EPICS_MCP_AUDIT_LOG_FILE`` UNSET (this module refuses to run otherwise; see the fixture).

**Required, not optional:** ``EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK`` names the SCRATCH logbook the deny
targets are created in. Unset is a refusal, never a guess, see :func:`denied_logbook`. It is
deliberately absent from ``EpicsConfig``, ``.env.example`` and the operator guide, like every other
``EPICS_MCP_LIVE_*`` variable: ``test_guide_matches_code`` checks each ``EPICS_MCP_*`` token in the
guide against the config and would go red.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

import epics_mcp.config as config_module
import epics_mcp.olog_safety as olog_safety_module
from epics_mcp.errors import OlogWriteDeniedError
from epics_mcp.services._http import basic_auth_header
from epics_mcp.services.checkers_olog import query_olog_add_attachment, query_olog_update
from epics_mcp.services.olog_client import OlogClient
from tests.live_gate import assert_live_available, live_demanded

_URL = os.environ.get("EPICS_MCP_OLOG_URL")
_WRITE = os.environ.get("EPICS_MCP_ALLOW_OLOG_WRITE", "").lower() == "true"
_LOGBOOKS = os.environ.get("EPICS_MCP_OLOG_WRITE_LOGBOOKS", "")
_WRITE_USER = os.environ.get("EPICS_MCP_OLOG_WRITE_USER")
_WRITE_PASSWORD = os.environ.get("EPICS_MCP_OLOG_WRITE_PASSWORD")

# The allowlist branch's own wording, the ONE string that tells branch 3 from branches 0/1/2, all
# of which raise the same class with the same code. Pinned deliberately: if the message is
# reworded, this test must be revisited, not silently degraded to "something denied".
_ALLOWLIST_DENY_MESSAGE = "not in the write allowlist"

# Every entry this module creates is labelled. Olog has no delete, so a test artifact can only be
# made recognisable, never removed.
_ARTIFACT = "write-gate live deny probe (offline test artifact)"

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate (S30) over ALL FIVE prerequisites of a gated Olog write.

    A one-variable gate would be the defect this module is written against: with
    ``EPICS_MCP_ALLOW_OLOG_WRITE`` missing the gate denies at its FIRST branch, the test stays
    green, and it proves nothing. Missing → skip by default, LOUD failure under
    ``EPICS_MCP_REQUIRE_LIVE=1``.
    """
    assert_live_available(
        bool(_URL and _WRITE and _LOGBOOKS and _WRITE_USER and _WRITE_PASSWORD),
        "the live Olog write-gate probe needs a WRITABLE loopback Olog: "
        "EPICS_MCP_OLOG_URL + _ALLOW_OLOG_WRITE + _OLOG_WRITE_LOGBOOKS "
        "+ _OLOG_WRITE_USER + _OLOG_WRITE_PASSWORD",
        demanded=live_demanded(os.environ),
    )


@pytest.fixture(autouse=True)
def _refuse_to_pollute_a_real_audit_trail() -> None:
    """This module deliberately produces DENY verdicts; they must not land in a durable audit file.

    A gate DENY written here is a REAL verdict, not a fabricated one, which is exactly why it would
    be misleading in an operational trail: it records a refusal nobody operationally attempted. With
    the sink unset the audit goes to stderr (ephemeral), which is what a probe run wants. Fail
    loudly rather than skip: a silent skip here would hide the pollution, not prevent it.
    """
    sink = os.environ.get("EPICS_MCP_AUDIT_LOG_FILE", "").strip()
    if sink:
        pytest.fail(
            "EPICS_MCP_AUDIT_LOG_FILE is set, this module emits real gate DENY verdicts and would "
            "write them into a durable audit trail. Unset it for the probe run.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _fresh_gate_and_config() -> Iterator[None]:
    """Build config and the Olog gate from the LIVE process env, not from a leftover singleton."""
    config_module._config = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    olog_safety_module._olog_safety = None


@pytest.fixture
def raw_client() -> OlogClient:
    """A whole-mode client used ONLY to lay down targets, it bypasses the write gate by design.

    Contract point 6: the gate guards writes *through this server's tool path*; the library it
    depends on reaches the same service without it. That property is what makes this module
    self-sufficient, it can create an entry in a logbook the gate would refuse to write to.
    """
    assert _URL is not None and _WRITE_USER is not None and _WRITE_PASSWORD is not None
    return OlogClient(
        _URL,
        timeout=15.0,
        auth_header=basic_auth_header(_WRITE_USER, _WRITE_PASSWORD),
    )


def _allowlisted_logbooks() -> frozenset[str]:
    return frozenset(name.strip() for name in _LOGBOOKS.split(",") if name.strip())


@pytest.fixture
def denied_logbook(raw_client: OlogClient) -> str:
    """The SCRATCH logbook the deny targets go into, **named by the operator, never derived**.

    ⚠️ These tests create a labelled entry in this logbook before the gate refuses, and **Olog has no
    delete**. So the unset case is a refusal, not a guess.

    An earlier revision derived the name as ``sorted(server_logbooks - allowlist)[0]`` and kept that
    as a "convenience fallback". On the machine this was written for, that expression resolves:
    deterministically, by alphabetical order, to the frozen fixture logbook of a PARALLEL window,
    and it deposited six entries there that cannot be removed. Deriving a write target from the
    server's own answer looks self-sufficient and is the opposite: the server cannot know which of
    its logbooks somebody else has frozen. Only the operator knows, so only the operator names it.
    A write probe against a SHARED service owns its data space explicitly, see the evidence
    discipline in ``CLAUDE.md``.

    The name is still validated against the SERVER's own logbook list (an entry cannot be created in
    a logbook that does not exist) and against the allowlist (it must be outside, or the test would
    prove nothing). Every failure, unset, unknown, inside the allowlist, goes through the live
    gate, never a bare ``pytest.skip``: a bare skip would defeat ``EPICS_MCP_REQUIRE_LIVE`` for
    exactly the conditions that make this test meaningful.

    RED-PROOF: run the module with all six prerequisites and ``EPICS_MCP_REQUIRE_LIVE=1`` but
    without ``EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK``, it fails loudly here and creates nothing.
    """
    # The env check runs BEFORE the first server round-trip: live_gate's own contract puts the gate
    # ahead of any connection attempt, and it keeps this refusal provable without a live Olog.
    named = os.environ.get("EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK", "").strip()
    assert_live_available(
        bool(named),
        "EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK is unset, name the SCRATCH logbook the deny probes may "
        "write into (it must exist on this Olog and stay OUTSIDE EPICS_MCP_OLOG_WRITE_LOGBOOKS). "
        "It is not derived: Olog has no delete, and the first non-allowlisted name a server "
        "reports can be another consumer's frozen fixture",
        demanded=live_demanded(os.environ),
    )

    on_server = set(raw_client.list_logbooks())
    assert_live_available(
        named in on_server and named not in _allowlisted_logbooks(),
        f"EPICS_MCP_LIVE_OLOG_DENY_LOGBOOK={named!r} must name a logbook that exists on this "
        "Olog and is NOT in EPICS_MCP_OLOG_WRITE_LOGBOOKS",
        demanded=live_demanded(os.environ),
    )
    return named


@pytest.fixture
def allowed_logbook(raw_client: OlogClient) -> str:
    """A logbook that exists on this server AND is in the write allowlist (positive control)."""
    candidates = sorted(set(raw_client.list_logbooks()) & _allowlisted_logbooks())
    assert_live_available(
        bool(candidates),
        "no logbook in EPICS_MCP_OLOG_WRITE_LOGBOOKS exists on this Olog, so the positive control "
        "cannot run, without it a deny-all allowlist would look like a working gate",
        demanded=live_demanded(os.environ),
    )
    return candidates[0]


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    """A REAL attachment file with real bytes.

    Not a dummy path on purpose: the positive control has to reach the actual upload, and a
    non-existent file would make it fail before the write for a reason that has nothing to do with
    the gate, a control that can never distinguish the two outcomes it exists to distinguish.
    """
    file = tmp_path / "write-gate-probe.bob"
    file.write_bytes(b"<display version='2.0.0'><name>write-gate-probe</name></display>")
    return file


def _create_entry(client: OlogClient, logbook: str) -> str:
    """Create a labelled, attachment-free entry in *logbook* and return its id.

    No ``level`` is set: the value space is server-configured, and an unknown level is stored
    verbatim and then matches no filter, a needless way for a probe to leave a broken record.
    """
    entry = client.create_log_entry(
        title=f"{_ARTIFACT} {uuid.uuid4().hex[:8]}",
        logbooks=[logbook],
        description=_ARTIFACT,
    )
    return str(entry["id"])


# ======================================================================================
# The deny path, against the wire. Each test lays down its OWN target in the scratch deny logbook
# first (a real entry, Olog has no delete); the gated call itself then mutates nothing, because the
# refusal falls after a pure read.
# ======================================================================================


async def test_add_log_attachment_denies_a_logbook_the_server_reported(
    raw_client: OlogClient, denied_logbook: str, payload: Path
) -> None:
    """``olog_allowlist_miss`` on ``add_log_attachment``, keyed on data only the server can supply.

    The call passes a log **id** and a file, never a logbook name. The gate is keyed on the target
    entry's OWN logbooks, which exist in this process only because the live server returned them in
    ``GET /logs/{id}``. So the assertion that the refusal names ``denied_logbook`` is a statement
    about the real round-trip, not about a fixture: an in-memory row cannot make it.

    RED-PROOF (mutant): widen the allowlist to include this logbook (or delete branch 3 of
    ``OlogWriteGate.check_write_preconditions``) and the call proceeds to the attach instead of
    raising.
    """
    log_id = _create_entry(raw_client, denied_logbook)

    with pytest.raises(OlogWriteDeniedError) as excinfo:
        await query_olog_add_attachment(log_id, attachments=[str(payload)])

    # The gate's audited code: the allowlist denial fires AFTER the round-trip read, keyed on the
    # logbooks the server itself reported for this entry.
    assert excinfo.value.error_code == "OLOG_WRITE_DENIED"
    # Branch 3 and not branch 0/1/2: all four raise the same class with the same code.
    assert _ALLOWLIST_DENY_MESSAGE in str(excinfo.value)
    # ...and it names the logbook the SERVER reported for this entry.
    assert denied_logbook in str(excinfo.value)


async def test_update_log_entry_denies_a_logbook_the_server_reported(
    raw_client: OlogClient, denied_logbook: str
) -> None:
    """``olog_allowlist_miss`` on ``update_log_entry``, the same wire-sourced decision input.

    A second tool path, not a duplicate: ``update_log_entry`` gates on the UNION of the entry's
    current and resulting logbooks, so its decision input is assembled differently from
    ``add_log_attachment``'s even though both read it off the same response.

    RED-PROOF (mutant): as above, widen the allowlist and the update lands instead of raising.
    """
    log_id = _create_entry(raw_client, denied_logbook)

    with pytest.raises(OlogWriteDeniedError) as excinfo:
        await query_olog_update(log_id, title=f"{_ARTIFACT} edited")

    assert excinfo.value.error_code == "OLOG_WRITE_DENIED"
    assert _ALLOWLIST_DENY_MESSAGE in str(excinfo.value)
    assert denied_logbook in str(excinfo.value)


# ======================================================================================
# Positive controls: these DO write, to entries this module created in the allowlisted logbook.
# Without them a deny-all allowlist would be indistinguishable from a working gate.
# ======================================================================================


async def test_add_log_attachment_passes_the_gate_for_an_allowlisted_logbook(
    raw_client: OlogClient, allowed_logbook: str, payload: Path
) -> None:
    """The control for the attach deny: the identical call against an ALLOWLISTED entry succeeds.

    The discriminating field is the ATTACHMENT COUNT (0 → 1). ``owner`` is deliberately NOT
    compared: the corpus owner and the write service account are the same principal here, so the
    comparison would hold no matter what the server did.
    """
    log_id = _create_entry(raw_client, allowed_logbook)
    before = raw_client.get_raw_entry(log_id)
    assert before is not None
    assert not before.get("attachments"), "the control entry must start without attachments"

    result = await query_olog_add_attachment(log_id, attachments=[str(payload)])
    assert result["added"] is True

    after = raw_client.get_raw_entry(log_id)
    assert after is not None
    attachments = after.get("attachments")
    assert isinstance(attachments, list)
    assert len(attachments) == 1, "the write passed the gate but did not land"


async def test_update_log_entry_passes_the_gate_for_an_allowlisted_logbook(
    raw_client: OlogClient, allowed_logbook: str
) -> None:
    """The control for the update deny: the identical call against an ALLOWLISTED entry succeeds.

    The discriminating field is the TITLE: it is what the call changes, and it is read back off
    the server rather than off the tool's own return value.
    """
    log_id = _create_entry(raw_client, allowed_logbook)
    new_title = f"{_ARTIFACT} edited {uuid.uuid4().hex[:8]}"

    result = await query_olog_update(log_id, title=new_title)
    assert result["updated"] is True

    after = raw_client.get_raw_entry(log_id)
    assert after is not None
    assert after.get("title") == new_title, "the update passed the gate but did not land"
