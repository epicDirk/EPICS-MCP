"""Where an answer came FROM, attached to the answer itself (GB-64).

The problem this closes, measured rather than supposed: a statement about which world this
server reads from could be made without anything having measured it. The reach IS computed
here, and it is even on the wire, but only on two channels a model cannot use:

* ``epics-doctor`` is a console script (``[project.scripts]``), not a tool. The server header
  tells every client to "run epics-doctor to see what this instance actually reaches", and a
  client with no shell cannot follow that instruction at all. None of the registered tools
  reports the reach either, ``diagnose_connection`` included.
* ``epics-pv://health`` carries the address-free ``pv_search`` block, but a resource is
  application-controlled. This repository already drew that conclusion once, for the operator
  guide, and turned it into the ``get_guide`` TOOL for exactly this reason.

So the fix is a third channel that needs no cooperation: the field rides along on the answer.

⛔ ON EVERY READ ANSWER, NEVER ONLY THE FIRST, and the third reason is the one that decides.
A once-per-session field cannot be CHECKED: the reader would have to believe that the lane of
the first answer still holds for the fifth, which reproduces the exact defect this module
exists to remove (a claim about reach, asserted rather than measured). The other two: a
"already announced" flag is hidden process state, so the same call would return different
content depending on when it is made, against the determinism rule this project states; and a
field that appears once scrolls out of the reader's view in a long session.

⛔ ONCE PER TOOL ANSWER, NEVER PER VALUE INSIDE IT. Measured on this repository's own
``_LOOPBACK_SEARCH`` preset: attaching to each entry of a 100-PV batch doubles the payload,
attaching once to the envelope costs a fraction of a percent. It is also the only correct
place semantically: the reach is a property of the PROCESS, identical for every value in one
answer, so per-value copies would be the same fact repeated up to ``max_batch_size`` times.

⛔ ADDRESS-FREE, and that is a standing rule of this repository rather than a preference.
``write_posture.PvWriteGateReport.search_reach_violations`` states it: "The strings name RAW
hosts and RAW environment values, so this field is for an operator's own terminal. Anything
shipping to a client projects the posture field by field and leaves this one out." The full,
host-naming rendering stays with ``epics-doctor``; what ships is the CLASSIFICATION, which is
the distinction an operator actually needs (a local test rig against a real facility) and
costs a fraction of the bytes. The same rule settles the two planes whose URL is deliberately
withheld from ``epics-pv://config`` (naming and olog, "never the URL, an ESS host"): they are
classified like every other plane, so nothing is disclosed that was not disclosed before, and
nothing is hidden that the caller needs.

⚠️ WHAT THIS FIELD IS NOT. It reports the CONFIGURATION, not a probe: nothing here opens a
socket, so ``probed`` is always False and the field cannot time out or slow an answer down.
A configured plane can still be down, and a plane classified ``beyond-loopback`` is not
thereby proven to be a production facility. What a probe would add is ``epics-doctor``'s job,
and the tool descriptions and the operator guide say so; repeating it in every answer would
spend on prose the bytes this shape exists to save.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, TypedDict, cast

from epics_mcp.config import EpicsConfig, get_config
from epics_mcp.errors import EpicsError
from epics_mcp.services._http import is_loopback_url
from epics_mcp.write_posture import pv_search_posture

#: The service planes an answer can come from. Same names the doctor's report prints, so an
#: operator reading both sees one vocabulary rather than two.
Plane = Literal[
    "live-pv",
    "channelfinder",
    "archiver",
    "archiver-retrieval",
    "alarm",
    "naming",
    "olog",
]

#: How far the plane that answered can reach.
#:
#: * ``not-configured``: the plane has no URL, so no request left this process.
#: * ``loopback-only``: provably confined to this machine (a local test rig).
#: * ``beyond-loopback``: it can leave this machine. NOT the same as "a production facility",
#:   and deliberately not sharpened: this process cannot tell a staging host from a real one.
#:
#: There is no ``unknown``. Every input resolves into one of the three, and an input that
#: cannot be PROVEN local resolves to ``beyond-loopback``, never to a fourth "we did not look"
#: value that a reader would have to interpret. See :func:`_scope_of_url` for why that
#: direction is the safe one.
Scope = Literal["not-configured", "loopback-only", "beyond-loopback"]

#: The wire key. Short on purpose: it rides on every read answer.
REACH_KEY = "reach"


# ⚠️ THE DOCSTRING BELOW IS ONE LINE, AND THAT IS A MEASURED CONSTRAINT RATHER THAN A STYLE.
# FastMCP embeds a TypedDict's docstring into the outputSchema of every tool that returns a shape
# carrying it, so this one ships EIGHTEEN times in ``tools/list``. Measured: the reasoning that
# now stands in this comment cost 14 904 chars there, a 17.9 percent growth of the whole payload,
# for text no caller needs at the point of use. A comment reaches the next author, which is who
# it is for; a docstring reaches every client on every listing.
# ⚠️ That count said TWELVE until the post-build review counted it: 14 904 / 18 is 828, the length
# of the docstring it replaced, which is the arithmetic that settles it. Re-count rather than
# trusting this line: the tools carrying the shape are the ones whose outputSchema has a
# ``reach`` property, and there is no list of them anywhere that could stay in step by itself.
#
# WHY ``planes`` IS A MAPPING rather than a single value: a cross-plane tool answers from several
# at once, and one shape for both cases beats two shapes that drift apart. A single-plane tool
# simply has one entry.
#
# WHY ``probed`` IS ON THE WIRE while it is a constant False: it is the difference between this
# field and the defect it removes. Without it a caller reading ``beyond-loopback`` cannot tell a
# measured statement from a configured one, which is precisely the confusion that produced GB-64.
# The day a probing variant exists, the key is already there to carry True.
class Reach(TypedDict):
    """Which planes answered, how far each reaches, and whether anything probed."""

    planes: dict[str, str]
    probed: bool


def _scope_of_url(url: str) -> Scope:
    """Classify one REST plane from its configured URL, without resolving anything.

    ⛔ THROUGH :func:`~epics_mcp.services._http.is_loopback_url`, NEVER through a parser of its
    own, and that is a correction paid for by an adversarial review of the first version. This
    function used to call ``urllib.parse.urlsplit``, which is the parser ``services/_http.py``
    already REJECTED for exactly this decision, naming exactly this URL as its reason:
    ``http://evil.example.org:8080\\@127.0.0.1/Olog``. urlparse splits at the last ``@`` and
    answers ``127.0.0.1``; urllib3, the parser ``requests`` actually connects through, answers
    ``evil.example.org``. Measured on the first version: the field reported ``loopback-only``
    for a configuration whose socket goes to a remote host. That is not a rounding error, it is
    the false all-clear this whole module exists to remove, produced by the module that removes
    it. One question, one parser.

    Fail-closed in the direction that cannot mislead: only a host the shared primitive can PROVE
    to be loopback is called ``loopback-only``. A name is never resolved, so
    ``host.docker.internal`` and every FQDN land in ``beyond-loopback`` even where DNS would map
    them to 127.0.0.1, and an unparseable URL lands there too, because a value nothing can read
    is not evidence of confinement. The two errors are not symmetric: claiming isolation this
    process does not have is the defect above, while over-reporting reach costs an operator one
    look at ``epics-doctor``.

    ``is_loopback_url`` collapses "parsed, not loopback" and "did not parse" into one False,
    which its own docstring names as a limitation for the write gate. Here that collapse is
    exactly right: both are "not provably local", which is one answer, not two.
    """
    if not url:
        return "not-configured"
    return "loopback-only" if is_loopback_url(url) else "beyond-loopback"


def _scope_of_live(environ: Mapping[str, str]) -> Scope:
    """Classify the live PV plane from the EPICS client search environment.

    Delegates to :func:`~epics_mcp.write_posture.pv_search_posture`, the one parser of those
    variables, rather than reading them again here: its ``loopback_only`` is already the
    fail-closed answer (an unset ``*_AUTO_ADDR_LIST`` means the subnet broadcast is ON, so a
    null environment is NOT confined), and it already governs the write gate's boot assert. A
    second reading of the same variables is how two answers to one question start to disagree.

    The live plane has no ``not-configured`` state: a PV search always has SOME reach, even
    with every variable unset, which is the trap the shared parser exists to name.
    """
    return "loopback-only" if pv_search_posture(environ).loopback_only else "beyond-loopback"


def scope_of(plane: Plane, cfg: EpicsConfig, environ: Mapping[str, str]) -> Scope:
    """The reach of one plane, from configuration alone. No socket, no resolver, no clock.

    ``cfg`` and ``environ`` are parameters rather than module lookups so the answer is a pure
    function of its arguments and a test can ask the question without mutating process state,
    the same posture :func:`~epics_mcp.write_posture.pv_search_posture` takes.

    ⛔ THE ARCHIVER IS TWO PLANES HERE, and collapsing them into one was a defect in the first
    version rather than a simplification. ``get_pv_history`` connects to
    ``archiver_retrieval_url`` when it is set (``tools/archiver.py`` passes it into the client),
    while every other archiver tool gates on the mgmt URL. In a split deployment those are
    separate Tomcats and may be separate HOSTS, so a single classification taken from the mgmt
    URL named the wrong world for exactly the tool that fetches the data. The retrieval row
    resolves ``archiver_retrieval_url or archiver_url``, the same fallback
    ``services/doctor.py`` and ``services/doctor_crosscut.py`` already spell, because a
    single-JVM appliance legitimately leaves the retrieval variable empty.
    """
    if plane == "live-pv":
        return _scope_of_live(environ)
    urls: dict[Plane, str] = {
        "channelfinder": cfg.channelfinder_url,
        "archiver": cfg.archiver_url,
        "archiver-retrieval": cfg.archiver_retrieval_url or cfg.archiver_url,
        "alarm": cfg.alarm_url,
        "naming": cfg.naming_url,
        "olog": cfg.olog_url,
    }
    return _scope_of_url(urls[plane])


def reach_of(
    *planes: Plane,
    cfg: EpicsConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> Reach:
    """The reach field for an answer that came from *planes*.

    Both keyword arguments default to the live process (``get_config()`` / ``os.environ``),
    which is what a tool wants, while a test passes its own and needs no monkeypatching.
    """
    resolved_cfg = get_config() if cfg is None else cfg
    resolved_environ = os.environ if environ is None else environ
    return {
        "planes": {p: scope_of(p, resolved_cfg, resolved_environ) for p in planes},
        "probed": False,
    }


def _annotate_empty(
    answer: dict[str, object], planes: tuple[Plane, ...], empty_key: str | None
) -> None:
    """Add :func:`empty_note` when *answer*'s payload list is empty and the plane WAS asked.

    Four conditions, and each one is a case that would otherwise produce a wrong sentence:

    * ``empty_key`` is opt-in per tool, because only the tool knows which of its keys carries
      the payload. A decorator guessing "the first list" would annotate the wrong field the day
      a shape gains a second one.
    * A single plane only. A cross-plane tool's empty result has no single cause, and naming one
      of its planes would be a guess dressed as a finding.
    * An existing ``note`` is never replaced. A tool that knows a SPECIFIC cause (an unknown Olog
      level, an anchored ChannelFinder glob, an unconfigured plane) has said something this
      function cannot improve on, and overwriting it would trade a precise answer for a general
      one.
    ``not-configured`` needs no fourth condition, and the absence is measured rather than
    assumed: a mutation removing the early return that used to sit here left every test green,
    because every site that has an unconfigured plane already sets its own note and the
    never-replace rule catches it first. Untested defensive code is the thing this repository
    says a guard must not be, so it is gone and :func:`empty_note` answers that case itself.
    """
    if empty_key is None or len(planes) != 1 or answer.get(NOTE_KEY):
        return
    payload = answer.get(empty_key)
    if not isinstance(payload, list) or payload:
        return
    answer[NOTE_KEY] = empty_note(planes[0], scope_of(planes[0], get_config(), os.environ))


def _inline(reach: Reach) -> str:
    """The reach as one short clause, for an error message that has no payload to carry a field.

    Deliberately NOT a second rendering of the field: it is the same mapping, spelled flat, so
    there is nothing here that could disagree with the structured form. ``not probed`` is spelled
    out rather than shown as ``probed=false``, because an error message is read by a human as
    often as by a client.
    """
    planes = ", ".join(f"{name}={scope}" for name, scope in reach["planes"].items())
    return f"{planes}, not probed"


#: The wire key for the honest-empty note. Same key the per-plane "not configured" stubs already
#: use, so a caller reads ONE field for "why is this thin", never two.
NOTE_KEY = "note"


def empty_note(plane: Plane, scope: Scope) -> str:
    """Why an empty answer from *plane* is empty, said in the one sentence a caller needs.

    ⛔ THE GAP THIS CLOSES, measured rather than supposed. The ``note`` pattern in this server
    fires on ONE cause only: a plane with no URL. Every site does it
    (``tools/archiver.py``, ``services/checkers.py``, ``services/checkers_olog.py``), all of them
    behind ``if not cfg.<x>_url``. So the two cases a reader most needs to tell apart looked
    IDENTICAL on the wire: a plane that was never asked, and a plane that was asked and found
    nothing. Both arrived as an empty list. A reader could not tell a misconfiguration from a
    real zero, which is the same class of ambiguity GB-64 removed for the reach.

    The sentence names the plane and its scope, because a zero from a plane confined to this
    machine and a zero from a plane that reaches a facility mean different things: the first is
    probably an empty test rig, the second is probably a real absence. It says nothing about
    WHICH filter narrowed the result, because this function cannot know; the tools that DO know
    carry their own, more specific note, and :func:`with_reach` never replaces one.
    """
    if scope == "not-configured":
        # Reachable: a tool whose plane is unconfigured and which sets no note of its own lands
        # here, and the honest answer is that nothing was asked at all.
        return f"The {plane} plane is not configured, so nothing was asked."
    reached = (
        "confined to this machine" if scope == "loopback-only" else "able to leave this machine"
    )
    return (
        f"Not a missing plane: the {plane} plane is configured, was queried and matched nothing. "
        f"Its reach is {scope} ({reached}), so this is an empty answer from the world named in "
        f"'reach', not a plane that was never asked."
    )


def with_reach[**P, R](
    *planes: Plane,
    empty_key: str | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Attach :func:`reach_of` to a read tool's answer, once, under :data:`REACH_KEY`.

    A sibling of :func:`~epics_mcp.tool_errors.translate_epics_errors`, and deliberately the
    same shape: one decorator, one place where the rule lives, and each tool body stays a
    single ``return await _impl(...)``.

    ⚠️ It decorates the TOOL, never the value formatter underneath. Two measured reasons:
    ``_format_value`` runs once per PV, so putting it there would repeat one process-level fact
    up to ``max_batch_size`` times; and ``tests/test_epics_client.py`` pins that a minimal
    scalar omits every absent block, an invariant a formatter that always adds a key would
    break.

    An answer that already carries the key keeps its own: a tool that computes a finer reach
    itself stays the authority on its own answer, and the decorator never overwrites it. Silent
    overwriting would make the general rule quietly wrong for the specific case, which is the
    harder bug to find.

    ⚠️ HONEST LIMIT, found by the post-build review and stated rather than dressed up: that
    clause has NO production caller today. Every tool computing its own reach (``discover_pvs``,
    the four display tools, ``diagnose_connection``) does so without this decorator, so nothing
    in ``src`` currently exercises the branch and only
    ``test_the_decorator_does_not_overwrite_a_tool_that_knows_better`` proves it works. It stays
    because the collision it prevents is silent when it happens, not because it happens today.
    """

    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = await fn(*args, **kwargs)
            except EpicsError as exc:
                # ⛔ THE FAILING READ IS THE ONE THAT NEEDS THIS MOST, and the first version left
                # it out. A read that raises has no answer to carry a field, so a PV that does
                # not connect, i.e. exactly the case a diagnosis starts from, arrived with no
                # statement about which world failed to answer. Worse, the shipped prompt tells
                # the reader to read the reach of the first answer, and on that path there is no
                # first answer: the advice was unfollowable in the situation it was written for.
                # Found by the post-build review.
                #
                # Appended to the MESSAGE rather than returned as a field because an error has no
                # payload here; ``translate_epics_errors`` sits outside this decorator and turns
                # the raised EpicsError into the client-facing text, so the clause travels with
                # the code the caller already reads. Short on purpose, and address-free like the
                # field.
                raise EpicsError(
                    f"{exc} [reach: {_inline(reach_of(*planes))}]",
                    error_code=exc.error_code,
                    details=exc.details,
                ) from exc
            # ⚠️ The ONE cast in this module, and it is deliberate rather than a shortcut. The
            # return type has to be passed through UNCHANGED, because FastMCP builds each tool's
            # advertised outputSchema from that annotation, and collapsing it to
            # ``dict[str, object]`` here would silently un-type every typed tool this decorator
            # touches. mypy cannot see that a TypedDict is a mutable string-keyed mapping at
            # runtime (key-type invariance), so the assignment needs the cast even though it is
            # exactly what the shapes declare: every decorated tool's TypedDict carries a
            # ``reach`` field, which is what makes the write legal in the first place.
            writable = cast(dict[str, object], result)
            if REACH_KEY not in writable:
                writable[REACH_KEY] = reach_of(*planes)
            _annotate_empty(writable, planes, empty_key)
            return result

        return wrapper

    return decorate
