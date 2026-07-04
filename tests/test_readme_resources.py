"""Drift guard: README resource URIs must match the @mcp.resource registrations (M13).

The quality review found the README documented ``health://status`` while the server
actually registered ``epics-pv://health``. This standing check keeps the two in sync so
that drift can never silently return.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_README = _ROOT / "README.md"
_SERVER = _ROOT / "src" / "epics_pv_mcp" / "server.py"

_URI = re.compile(r"epics-pv://[a-z]+")
_RESOURCE = re.compile(r'@mcp\.resource\("(epics-pv://[a-z]+)"\)')


def test_readme_resource_uris_match_server() -> None:
    server_uris = set(_RESOURCE.findall(_SERVER.read_text(encoding="utf-8")))
    assert server_uris, "no @mcp.resource registrations found — the test anchor broke"

    readme_uris = set(_URI.findall(_README.read_text(encoding="utf-8")))

    assert readme_uris == server_uris, (
        "README ↔ server resource-URI drift: "
        f"readme={sorted(readme_uris)} server={sorted(server_uris)}"
    )
