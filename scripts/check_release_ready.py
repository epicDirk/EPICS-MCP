#!/usr/bin/env python3
"""Release gate: refuse a distribution a package index would reject or a user would mistrust.

Runs against BUILT artifacts (``dist/*.whl``, ``dist/*.tar.gz``), never against ``pyproject.toml``.
That distinction is the whole point: what an index sees is the generated ``METADATA``, and the two
can differ. Measured here, on this repository: the ``[displays]`` extra produced **two** direct
references in the metadata, not one, because the ``all`` extra resolved ``displays`` transitively.
Reading the source would have found one and called it done.

Four checks, in the order a failure costs:

1. **No direct reference.** A ``Requires-Dist`` carrying ``@ <url>`` makes the upload fail outright;
   it is the one thing PyPI refuses categorically. This repository had exactly that until the
   display engine became a dependency group, and the failure would otherwise arrive as an opaque
   HTTP 400 at the end of a release, rather than here.
2. **No pre-release version**, unless ``--allow-prerelease`` says so. Publishing a ``.dev`` version
   by accident cannot be undone: an index never lets a version be reused, even after a delete.
   ⚠️ This check does NOT rest on a house convention of carrying a ``.dev`` version between
   releases, and an earlier wording claimed one. Measured with a pickaxe search for ``.dev0`` over
   every commit that touched ``pyproject.toml``: exactly two ever carried such a version, the one
   that introduced it and the one that removed it at 0.3.0; since then the declared version simply
   stays at the last release until the next bump. It earns its place regardless: it refuses the
   accident, and
   ``--allow-prerelease`` is the rehearsal's safety valve, there so a dry run stays green whatever
   the working version happens to be rather than because it is known to be a pre-release.
3. **A description that renders.** The long description is the project page. If its content type is
   missing or its body is empty, the page renders as raw text or as nothing, and there is no second
   chance for a published file.
4. **One build, one distribution.** The three checks above judge an artifact ALONE, so a leftover
   file beside a fresh one is invisible unless it happens to fail on its own merits. ``uv build``
   does not clear ``dist/``, and the usual invocation is a shell glob over the whole directory, so
   both builds arrive together and an upload takes both. Measured in this checkout: ``dist/`` held
   ``epics-pv-mcp 0.3.0.dev0`` beside a 0.4.0 tree, a pre-rename name on a pre-release version. The
   tag path already blocks that through ``--expect-version``; the REHEARSAL path
   (``--allow-prerelease``, no expected version) is where it passes, which is the gap this closes.

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


def check(metadata: str, *, allow_prerelease: bool, expect_version: str | None = None) -> list[str]:
    """Return one reason string per finding; empty means the artifact may be published.

    *expect_version* is the tag-derived version the caller is about to publish under (QA-2): the
    workflow builds whatever ``pyproject.toml`` says, so a tag pushed against a tree whose version
    was never bumped would upload the WRONG version under that tag, and an index never lets either
    number be reused. Exact string match against the built metadata, which hatchling normalises,
    so the caller strips the ``v`` prefix and nothing else.
    """
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
    if expect_version is not None and version != expect_version:
        reasons.append(
            f"version mismatch: the tag says {expect_version!r} but the built metadata says "
            f"{version!r}. Publishing would burn the wrong version number for good (an index "
            "never allows reuse). Bump [project].version and the __init__.py fallback so the "
            "tree matches the tag"
        )
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


def _normalised_name(name: str) -> str:
    """PEP 503 name normalisation, so one distribution spelled two ways is not a finding.

    A wheel filename uses ``epics_mcp`` while the metadata field says ``epics-mcp``, and a backend
    is free to emit either into ``Name``. Comparing raw strings would report a build against itself.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def check_one_build(artifacts: Sequence[tuple[str, str]]) -> list[str]:
    """Refuse a set of artifacts that did not all come from ONE build of ONE distribution.

    Takes ``(filename, metadata)`` pairs, because the finding is about the SET: every other check
    here judges an artifact alone and therefore cannot see a leftover sitting beside a fresh one.
    A single artifact, or a wheel and an sdist of the same build, yields nothing.
    """
    builds: dict[tuple[str, str], list[str]] = {}
    labels: dict[tuple[str, str], str] = {}
    for filename, metadata in artifacts:
        headers = Parser().parsestr(metadata)
        raw_name = headers.get("Name", "")
        version = headers.get("Version", "")
        key = (_normalised_name(raw_name), version)
        builds.setdefault(key, []).append(filename)
        # The RAW spelling is what the reader has to recognise on disk, so the message carries it
        # even though the comparison above is normalised.
        labels[key] = f"{raw_name} {version}"
    if len(builds) < 2:
        return []
    detail = "; ".join(
        f"{labels[key]} ({', '.join(sorted(files))})" for key, files in sorted(builds.items())
    )
    return [
        f"these artifacts are not one build: {detail}. uv build does not clear dist/, so an older "
        "build stays behind and a dist/* glob hands it to the upload as well. Run: "
        "rm -rf dist && uv build"
    ]


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
    parser.add_argument(
        "--expect-version",
        default=None,
        metavar="X.Y.Z",
        help="require the built metadata Version to equal this exactly (the release workflow "
        "passes the tag with its leading 'v' stripped, so a tag on an unbumped tree blocks "
        "instead of publishing the wrong version)",
    )
    args = parser.parse_args(argv)

    findings: list[str] = []
    readable: list[tuple[str, str]] = []
    for artifact in args.artifacts:
        try:
            metadata = read_metadata(artifact)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            findings.append(f"{artifact.name}: unreadable ({exc})")
            continue
        readable.append((artifact.name, metadata))
        findings.extend(
            f"{artifact.name}: {reason}"
            for reason in check(
                metadata,
                allow_prerelease=args.allow_prerelease,
                expect_version=args.expect_version,
            )
        )
    # Unprefixed: this finding belongs to the SET, and naming one artifact would point at whichever
    # file the loop happened to see first rather than at the pair that disagrees.
    findings.extend(check_one_build(readable))

    if findings:
        sys.stderr.write("Blocked: this distribution must not be published.\n")
        for finding in findings:
            sys.stderr.write(f"  {finding}\n")
        return 1

    sys.stderr.write(f"Release gate passed for {len(args.artifacts)} artifact(s).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
