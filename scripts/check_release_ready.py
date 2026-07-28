#!/usr/bin/env python3
"""Release gate: refuse a distribution a package index would reject or a user would mistrust.

Runs against BUILT artifacts (``dist/*.whl``, ``dist/*.tar.gz``), never against ``pyproject.toml``.
That distinction is the whole point: what an index sees is the generated ``METADATA``, and the two
can differ. Measured here, on this repository: the ``[displays]`` extra produced **two** direct
references in the metadata, not one, because the ``all`` extra resolved ``displays`` transitively.
Reading the source would have found one and called it done.

Three checks, in the order a failure costs:

1. **No direct reference.** A ``Requires-Dist`` carrying ``@ <url>`` makes the upload fail outright;
   it is the one thing PyPI refuses categorically. This repository had exactly that until the
   display engine became a dependency group, and the failure would otherwise arrive as an opaque
   HTTP 400 at the end of a release, rather than here.
2. **No pre-release version**, unless ``--allow-prerelease`` says so. ``0.3.0.dev0`` is the working
   version between releases, and publishing it by accident cannot be undone: an index never lets a
   version be reused, even after a delete.
3. **A description that renders.** The long description is the project page. If its content type is
   missing or its body is empty, the page renders as raw text or as nothing, and there is no second
   chance for a published file.

Exit 1 blocks, and every finding is printed as ``artifact: reason`` on stderr, mirroring the other
two guards in ``scripts/``. This is deliberately NOT a pre-commit hook: it needs an artifact, which
only exists after a build.

Usage::

    uv build
    uv run python scripts/check_release_ready.py dist/*
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from collections.abc import Sequence
from email.parser import Parser
from pathlib import Path

# A direct reference is PEP 508's ``name @ url`` form. Marker text can contain '@' too (an author
# email in an environment marker, say), so only the part BEFORE the first ';' is examined.
_DIRECT_REFERENCE = "@"

# PEP 440 pre-release and development spellings: a ``.dev`` segment anywhere, or an ``a``, ``b`` or
# ``rc`` segment directly after a release digit. Written out rather than taken from
# ``packaging.version`` because that package is not a declared dependency of this project, and a
# release gate that only works when a transitive dependency happens to be installed is not a gate.
_PRERELEASE_RE = re.compile(r"\.dev\d*|(?<=\d)(?:a|b|rc)\d*")


def _metadata_from_wheel(path: Path) -> str:
    """Return the ``METADATA`` text of a wheel, which is what an index stores and renders."""
    with zipfile.ZipFile(path) as wheel:
        names = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
        if not names:
            raise ValueError("wheel contains no .dist-info/METADATA")
        return wheel.read(names[0]).decode("utf-8")


def _metadata_from_sdist(path: Path) -> str:
    """Return the ``PKG-INFO`` text of a source distribution (the sdist spelling of METADATA)."""
    with tarfile.open(path, "r:gz") as sdist:
        names = [n for n in sdist.getnames() if n.count("/") == 1 and n.endswith("/PKG-INFO")]
        if not names:
            raise ValueError("sdist contains no top-level PKG-INFO")
        member = sdist.extractfile(names[0])
        if member is None:
            raise ValueError(f"{names[0]} is not a regular file")
        return member.read().decode("utf-8")


def read_metadata(path: Path) -> str:
    """Dispatch on the artifact kind; anything else is a caller error, not a silent pass."""
    if path.suffix == ".whl":
        return _metadata_from_wheel(path)
    if path.name.endswith(".tar.gz"):
        return _metadata_from_sdist(path)
    raise ValueError(f"not a distribution artifact: {path.name}")


def is_prerelease(version: str) -> bool:
    """True for a version an index would accept but nobody meant to publish.

    ``0.3.0.dev0``, ``0.3.0b1`` and ``1.0rc2`` are pre-releases; ``0.3.0`` and ``2026.1`` are not.
    """
    return bool(_PRERELEASE_RE.search(version))


def check(metadata: str, *, allow_prerelease: bool) -> list[str]:
    """Return one reason string per finding; empty means the artifact may be published."""
    headers = Parser().parsestr(metadata)
    reasons: list[str] = []

    for requirement in headers.get_all("Requires-Dist") or []:
        # Split off the environment marker first: only the requirement half can hold a direct URL.
        requirement_part = requirement.split(";", 1)[0]
        if _DIRECT_REFERENCE in requirement_part:
            reasons.append(
                f"direct reference in Requires-Dist ({requirement_part.strip()}). A package index "
                "rejects these outright. Move the dependency to a dependency group with its "
                "source in [tool.uv.sources], which stays out of published metadata"
            )

    version = headers.get("Version", "")
    if not allow_prerelease and is_prerelease(version):
        reasons.append(
            f"version {version!r} is a pre-release. Publishing it burns that version number for "
            "good (an index never allows reuse). Bump [project].version and the __init__.py "
            "fallback, or pass --allow-prerelease if this is intentional"
        )

    if not headers.get("Description-Content-Type"):
        reasons.append(
            "no Description-Content-Type, so the project page renders the long description as "
            "plain text. Set [project].readme to a file the backend can type"
        )
    # get_payload() returns a list for a multipart message. METADATA is never multipart, so a
    # non-str payload is itself a malformed artifact rather than a case to handle gracefully.
    payload = headers.get_payload()
    if not isinstance(payload, str) or not payload.strip():
        reasons.append("the long description is empty, so the project page would have no content")

    return reasons


def main(argv: Sequence[str] | None = None) -> int:
    """Check every artifact named on the command line; return 1 if any of them must not ship."""
    parser = argparse.ArgumentParser(
        description="Refuse a distribution that must not be published."
    )
    parser.add_argument("artifacts", nargs="+", type=Path, help="dist/*.whl and/or dist/*.tar.gz")
    parser.add_argument(
        "--allow-prerelease",
        action="store_true",
        help="permit a .dev/a/b/rc version (a deliberate pre-release upload)",
    )
    args = parser.parse_args(argv)

    findings: list[str] = []
    for artifact in args.artifacts:
        try:
            metadata = read_metadata(artifact)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            findings.append(f"{artifact.name}: unreadable ({exc})")
            continue
        findings.extend(
            f"{artifact.name}: {reason}"
            for reason in check(metadata, allow_prerelease=args.allow_prerelease)
        )

    if findings:
        sys.stderr.write("Blocked: this distribution must not be published.\n")
        for finding in findings:
            sys.stderr.write(f"  {finding}\n")
        return 1

    sys.stderr.write(f"Release gate passed for {len(args.artifacts)} artifact(s).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
