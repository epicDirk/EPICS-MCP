"""Drift guard: the documented resource URIs must match the @mcp.resource registrations (M13).

The quality review found the README documented ``health://status`` while the server
actually registered ``epics-pv://health``. This standing check keeps the two in sync so
that drift can never silently return.

The resource table moved from README.md to docs/tools.md when the README was cut down to a
landing page, so the guard no longer hardcodes one file. It asserts two things instead, which
together are STRICTER than the single equality it replaced:

1. The page that OWNS the table lists every registered URI. A table quietly deleted from the
   owning page fails here even if some other page happens to mention a URI in passing, which a
   union-over-all-docs check would have accepted.
2. NO documentation file names a URI the server does not register. This is the original
   direction of the drift that was found, widened from the README to the whole doc surface.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# The page that owns the resource table. If it moves again, move this constant with it; the
# test then keeps working without being weakened.
_OWNING_DOC = _ROOT / "docs" / "tools.md"
_SERVER = _ROOT / "src" / "epics_pv_mcp" / "server.py"

_URI = re.compile(r"epics-pv://[a-z]+")
_RESOURCE = re.compile(r'@mcp\.resource\("(epics-pv://[a-z]+)"\)')


def _documentation_files() -> list[Path]:
    """Every user-facing markdown page, so an invented URI is caught wherever it is written."""
    top_level = ("README.md", "OPERATING.md", "SECURITY.md", "ARCHITECTURE.md", "CONTRIBUTING.md")
    pages = [_ROOT / name for name in top_level]
    pages.extend(sorted((_ROOT / "docs").glob("*.md")))
    return [p for p in pages if p.is_file()]


def test_owning_doc_lists_every_registered_resource_uri() -> None:
    server_uris = set(_RESOURCE.findall(_SERVER.read_text(encoding="utf-8")))
    assert server_uris, "no @mcp.resource registrations found, the test anchor broke"

    assert _OWNING_DOC.is_file(), f"the owning documentation page is missing: {_OWNING_DOC}"
    documented = set(_URI.findall(_OWNING_DOC.read_text(encoding="utf-8")))

    missing = server_uris - documented
    assert not missing, (
        f"{_OWNING_DOC.relative_to(_ROOT)} does not document {sorted(missing)}; "
        f"registered={sorted(server_uris)}"
    )


def test_no_documentation_invents_a_resource_uri() -> None:
    server_uris = set(_RESOURCE.findall(_SERVER.read_text(encoding="utf-8")))
    assert server_uris, "no @mcp.resource registrations found, the test anchor broke"

    invented: dict[str, list[str]] = {}
    for page in _documentation_files():
        extra = set(_URI.findall(page.read_text(encoding="utf-8"))) - server_uris
        if extra:
            invented[str(page.relative_to(_ROOT))] = sorted(extra)

    assert not invented, f"documentation names URIs the server does not register: {invented}"
