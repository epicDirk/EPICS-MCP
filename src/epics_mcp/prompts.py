"""MCP Prompts for the EPICS MCP server."""

from __future__ import annotations

from collections.abc import Mapping

from epics_mcp.display_files import is_inventory_file
from epics_mcp.presets import Preset


def diagnose_pv(pv_name: str) -> str:
    """Step-by-step PV diagnosis workflow."""
    return (
        f"Diagnose EPICS PV: {pv_name}\n\n"
        "Follow these steps:\n"
        f'1. get_pv_info("{pv_name}"), check connection state, data type, alarm status\n'
        f'2. get_pv_value("{pv_name}"), read current value\n'
        f'3. monitor_pv("{pv_name}", duration=5), watch for value changes over 5 seconds\n'
        "\n"
        "Report:\n"
        "- Connection status (connected/disconnected/timeout)\n"
        "- Current value and data type\n"
        "- Alarm severity and status\n"
        "- Update rate (events/second from monitor)\n"
        "- Recommended actions if issues found"
    )


def compare_machine_state(
    pv_prefix: str, reference_file: str = "", *, display_tools_available: bool
) -> str:
    """Compare current machine state to expected values.

    ``display_tools_available`` is keyword-only and REQUIRED (no default): the caller MUST state
    whether the display-gated ``validate_pvs`` tool is registered (S26/N05). A core-only install
    (no ``displays`` group) must not be told to call ``validate_pvs``, that would be an
    impossible plan. A default here would fail OPEN: if the server wrapper forgot to thread the
    real capability, the prompt would silently re-instruct the missing tool. Required → a mis-wired
    wrapper is a loud TypeError in a test, not a silent regression.
    """
    if reference_file and is_inventory_file(reference_file) and display_tools_available:
        # S1-3: pass the dataset ROOT as displays_dir too, without it validate_pvs walks the
        # file's own directory and under-resolves embedded fragments (consistent with the tool's
        # own description).
        # GB-4: name the view as well. Comparing machine state against a reference SCREEN wants
        # everything that screen shows, and the tool's default is the other question (what the file
        # declares). On a screen that only composes fragments the default answers total 0, so a
        # prompt that omits the view teaches a call which silently finds nothing.
        #
        # GB-79: this branch is reached by a .plt Data Browser trend as well, and the advice holds
        # unchanged there. A trend embedded in a screen answers under the file view, one opened by
        # a button answers under the display view, and asking for the display view costs nothing in
        # the first case, so naming one view keeps the prompt short without being wrong.
        file_note = (
            f'\n1. Extract PVs from "{reference_file}" using '
            f'validate_pvs(file_path="{reference_file}", displays_dir="<dataset ROOT>", '
            'view="display") '
            "(displays_dir = the project ROOT; without it embedded fragments under-resolve. "
            'view="display" asks what the screen shows, fragments included; the default "file" '
            "asks only what this file declares itself)\n"
        )
    elif reference_file and display_tools_available:
        # QA-33: the tool REFUSES a file_path it cannot read outright (INVALID_INPUT), where it
        # used to answer an empty result. Naming it for, say, a CSV would teach a call that is
        # certain to fail, and this prompt has been that surface once before (the pv_names
        # rename). The display tools exist here, they just do not parse this KIND of file.
        #
        # GB-79 narrowed which kinds land here: a .plt trend now takes the branch above, so this
        # one is for everything the inventory still does not collect. The sentence below names
        # both readable kinds rather than only .bob, because a client told "displays only" would
        # not think to retry with the trend it also has.
        file_note = (
            f'\n1. Read "{reference_file}" yourself and collect the PV names it references '
            "(or ask the user for the PV list); validate_pvs reads .bob displays and .plt Data "
            "Browser trends only, and refuses any other file\n"
        )
    elif reference_file:
        # Core-only: no MCP tool parses a display or trend file here (the parsing tool is
        # display-gated and not registered). Do NOT name an unavailable tool, tell the client to
        # read the file itself.
        file_note = (
            f'\n1. Read "{reference_file}" yourself and collect the PV names it references '
            "(or ask the user for the PV list), no MCP tool parses a display or trend file in "
            "this core-only install\n"
        )
    else:
        file_note = (
            f'\n1. Collect PVs with prefix "{pv_prefix}" '
            "; ask the user for the PV list, a .bob display or a .plt trend\n"
        )

    return (
        f"Compare machine state for: {pv_prefix}\n\n"
        "Follow these steps:"
        f"{file_note}"
        "2. Read all current values with get_pvs(pv_names=[...])\n"
        "3. Compare to expected/nominal values\n"
        "4. Report deviations with severity:\n"
        "   - CRITICAL: Alarm severity > 0 or value out of range\n"
        "   - WARNING: Value changed but within limits\n"
        "   - OK: Value matches expected"
    )


