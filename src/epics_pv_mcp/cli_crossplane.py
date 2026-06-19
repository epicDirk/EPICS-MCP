"""CLI for the cross-plane PV provenance check (Display ↔ e3 IOC ↔ Naming).

Reads a directory of ``.bob`` displays and an e3 ``st.cmd`` (both local files), joins
them, and writes a Markdown provenance report to stdout. The live ESS Naming Service is
queried only with ``--naming`` (a read-only GET); without it the check is fully offline.

Usage::

    python -m epics_pv_mcp.cli_crossplane --displays <bob-dir> --st-cmd <st.cmd> [--naming]
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from epics_pv_mcp.services.bob_pvs import extract_pvs_from_dir
from epics_pv_mcp.services.crossplane import crossplane_check, render_markdown
from epics_pv_mcp.services.e3_db import parse_st_cmd
from epics_pv_mcp.services.naming_client import NamingServiceClient


def main(argv: list[str] | None = None) -> int:
    """Run the cross-plane check and print a Markdown report. Returns an exit code."""
    parser = argparse.ArgumentParser(description="Cross-plane PV provenance: Display ↔ e3 ↔ Naming")
    parser.add_argument("--displays", required=True, type=Path, help="directory of .bob displays")
    parser.add_argument("--st-cmd", required=True, type=Path, help="e3 IOC st.cmd file")
    parser.add_argument(
        "--naming",
        action="store_true",
        help="query the live ESS Naming Service (read-only GET); omit to stay offline",
    )
    args = parser.parse_args(argv)

    # The report contains Unicode (emoji/en-dash); force UTF-8 so a cp1252 Windows
    # console doesn't crash on encode.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    if not Path(args.st_cmd).is_file():
        sys.stderr.write(f"Error: st.cmd file not found: {args.st_cmd}\n")
        return 2
    if not Path(args.displays).is_dir():
        sys.stderr.write(f"Error: displays directory not found: {args.displays}\n")
        return 2

    display_pvs = extract_pvs_from_dir(args.displays)
    st_info = parse_st_cmd(Path(args.st_cmd).read_text(encoding="utf-8"))
    naming = NamingServiceClient() if args.naming else None
    report = crossplane_check(display_pvs, st_info, naming=naming)
    sys.stdout.write(render_markdown(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
