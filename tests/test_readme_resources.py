"""Drift guard: the documented resource URIs must match the @mcp.resource registrations (M13).

The quality review found the README documented ``health://status`` while the server
actually registered ``epics-pv://health``. This standing check keeps the two in sync so
that drift can never silently return.

The resource table moved from README.md to docs/tools.md when the README was cut down to a
landing page, so the guard no longer hardcodes one file. It asserts three things, which
together are STRICTER than the single equality it replaced:

1. The page that OWNS the table lists every registered URI. A table quietly deleted from the
   owning page fails here even if some other page happens to mention a URI in passing, which a
   union-over-all-docs check would have accepted.
2. NO documentation file names a URI the server does not register. This is the original
   direction of the drift that was found, widened from the README to the whole doc surface.
3. NO shipped source file does either. Most of the URI mentions in this package are in tool
   DOCSTRINGS, which travel to a client over ``tools/list``, so a stale one there reaches
   further than a stale one on a page nobody has open.

⚠ **NOTHING HERE SPELLS A SCHEME, AND THAT IS THE POINT.** Until 2026-08-23 both patterns wrote
``epics-pv://`` literally. Renaming the scheme, which is what happened for [QA-26], would have
dragged the guard along and left it looking ONLY for the new one, so a forgotten mention of the
old one would have been invisible to the very test written to catch exactly that drift. Measured
on this tree: with the literal patterns merely swapped, a deliberately planted ``epics-pv://guide``
in ``docs/tools.md`` left both tests green. The permitted set is therefore READ from the
registrations, and everything else with a ``://`` in it has to be declared below.

The foreign-scheme list is CLOSED rather than open, and that is a deliberate trade. An unknown
scheme on a page or in a module of this repository is far more often a leftover from a rename than
a new kind of link, so an unexpected one is reported and has to be added here on purpose. The cost
is one line when a genuinely new scheme arrives; the benefit is that a retired scheme cannot come
back quietly and that an INVENTED one, the defect this file was written for, is caught as well. A
list of retired schemes would have caught only the first of those two.

The honest limits, measured against this helper rather than reasoned about, and every one of them
INHERITED from the literal pattern this replaces rather than introduced by it:

* ``tests/`` is out of scope. Roughly twenty mentions live there and they all moved with the
  rename, but a stale URI in a test is not a contract defect, and pointing the guard at its own
  directory would trade real coverage for noise.
* A path of more than one segment is not read: ``epics://guide/topic`` matches as the registered
  ``epics://guide`` and passes. Widening the path characters to include ``/`` would ALSO have to
  decide what a sentence-ending period is, and five mentions in this tree are ``epics://guide.``,
  so that trade closes an empty hole and opens five false reports. Measured: no file in this tree
  carries a multi-segment resource URI.
* A scheme with no path at all (``epics-pv://`` on its own) and an uppercase one are not matched.
  Measured: the only pathless mention in the tree is the CHANGELOG line describing this rename,
  and there is no uppercase one.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# The page that owns the resource table. If it moves again, move this constant with it; the
# test then keeps working without being weakened.
_OWNING_DOC = _ROOT / "docs" / "tools.md"
_SERVER = _ROOT / "src" / "epics_mcp" / "server.py"

# A URI scheme as RFC 3986 writes one, lowercase because everything in this repository is.
_SCHEME = r"[a-z][a-z0-9+.-]*"
_RESOURCE = re.compile(rf'@mcp\.resource\("({_SCHEME}://[a-z]+)"\)')
# The lookbehind keeps a scheme from starting in the middle of a word, so `git+https://` is read
# as one scheme rather than as a stray `https`.
_ANY_URI = re.compile(rf"(?<![a-z0-9+.-])({_SCHEME})://[a-z]+")

#: Schemes a page or module of this repository may name although the server registers no resource
#: under them. Each earns its place by being something other than a resource URI:
#: ``http``/``https`` are ordinary links, ``git+https`` is the dependency-group reference the
#: README explains, ``scheme`` is the placeholder two comments about URL parsing use to stand for
#: any scheme at all, and ``ca``/``pva``/``loc``/``sim`` are the EPICS and Phoebus CHANNEL
#: prefixes, which are addresses of a completely different kind that happen to share the syntax.
#:
#: ⚠ The channel prefixes are not hypothetical and were not here at first. Measured on this tree,
#: four lines inside the checked surface already write them (``services/device_lookup.py``,
#: ``services/inventory_adapter.py``, ``tools/validate.py`` twice) and every one of them passes
#: only because the next character happens to be a backtick, a period or an uppercase letter.
#: The first author to write ``ca://motor`` in lowercase prose would have reddened this guard for
#: nothing, which is the failure mode that gets a guard switched off rather than fixed.
_FOREIGN_SCHEMES = frozenset({"http", "https", "git+https", "scheme", "ca", "pva", "loc", "sim"})


def _registered_uris() -> set[str]:
    """The resource URIs the server actually registers, scheme included, read from the source."""
    return set(_RESOURCE.findall(_SERVER.read_text(encoding="utf-8")))


def _uris_a_page_may_not_name(text: str, registered: set[str]) -> list[str]:
    """Every URI in ``text`` that is neither registered here nor an ordinary foreign link.

    Pure and text-only so both directions of the red proof can be stated as data rather than as a
    file that has to be planted and cleaned up again.
    """
    offending = {
        match.group(0)
        for match in _ANY_URI.finditer(text)
        if match.group(1) not in _FOREIGN_SCHEMES and match.group(0) not in registered
    }
    return sorted(offending)


def _documentation_files() -> list[Path]:
    """Every user-facing page, so an invented URI is caught wherever it is written.

    ``CLAUDE.md`` is in the list although it is development surface and stays out of the sdist: a
    wrong URI in the instructions an agent reads misleads a writer, which is how the drift this
    file guards against gets INTO the pages in the first place. ``operator_guide.md`` is in it
    because it is the guide that ships in the wheel, markdown that happens to live under ``src/``.
    """
    top_level = (
        "README.md",
        "OPERATING.md",
        "SECURITY.md",
        "ARCHITECTURE.md",
        "CONTRIBUTING.md",
        "CLAUDE.md",
    )
    pages = [_ROOT / name for name in top_level]
    pages.extend(sorted((_ROOT / "docs").rglob("*.md")))
    pages.append(_ROOT / "examples" / "README.md")
    pages.append(_ROOT / "src" / "epics_mcp" / "operator_guide.md")
    # Not a page but a shipped one: pyproject lists .env.example in the sdist, and it names a
    # resource URI in its credentials note. It was the ONE file the work item's own search recipe
    # missed, because three --include suffixes cannot see a file that has none.
    pages.append(_ROOT / ".env.example")
    return [p for p in pages if p.is_file()]


def _source_files() -> list[Path]:
    """The shipped modules. Their docstrings are the widest surface a stale URI can reach."""
    return sorted((_ROOT / "src").rglob("*.py"))


def test_owning_doc_lists_every_registered_resource_uri() -> None:
    server_uris = _registered_uris()
    assert server_uris, "no @mcp.resource registrations found, the test anchor broke"

    assert _OWNING_DOC.is_file(), f"the owning documentation page is missing: {_OWNING_DOC}"
    documented = {
        match.group(0) for match in _ANY_URI.finditer(_OWNING_DOC.read_text(encoding="utf-8"))
    }

    missing = server_uris - documented
    assert not missing, (
        f"{_OWNING_DOC.relative_to(_ROOT)} does not document {sorted(missing)}; "
        f"registered={sorted(server_uris)}"
    )


def test_no_documentation_invents_a_resource_uri() -> None:
    server_uris = _registered_uris()
    assert server_uris, "no @mcp.resource registrations found, the test anchor broke"

    invented: dict[str, list[str]] = {}
    for page in _documentation_files():
        extra = _uris_a_page_may_not_name(page.read_text(encoding="utf-8"), server_uris)
        if extra:
            invented[str(page.relative_to(_ROOT))] = extra

    assert not invented, (
        f"documentation names URIs the server does not register: {invented}; "
        f"registered={sorted(server_uris)}. A retired scheme means a rename left a mention "
        f"behind; a genuinely new kind of link belongs in _FOREIGN_SCHEMES with its reason."
    )


def test_no_shipped_module_invents_a_resource_uri() -> None:
    """The same question one surface over, where a stale mention rides out on ``tools/list``."""
    server_uris = _registered_uris()
    assert server_uris, "no @mcp.resource registrations found, the test anchor broke"

    invented: dict[str, list[str]] = {}
    for module in _source_files():
        extra = _uris_a_page_may_not_name(module.read_text(encoding="utf-8"), server_uris)
        if extra:
            invented[str(module.relative_to(_ROOT))] = extra

    assert not invented, (
        f"shipped modules name URIs the server does not register: {invented}; "
        f"registered={sorted(server_uris)}"
    )


def test_a_left_behind_scheme_from_a_rename_is_reported() -> None:
    """The red proof for [QA-26], kept in the suite instead of being run once and forgotten.

    This is the direction the previous version could not see. With the scheme spelled literally,
    swapping it left this exact text green.
    """
    left_behind = "the cookbook is at epics-pv://guide, see also epics://guide"

    assert _uris_a_page_may_not_name(left_behind, {"epics://guide"}) == ["epics-pv://guide"]


def test_an_invented_path_under_the_registered_scheme_is_reported() -> None:
    """The direction the two probes beside this one do NOT cover, and an audit found it unpinned.

    Both of those compare a FOREIGN scheme against a registered one, so the whole check could be
    weakened to a scheme-only comparison and stay green while every invented path under our own
    scheme walked through. Measured: replacing the URI comparison with
    ``match.group(1) not in {u.split("://")[0] for u in registered}`` left all five tests passing
    and silently stopped reporting ``epics://status``, ``epics://helth`` and ``epics://guidebook``.
    That is the exact defect this module was written for, ``health://status`` one scheme over.

    Two offenders rather than one, and one of them a PREFIX of the registered URI, so a truncating
    return and a ``startswith`` comparison die here too.
    """
    invented = "read epics://status and epics://guidebook, but not epics://guide"

    assert _uris_a_page_may_not_name(invented, {"epics://guide"}) == [
        "epics://guidebook",
        "epics://status",
    ]


def test_an_ordinary_link_and_a_registered_uri_are_not_reported() -> None:
    """The other direction, so the check above cannot be passed by refusing everything."""
    clean = (
        "See https://example.org/docs and http://localhost:8080/ChannelFinder, install with "
        "git+https://example.org/pkg, monitor sim://ramp, and read epics://guide."
    )

    assert _uris_a_page_may_not_name(clean, {"epics://guide"}) == []