def setup_epics_mcp(*, presets: Mapping[str, Preset]) -> str:
    """Walk a newcomer through configuring this server, conversationally.

    The same ground ``epics-init`` covers, for the reader who would rather be asked than read
    ``docs/deployment.md``. It deliberately ends at a COMMAND rather than at a block of variables:
    the assistant's job here is to work out which shape the user has, and ``epics-init`` already
    knows what each shape needs. Emitting the variables from inside a prompt would be a second,
    unguarded copy of the preset table.

    ``presets`` is keyword-only and REQUIRED (no default), the same posture as
    ``compare_machine_state``'s ``display_tools_available`` and for the same reason: the caller
    must state what actually exists rather than let this function assume. A default would fail
    OPEN. If the server wrapper stopped threading the real table, the prompt would keep naming
    whatever set was frozen here, and a preset added later would silently never be offered.
    Required means a mis-wired wrapper is a loud TypeError in a test instead.
    """
    catalogue = "\n".join(f"   - {name}: {preset.summary}" for name, preset in presets.items())
    return (
        "Help me configure the EPICS MCP server for my facility.\n\n"
        "Ask me about each service plane in turn, and do NOT guess: an unset URL disables its "
        "plane with no network call, so leaving one out is a valid answer, not a gap. One "
        "exception: an unset EPICS_MCP_ARCHIVER_RETRIEVAL_URL falls back to the mgmt URL instead "
        "of switching retrieval off.\n"
        "1. Live PVs: which host do my IOCs or my gateway answer on? PVA or CA?\n"
        "2. ChannelFinder: do I have a channel registry, and at which URL?\n"
        "3. Archiver Appliance: mgmt URL, and does retrieval run on its own port?\n"
        "4. Alarm Logger: do I have a Phoebus alarm logger?\n"
        "5. Naming Service: do I have one?\n"
        "6. Olog: do I have a logbook?\n"
        "7. Internal CA: does any of the above use HTTPS with a private root certificate?\n\n"
        "Three things NEVER belong in what you tell me to run, however convenient they look:\n"
        "   - A user name or password inside any of the *_URL values above. A URL accepts "
        "https://user:password@host/path and nothing rejects it, but the three service URLs are "
        "reported by the epics-pv://config resource, which the client keeps: that resource strips "
        "a userinfo before printing, and a credential still has no business being there. Where a "
        "plane needs authentication, the answer is its EPICS_MCP_*_AUTH header variable "
        "(ChannelFinder, Archiver, Alarm and Olog have one; the Naming plane has none).\n"
        "   - EPICS_MCP_ALLOW_PV_WRITE or EPICS_MCP_ALLOW_OLOG_WRITE. Arming a write gate is a "
        "decision I make knowingly, never a step in a setup. The check below does print the "
        "resulting posture in its 'Write gates' block, but it does not say whether such a server "
        "would even start. If I ask for writes, send me to docs/safety.md first.\n"
        "   - EPICS_MCP_TLS_VERIFY=false as the answer to question 7. The answer to an internal CA "
        "is EPICS_MCP_CA_BUNDLE pointing at a bundle that combines it with the public roots; "
        "turning verification off is a last resort on a trusted network, and mine to choose.\n\n"
        "Then match my answers to the closest of these shapes:\n"
        f"{catalogue}\n\n"
        "Then tell me to run, in a terminal (these are commands for ME, not tools you can call):\n"
        "   epics-init --preset <the shape you picked> --set NAME=VALUE ... --out <my config> "
        "--probe-pv <a PV name I gave you>\n"
        "Include --probe-pv whenever I have named a PV, for the reason at the end of this "
        "message; leave it off only if I have none, and then say why the report is quieter.\n"
        "Use --out rather than telling me to redirect with '>': a redirect writes whatever "
        "encoding my shell prefers, and in Windows PowerShell 5.1 that is bytes "
        "a strict JSON parser rejects. If the file already exists the command refuses instead of "
        "overwriting, because "
        "it probably holds my other MCP servers; then have me write a new file and merge the block "
        "by hand. Add --absolute-command when my client is a desktop application, which usually "
        "cannot resolve a bare command name and reports nothing but a server that did not start.\n"
        "After the file is in place, remind me to RESTART or reconnect the client. It reads that "
        "file when it starts, so until then the tools are absent and nothing looks wrong.\n\n"
        "It runs epics-doctor against the result and prints what each plane answered, plus a "
        "'Write gates' block saying what each write gate would allow and where. Read that report "
        "back with me, and treat these three as findings rather than noise:\n"
        "   - '?' unverified: it answered, but could not prove what it is\n"
        "   - '!' identity probe failed: reachable, but the probe got a 401/404/redirect\n"
        "   - '~' no ingest: it proved what it is and is not doing its job\n"
        "Note that without --probe-pv nothing reads a live PV, so a clean report on a "
        "PV-only setup confirms that nothing is misconfigured, not that anything works."
    )
