"""Client for the Phoebus Olog (electronic logbook) REST API, read plus one gated write path.

The reads are default-disabled (no ``EPICS_MCP_OLOG_URL`` = no network call) and return the WHOLE
server record; the single write (:meth:`OlogClient.create_log_entry`, ``PUT /logs``) is gated
separately by :mod:`epics_mcp.olog_safety` (this module only carries the transport) and its
response goes through the same output shaping as a read (:func:`_expand_log_entry`).

Read-only jobs:

  GET {root}/logs/search?desc&logbooks&tags&level&title&start&end&size&from&sort
                                                                        → search
  GET {root}/logs/{id}                                                  → one log entry
  GET {root}/logbooks                                                   → list logbook names
  GET {root}/tags                                                       → list tag names
  GET {root}/levels                                                     → list level names (OA2)

``olog_url`` is the Olog REST root incl. context path (e.g. ``http://olog:8080/Olog``); the
``/logs`` paths are appended. Reading needs no auth by default; an optional ``Authorization``
header is forwarded for secured deployments. Structure mirrors
:mod:`epics_mcp.services.alarm_client`.

Entries leave WHOLE: ``title``, ``description``, ``owner``, ``source``, ``properties`` and the raw
``attachments`` array all come back as the server sent them. The former DS-PRIVACY read redaction
(an output allowlist with withheld free text) was removed 2026-08-01: it was written against an
ASSUMED privacy rule that was never specified for this server, and withholding the free text costs
a logbook its entire point (a search returns ids whose content the caller cannot judge, and a
write cannot verify what it just wrote). If a real facility privacy specification ever arrives,
rebuild against THAT spec; the removed mechanism (``_project_log_entry`` and its allowlist) is in
the git history up to 2026-08-01.

Redirects are still refused rather than followed (:meth:`OlogClient._get`): Olog's REST API has no
legitimate redirect, so a hop is a misconfiguration surfaced loudly, not silently traversed.

Keeping person names out of COMMITTED files is a separate, unchanged rule (the facility-agnostic
guards and hand-transcription rule, CLAUDE.md).

⚠️ Search-param names and the entry field names follow the documented Phoebus Olog model. What has
actually been PROBED live against a running Olog (2026-07-15), and what has not:

* ``start``/``end``/``tz``, probed, and the window turned out to be actively BROKEN as sent: Olog
  cannot parse ISO-8601 and says so only by returning an empty result. See
  :mod:`epics_mcp.services.olog_time` for the mechanism and :meth:`OlogClient._add_window` for
  the fix.
* ``desc``/``logbooks``/``tags``, probed WITH positive controls (``desc`` matched only bodies, not
  titles; each filter returned a known subset, so "everything is 0" could be ruled out). They filter
  as named.
* ``level``/``title``, probed again 2026-07-19 (OA2/OA5), now with BOTH controls and pinned by live
  tests rather than by this docstring alone. They filter as named and are case-insensitive (the
  index analyzer lowercases). Three findings that changed the code:

  - An UNKNOWN value is not rejected: the server answers 200 with **0 hits**, which reads exactly
    like "there are no such entries", the ``sort`` failure mode again, one step worse. Hence
    ``list_log_levels`` (enumerate the valid values) and the service-layer annotation on an empty
    level-filtered result.
  - A BLANK value is not "no filter", and the two fields DISAGREE about what it means: a blank
    ``level`` matches nothing (0 hits) while a blank ``title`` is dropped (unfiltered result). Both
    are refused before the request (:func:`_reject_blank_filter`).
  - ``title`` matches whole WORDS, not substrings, a fragment finds nothing unless wildcarded, and
    several words are AND-ed. Notably this is NOT the anchored-glob behaviour of ``find_channels``,
    so that wording must not be copied over.

  The control that makes the probe meaningful: an unknown PARAMETER NAME returns the unfiltered
  count, because ``LogSearchUtil``'s parameter switch ends in ``default: // Unsupported search
  parameters are ignored``. A filter that "returns results" proves nothing on its own.
* ``sort``: probed, and unreadable values do NOT fail: anything but ``down``/``desc`` silently
  becomes ASC, the REVERSE of this client's default. The tool layer therefore constrains it to a
  ``Literal["down", "up"]`` (:mod:`epics_mcp.server`); see
  ``tests/test_olog_live.py::test_unreadable_sort_silently_reverses_on_the_server``.
* ``size``/``offset``, ``offset`` probed (page 0 vs page N differ as expected); ``size`` is bounded
  at the tool layer and only ever narrows a result.
* The ENTRY FIELD names (the shape of what comes BACK) are still best-effort, they are not a
  promise this client makes about a request; extra fields a given Olog version returns are passed
  through unchanged.
"""

from __future__ import annotations

import functools
import json
import re
from typing import TypedDict, cast
from urllib.parse import quote

import requests

from epics_mcp.config import get_config
from epics_mcp.services._http import (
    MultipartFiles,
    build_write_session,
    get_shared_session,
    http_status,
    is_http_400,
    is_http_404,
    rest_get_bytes,
    rest_get_json,
    rest_post_multipart,
    rest_put_json,
    rest_put_multipart,
)
from epics_mcp.services._time_window import TimeWindowFormatError
from epics_mcp.services.olog_exceptions import (
    OlogConnectionError,
    OlogFilterValueError,
    OlogResponseError,
    OlogRoundTripUnsafe,
)
from epics_mcp.services.olog_time import OLOG_WIRE_TZ, normalize_olog_time


class AttachmentUpload(TypedDict):
    """One attachment ready for a multipart upload, the service-layer hands these to the client.

    ``id`` is the client-generated UUID (the checker mints it, deterministic, injectable).
    ``filename`` is the per-submission-unique ``<id>_<basename>`` (id-prefixed so a by-name
    download can never hit the server's duplicate-filename 404). ``content`` is the raw bytes;
    ``content_type`` the guessed MIME (``None`` → the wire falls back to octet-stream, mirroring
    CS-Studio's ``Files.probeContentType`` fallback). The client turns ``content_type`` into the
    Olog ``fileMetadataDescription`` metadata string (``"image"``/``"file"``), NOT a MIME.
    """

    id: str
    filename: str
    content: bytes
    content_type: str | None


def _attachment_meta_description(content_type: str | None) -> str:
    """Olog's ``fileMetadataDescription`` metadata STRING (``"image"``/``"file"``), NOT a MIME.

    CS-Studio sets exactly these two values (AttachmentsEditorController:313-317)."""
    return "image" if (content_type or "").startswith("image") else "file"


def _attachment_parts(
    uploads: list[AttachmentUpload],
) -> tuple[list[dict[str, str]], MultipartFiles]:
    """Split uploads into (``attachments`` metadata, ``files`` multipart parts).

    Shared by create-with-attachments (:meth:`OlogClient._put_multipart`) and attach-to-existing
    (:meth:`OlogClient.add_attachment`): each upload contributes one ``{id, filename,
    fileMetadataDescription}`` metadata entry (paired to a ``files`` part by ``filename``) plus one
    ``("files", (filename, content, content_type))`` part. Keeping the two in one place means the
    metadata string and the part always agree on the filename."""
    meta = [
        {
            "id": item["id"],
            "filename": item["filename"],
            "fileMetadataDescription": _attachment_meta_description(item["content_type"]),
        }
        for item in uploads
    ]
    parts: MultipartFiles = [
        (
            "files",
            (item["filename"], item["content"], item["content_type"] or "application/octet-stream"),
        )
        for item in uploads
    ]
    return meta, parts


# Default cap on returned log entries (a wide/empty search is otherwise unbounded).
DEFAULT_MAX_LOGS = 50

# Static, non-identifying client-info header value (server-side only logged, never persisted). NOT a
# personal username/hostname, a facility-agnostic constant.
_CLIENT_INFO = "epics-mcp"


def _names(items: object) -> list[str]:
    """The ``name`` of each dict in *items* (logbook/tag structs); per-item owners dropped.

    LENIENT by design and ONLY for fields INSIDE an already-anchored entry (``_derive_shape``):
    the entry's identity is guarded by :func:`_require_entry`, and its ``logbooks``/``tags`` are a
    derived metadata projection where one malformed element must not sink the whole entry. The
    TOP-LEVEL ``/logbooks`` / ``/tags`` listings go through :func:`_named_list` instead, there
    the list IS the answer, and unreadable must never read as "there are none" (S11).
    """
    if not isinstance(items, list):
        return []
    return [str(item["name"]) for item in items if isinstance(item, dict) and "name" in item]


def _named_list(data: object, endpoint: str) -> list[str]:
    """STRICT name extraction for the top-level ``/logbooks`` / ``/tags`` listings (S11).

    The measured payload (live) is a list of ``{name, ...}`` structs with ``name`` always a string.
    Anything else used to collapse to ``[]``, a fabricated "there are no logbooks/tags",
    indistinguishable from a genuinely empty server, and a silently dropped item told anyone
    validating a filter name "this one does not exist". Unreadable now raises.
    """
    if not isinstance(data, list):
        raise OlogResponseError(
            f"Olog {endpoint} returned an unreadable payload "
            f"(expected a list, got {type(data).__name__}); the listing is not readable."
        )
    names: list[str] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise OlogResponseError(
                f"Olog {endpoint} returned an unreadable item "
                f"(expected a dict with a string 'name', got {type(item).__name__})."
            )
        names.append(item["name"])
    return names


#: Exactly what Java's ``String.trim()`` removes: every code point <= U+0020.
#:
#: Python's bare ``str.strip()`` is Unicode-aware and ALSO removes NBSP / thin / ideographic space:
#: which the server KEEPS. Stripping them here would silently normalise an unmatchable level into a
#: configured one (measured 2026-07-19: ``level="\xa0Info"`` returns 0 hits where ``level="Info"``
#: returns 19), so the "is this a configured level?" cross-check would see a known name, stay quiet,
#: and let a fabricated emptiness through.
_JAVA_TRIM_CHARS = "".join(chr(code) for code in range(0x21))

#: The server's separator class for ``level``, ``LogSearchUtil`` splits on exactly these.
_LEVEL_SEPARATORS = re.compile(r"[|,;]")

#: The server's separator class for ``title`` (and ``desc``), which is NOT simply "the level class
#: plus whitespace": the Java literal is ``[\|,;\s+]``, and inside a character class that trailing
#: ``+`` is a LITERAL member, not a quantifier. A ``+``-only title therefore yields no search terms
#: at all, and Olog answers an UNFILTERED result (measured: ``title="+"`` returns every entry).
_TITLE_SEPARATORS = re.compile(r"[|,;\s+]")


def split_level_values(value: str) -> list[str]:
    """The parts an Olog ``level`` filter is OR-ed over, mirroring the server's own split+trim.

    ``LogSearchUtil`` splits a level value on ``[|,;]`` and treats the parts as alternatives
    (measured: two levels comma-joined return the union of their entry counts), then applies Java's
    ``trim()``: hence :data:`_JAVA_TRIM_CHARS` rather than Python's wider ``strip()``.

    Public because the service layer re-uses exactly this split to name which parts of a fruitless
    level filter the server does not know, a private copy there would be free to drift from what
    was actually sent."""
    return [
        stripped
        for part in _LEVEL_SEPARATORS.split(value)
        if (stripped := part.strip(_JAVA_TRIM_CHARS))
    ]


def _reject_blank_filter(name: str, value: str | None) -> None:
    """Refuse a filter that is present but blank, BEFORE any request (OA2/OA5).

    ``None`` means "not filtering" and is fine. A value that leaves NO search term once the server
    has split it is a filter the caller meant to apply and the server will not apply, and it is
    refused here because every way that plays out is misleading. Measured 2026-07-19:

    * ``level=""`` → 0 hits (Java's ``split`` returns one empty term, which becomes a wildcard
      matching nothing), reads exactly like "there are no such entries";
    * ``level=","``, ``title=""``, ``title="+"`` → the term list comes out EMPTY, the filter is
      dropped, and the caller gets the UNFILTERED set presented as a filtered one.

    The second outcome is the worse of the two, and it is why the check must use the FIELD'S OWN
    separator class: ``title`` treats whitespace and a literal ``+`` as separators too, so a value
    that is a perfectly good level (``"+"``) is blank as a title."""
    if value is None:
        return
    separators = _TITLE_SEPARATORS if name == "title" else _LEVEL_SEPARATORS
    if any(part.strip(_JAVA_TRIM_CHARS) for part in separators.split(value)):
        return
    raise OlogFilterValueError(
        f"{name}={value!r} contains no search term once Olog has split it on its separators "
        f"({'whitespace, + , ; |' if name == 'title' else ', ; |'}), so it is not a usable filter. "
        "Olog does not reject it, depending on the exact value it either matches NOTHING (0 hits, "
        "reading like 'no such entries') or drops the filter and returns the UNFILTERED set as if "
        f"it were filtered. Pass a real value, or omit {name} to not filter."
    )


def _level_list(data: object, endpoint: str) -> tuple[list[str], str | None, str | None]:
    """``(names, default_level, note)`` for ``GET /levels``, computed in ONE pass (OA2).

    Reuses the S11-strict :func:`_named_list` for the names, so an unreadable listing RAISES here
    too and never collapses to "there are no levels". The default flag is then read from that SAME
    already-validated list, a second, independent walk is what let a guard and a payload diverge
    once before (OA3's P0), so names and default are never derived from separate traversals.

    A ``Level`` is ``{name, defaultLevel}`` (Level.java) and carries no owner, so this is trivially
    PII-free like ``/tags``.

    The default is reported only when the server states it UNAMBIGUOUSLY; otherwise ``None`` plus a
    note saying why, never a guess:

    * exactly one item flagged → that name;
    * no item flagged → ``None`` (legitimate: a server may mark none);
    * MORE than one flagged → ``None``. Not hypothetical: the server's own seed file ships two
      defaults, even though its ``createLevel`` endpoint enforces one, so picking "the first" would
      invent an answer the server never gave;
    * the flag missing or not a boolean on any item → ``None``. Deliberately not a raise: the NAMES
      are still readable and are the tool's primary answer; an older/leaner Olog omitting the field
      must not take the whole listing down.
    """
    names = _named_list(data, endpoint)
    # _named_list has already PROVEN this shape (top-level list, every item a dict with a str
    # 'name') and raised otherwise, hence a cast rather than a second round of checks: re-deriving
    # the shape is exactly the independent-walk pattern this function exists to avoid.
    items = cast("list[dict[str, object]]", data)

    # Zipped against the names _named_list just extracted (same list, same order, strict=True pins
    # that) so the flag is always attributed to the name it was read next to.
    flagged: list[str] = []
    for name, item in zip(names, items, strict=True):
        flag = item.get("defaultLevel")
        if not isinstance(flag, bool):
            unreadable = (
                f"Olog {endpoint} did not report a readable 'defaultLevel' flag for every "
                "level, so the default is withheld rather than guessed; the names are unaffected."
            )
            return names, None, unreadable
        if flag:
            flagged.append(name)

    if len(flagged) == 1:
        return names, flagged[0], None
    if not flagged:
        return names, None, f"Olog {endpoint} marks no level as the default."
    ambiguous = (
        f"Olog {endpoint} marks {len(flagged)} levels as default "
        f"({', '.join(sorted(flagged))}); only one can be THE default, so it is withheld "
        "rather than guessed."
    )
    return names, None, ambiguous


def _require_entry(data: object, context: str) -> dict[str, object]:
    """The measured log-entry record: a dict carrying the identity field ``id`` (S11).

    Guards every place a payload is about to be PROJECTED as a log entry. The old behaviour
    projected ANY non-empty dict, an unrelated 2xx body became a fabricated, plausible entry
    (auditor probe: ``{"unexpected": "shape"}`` → a projected log entry that never existed):
    and an empty/non-dict body collapsed to ``None``, indistinguishable from the definitive
    404 "not found".
    """
    if isinstance(data, dict) and data.get("id") is not None:
        return data
    raise OlogResponseError(
        f"Olog {context} returned a payload that is not a log entry "
        f"(expected a dict carrying a non-null 'id', got {type(data).__name__}); "
        "the answer is not readable, this is NOT a 'not found'."
    )


def attachment_round_trip(raw_entry: dict[str, object]) -> tuple[list[dict[str, str]], list[str]]:
    """``(metadata to resubmit, offenders that cannot survive)``, computed in ONE pass.

    Olog keeps an attachment across an update only if the submitted list still contains it:
    ``CollectionUtils.retainAll(persisted, submitted)`` (LogResource.java:537-538). The submitted
    side deserializes into a ``TreeSet`` (``Log.attachments``, Log.java:63), so ``contains`` is
    decided by ``Attachment.compareTo``, which compares ``filename.compareToIgnoreCase``
    (Attachment.java:55-68). Retention is therefore **filename-keyed**: ``equals``/``hashCode`` do
    use the id (:37-53) but a ``TreeSet`` never consults them.

    An attachment is an OFFENDER when re-submitting it cannot preserve it:
    * a filename colliding case-insensitively with another, they collapse to ONE element in the
      set (in the persisted set, in ours, and in the result); the id cannot tell them apart;
    * no usable filename, nothing can match it (and a naive ``str(None)`` would submit the literal
      ``"None"``, matching nothing);
    * no id, or a shape that is not a readable record, we cannot faithfully resubmit it;
    * an ``attachments`` value that is present but not a list, we cannot enumerate what to
      resubmit at all, and submitting nothing would prune EVERYTHING.

    **Both halves come from this one function on purpose.** They were once two (a guard that
    checked filenames and a builder that additionally skipped id-less/malformed items), and the
    pair silently disagreed: an id-less attachment passed the guard as "safe" and was then dropped
    from the payload, the exact silent prune the guard exists to prevent (found by adversarial
    diff review, probe-measured). Deriving payload and verdict together makes that drift
    impossible: anything the payload would omit is, by construction, an offender.

    An EMPTY offender list means the entry round-trips losslessly; the callers refuse otherwise
    (safe-refuse) rather than silently dropping an attachment.
    """
    items = raw_entry.get("attachments")
    meta: list[dict[str, str]] = []
    offenders: list[str] = []
    if items is None:
        return meta, offenders
    if not isinstance(items, list):
        offenders.append("<unreadable attachment list>")
        return meta, offenders
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            offenders.append("<unreadable attachment>")
            continue
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            offenders.append("<no filename>")
            continue
        if item.get("id") is None:
            offenders.append(filename)
            continue
        # lower(), not casefold(): Java's compareToIgnoreCase folds per character, so it does NOT
        # treat "straße"/"strasse" as equal, casefold() does, and would refuse an entry that
        # actually round-trips fine.
        key = filename.lower()
        if key in seen:
            offenders.append(filename)
            continue
        seen.add(key)
        meta.append(
            {
                "id": str(item["id"]),
                "filename": filename,
                "fileMetadataDescription": str(item.get("fileMetadataDescription") or "file"),
            }
        )
    return meta, sorted(set(offenders))


def unroundtrippable_attachment_filenames(raw_entry: dict[str, object]) -> list[str]:
    """The offenders of :func:`attachment_round_trip`, the safe-refuse check on its own."""
    return attachment_round_trip(raw_entry)[1]


def _derive_shape(entry: dict[str, object], out: dict[str, object]) -> dict[str, object]:
    """Add the DERIVED fields the projection owes its callers, and return *out*.

    ``logbooks``/``tags`` are reshaped from Olog's list-of-structs to name-only lists, and
    ``attachment_count`` is SYNTHESISED (it is not an Olog field at all). Returning the server
    dict verbatim instead would silently drop ``attachment_count`` and flip ``logbooks`` from
    list[str] to list[dict], and ``dict[str, object]`` is wide enough that mypy would not say a
    word.
    """
    out["logbooks"] = _names(entry.get("logbooks"))
    out["tags"] = _names(entry.get("tags"))
    attachments = entry.get("attachments")
    out["attachment_count"] = len(attachments) if isinstance(attachments, list) else 0
    return out


def _expand_log_entry(entry: dict[str, object]) -> dict[str, object]:
    """Return *entry* WHOLE (free text, owner, source, properties, raw attachments) + derived
    fields.

    The ONE output projection since the read redaction was removed (decision PI, 2026-08-01): the
    former ``_project_log_entry`` allowlist was built on an ASSUMED privacy rule that was never
    specified for this server. If a real facility privacy specification ever arrives, rebuild
    against that spec (the removed mechanism is in the git history up to 2026-08-01).

    ⚠ Whole means the caller now sees BOTH bodies with nothing marking which is which: ``source``
    is what an author wrote, ``description`` is Olog's rendering of it. Anything that hands a body
    back to a write reads source, not description, or what the rendering dropped is lost.

    THE ONE PLACE THIS IS MEASURED, so the figure is not repeated on the surfaces that quote it.
    A read-only page of a LOCAL SANDBOX Olog, 2026-08-13, 94 entries: every one carried a
    ``source``, 22 differed from their ``description``, and of those 22 the difference was pure
    whitespace on 12 (the renderer collapses a blank line) and a genuine loss of markup on 10.
    Scope, because it bounds what any of this may claim: a sandbox is not a facility, and those 10
    are artifacts this repository's own live probes left behind, all carrying the same fixture
    string. What the page establishes is that the two fields DIVERGE in practice and that a
    divergence is not by itself proof of lost markup. It establishes nothing about how often a
    real logbook carries an inline image.
    """
    return _derive_shape(entry, dict(entry))


def _entries_of(data: object) -> list[object]:
    """The list of raw entries from an Olog search response (S11: strict).

    Two MEASURED shapes are readable: the ``{logs: [...], hitCount}`` wrapper (live Olog 6.x) and
    a bare list (an older version that returns just the page). Anything else used to collapse to
    ``[]``: a fabricated empty search, indistinguishable from zero hits (auditor probe
    OLOG_SEARCH_BAD_2XX → ``([], False, None)``). Unreadable now raises.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        logs = data.get("logs")
        if isinstance(logs, list):
            return logs
        raise OlogResponseError(
            "Olog search returned a mapping without a readable 'logs' list; "
            "the result is not readable, this is NOT an empty search."
        )
    raise OlogResponseError(
        f"Olog search returned an unreadable payload "
        f"(expected a list or a {{logs, hitCount}} mapping, got {type(data).__name__})."
    )


def _hit_count(data: object) -> int | None:
    """The Olog ``hitCount`` (total matches for the query) from a ``{logs, hitCount}`` wrapper.

    Returns ``None`` when the response carries no count at all (the bare-list variant of an older
    Olog, or a wrapper without the field), the honest "this server version provides no total".
    A PRESENT-but-unreadable count raises (S11): it must never silently become ``None``, which
    reads as "no count provided". ``hitCount`` is the total across ALL pages and need not equal
    the returned page size; Olog documents this on ``SearchResult`` (it differs whenever
    ``from``/``size`` paginate).
    """
    if isinstance(data, dict):
        count = data.get("hitCount")
        if count is None:
            return None
        if isinstance(count, int) and not isinstance(count, bool):
            return count
        raise OlogResponseError(
            f"Olog search returned an unreadable hitCount "
            f"(expected an integer, got {type(count).__name__})."
        )
    return None


class OlogClient:
    """Client for the Phoebus Olog REST API. Reads are GET-only and return the whole server
    record; the one write (create_log_entry, PUT /logs) is gated by olog_safety."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        auth_header: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = get_shared_session(auth_header=auth_header)
        # Keep the auth header on the instance so the lazily-built WRITE session
        # (:attr:`_write_session`) can carry it too, a constructor-local is out of the
        # cached_property's reach. The read `session` above stays byte-identical (auth on it is
        # harmless: reads / check_connectivity use it, never the PUT, that uses the write one).
        self._auth_header = auth_header

    @functools.cached_property
    def _write_session(self) -> requests.Session:
        """The dedicated session for the ONE write (:meth:`create_log_entry`): no retries and
        env-independent (see :func:`~epics_mcp.services._http.build_write_session`). Built
        lazily, so a read-only client never constructs it. ``verify`` mirrors the read session's
        resolved value; build_write_session then drops the env fallbacks. With an explicit
        ``EPICS_MCP_CA_BUNDLE`` read and write trust the SAME CA; on the plain default only the read
        session honours a ``REQUESTS_CA_BUNDLE`` env (the deliberate N03 tradeoff, a remote-https
        write needs its CA via config). A duplicate build (the cached_property is not
        thread-safe) yields an equivalent, side-effect-free session, so no lock is needed.
        """
        return build_write_session(auth_header=self._auth_header, verify=self.session.verify)

    def _get(self, url: str, params: dict[str, str]) -> object:
        """Issue a GET and return parsed JSON, translating failures to Olog exceptions.

        Redirects are refused, not followed: Olog's REST API has no legitimate redirect, so a hop
        is a misconfiguration and a loud error is the right answer.
        """
        return rest_get_json(
            self.session,
            url,
            params,
            self.timeout,
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            allow_redirects=False,
        )

    def check_connectivity(self) -> bool:
        """Return True if Olog is reachable; raise OlogConnectionError otherwise.

        A HEAD to the service root proves transport + TLS (the CA bundle), any HTTP response counts
        as reachable (status irrelevant, as in the Naming client). A transport/TLS failure re-raises
        as OlogConnectionError with the original requests error as ``__cause__``, so a caller can
        inspect it (:func:`is_ssl_error`) to tell a CA problem from a plain unreachable host.
        """
        try:
            self.session.head(self.base_url, timeout=self.timeout)
            return True
        except OSError as exc:
            # requests.exceptions.RequestException ⊂ OSError (see naming_client.check_connectivity).
            raise OlogConnectionError(
                f"Failed to connect to Olog at {self.base_url}: {exc}"
            ) from exc

    @staticmethod
    def _add_window(params: dict[str, str], start: str | None, end: str | None) -> None:
        """Put the normalized time window into *params*, or raise before anything is sent.

        Olog cannot read ISO-8601 and does not say so: an unparseable value degrades to *now*,
        collapsing the window to nothing and returning a well-formed EMPTY result (measured live;
        see :mod:`epics_mcp.services.olog_time`). So every value is normalized to a form Olog
        actually parses, or refused here.

        ``tz`` is sent iff an absolute value is present: it exists only to interpret wall-clock
        strings, and Olog resolves relative amounts by instant arithmetic where the zone is a
        no-op. Omitting it would let Olog read our strings in ITS default zone, an invisible
        offset against any Olog not running UTC.
        """
        start_is_absolute = end_is_absolute = False
        if start:
            params["start"], start_is_absolute = normalize_olog_time(start, param="start")
        if end:
            params["end"], end_is_absolute = normalize_olog_time(end, param="end")
        if start_is_absolute or end_is_absolute:
            params["tz"] = OLOG_WIRE_TZ
        if start_is_absolute and end_is_absolute and params["start"] > params["end"]:
            # Both absolute -> comparable right here, without a clock. The normalized form is
            # fixed-width and always UTC, so a string compare IS a chronological one. Left to
            # Olog this is a 400, which an anonymous read only ever sees as a 401
            # ("unauthorized"), a misleading answer to what is simply a swapped window. A
            # mixed/relative window stays the server's call: resolving the amount ourselves would
            # substitute our clock for the one that owns the data.
            raise TimeWindowFormatError(
                f"start ({start!r}) is after end ({end!r}), the window is empty. "
                f"Swap them, or drop end to search up to now."
            )

    def search_logbook(
        self,
        text: str | None = None,
        logbooks: str | None = None,
        tags: str | None = None,
        start: str | None = None,
        end: str | None = None,
        size: int = DEFAULT_MAX_LOGS,
        offset: int = 0,
        sort: str = "down",
        level: str | None = None,
        title: str | None = None,
    ) -> tuple[list[dict[str, object]], bool, int | None]:
        """Search log entries → ``(entries, capped, total_matches)``, each entry whole.

        *text* searches the description; *logbooks*/*tags* are comma-separated names; *start*/*end*
        bound the time window. *offset* is the 0-based pagination offset (Olog wire param ``from``;
        ``from`` is a Python keyword, hence the ``offset`` name, read past the first page).
        *sort* orders by create time: ``down`` (default) = newest first, ``up`` = oldest first.

        *level* and *title* (OA2/OA5) filter by triage level and by title. Both are honoured by the
        server and both are case-insensitive, probed differentially 2026-07-19 with a positive AND
        a negative control, plus a control proving an IGNORED parameter looks different (see
        ``tests/test_olog_live.py``). *level* is OR-ed over ``, ; |`` separated values; a value the
        server does not know yields **0 hits and no error**, so an unrecognised level is
        indistinguishable from "no entries" at this layer, ``list_log_levels`` enumerates the valid
        ones, and the service layer annotates an empty level-filtered result. *title* matches whole
        WORDS, not substrings (a word fragment finds nothing unless wildcarded with ``*``), and
        several words are AND-ed; it is a separate axis from *text*, which searches the body only.

        ``capped`` is True when more than *size* matched on this page (one extra is requested to
        detect it honestly, the Archiver/ChannelFinder pattern). ``total_matches`` is the true total
        across all pages (Olog ``hitCount``); it is ``None`` only when the Olog version returns a
        bare list with no count, ``capped`` then still signals honestly whether more matched (no
        fabricated total).
        """
        # Refused before anything is sent: a blank filter is never what the caller meant, and the
        # two servers-side outcomes are BOTH misleading (a blank level matches nothing → a
        # fabricated "no entries"; a blank title is dropped → an unfiltered result presented as a
        # filtered one). Measured 2026-07-19; see OlogFilterValueError.
        _reject_blank_filter("level", level)
        _reject_blank_filter("title", title)

        params: dict[str, str] = {"size": str(size + 1), "sort": sort}
        if text:
            params["desc"] = text
        if logbooks:
            params["logbooks"] = logbooks
        if tags:
            params["tags"] = tags
        if level:
            params["level"] = level
        if title:
            params["title"] = title
        self._add_window(params, start, end)
        if offset > 0:
            params["from"] = str(offset)
        try:
            data = self._get(f"{self.base_url}/logs/search", params)
        except OlogResponseError as exc:
            # Chain from the ORIGINAL transport cause so http_status still reads the served code
            # downstream (mirrors create_log_entry). 5xx / anything else propagates as-is.
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            if http_status(exc) == 401:
                # Measured: Olog answers 401 to an ANONYMOUS caller for every server-side 400:
                # its error dispatch requires auth and so masks its own rejection. This read path
                # IS anonymous, making 401 the reachable branch; blaming credentials would send
                # the reader after the wrong problem entirely.
                raise OlogResponseError(
                    "Olog rejected the search (HTTP 401). On this anonymous read path that "
                    "almost always means the QUERY was rejected, not that credentials are "
                    "wrong: Olog's error dispatch requires authentication and returns 401 in "
                    "place of its own 400. Check the time window (start/end) first."
                ) from cause
            if is_http_400(exc):
                raise OlogResponseError(
                    "Olog rejected the search (HTTP 400): most likely the time window, start "
                    "must not be after end, and with no end Olog compares against now, so a "
                    "start in the future is rejected."
                ) from cause
            raise
        entries = _entries_of(data)
        capped = len(entries) > size
        total_matches = _hit_count(data)
        # S11: every entry of the page must be a readable log entry, junk used to be silently
        # dropped (a fabricated, smaller result). Validate ALL entries, then project the page.
        records = [_require_entry(e, "search") for e in entries]
        projected = [_expand_log_entry(record) for record in records[:size]]
        return projected, capped, total_matches

    def get_log_entry(self, log_id: str) -> dict[str, object] | None:
        """Return one log entry by id (whole + derived fields), or ``None`` when it does not exist.

        A missing/deleted id makes the Olog answer HTTP 404, which is the definitive "not found"
        (mapped to ``None``); every OTHER failure (5xx, an unreachable service, or a 2xx whose
        body is not a readable log entry) PROPAGATES, so a could-not-read never masquerades as
        not-found. Mirrors the archiver ``getPVTypeInfo`` handling. (S11 closed the 2xx gap:
        ``{}`` used to collapse to ``None``, and any unrelated non-empty dict was projected as a
        fabricated entry.) NOTE: a real Olog answers **401** for an unknown id on this anonymous
        read path (measured 2026-07-16, its error dispatch requires auth), so the 404 branch is
        the documented contract, not the only signal seen in the wild; a 401 propagates loudly.
        """
        try:
            data = self._get(f"{self.base_url}/logs/{quote(log_id, safe='')}", {})
        except OlogResponseError as exc:
            if is_http_404(exc):
                return None
            raise
        return _expand_log_entry(_require_entry(data, "GET /logs/{id}"))

    def list_logbooks(self) -> list[str]:
        """Return the names of all Olog logbooks (``GET /logbooks``), name-only.

        Each ``Logbook`` struct also carries an ``owner`` and a ``state``; :func:`_named_list`
        keeps only the ``name``, because the names ARE the answer here: the valid filter values
        for ``search_logbook(logbooks=...)``.
        """
        data = self._get(f"{self.base_url}/logbooks", {})
        return _named_list(data, f"GET {self.base_url}/logbooks")

    def list_tags(self) -> list[str]:
        """Return the names of all Olog tags (``GET /tags``), name-only.

        A ``Tag`` has only ``name``/``state`` (no owner), so this is trivially PII-free. The names
        are the valid filter values for ``search_logbook(tags=...)``.
        """
        data = self._get(f"{self.base_url}/tags", {})
        return _named_list(data, f"GET {self.base_url}/tags")

    def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
        """``(names, default_level, note)`` for the Olog levels (``GET /levels``), name-only (OA2).

        Levels are the logbook's TRIAGE axis (Info / Problem / Request / ..., site-configurable, not
        a fixed enum), so the valid values can only come from the server. These names are the valid
        values for ``search_logbook(level=...)`` and for ``create_log_entry(level=...)``.
        *default_level* is the one a create uses when no level is given, and is ``None`` with an
        explaining *note* whenever the server does not state it unambiguously (see
        :func:`_level_list`).

        A ``Level`` carries no owner, so, like ``/tags``, this is a pure name/flag listing. An
        unreadable listing raises rather than collapsing to an empty list: "there are no levels"
        would tell a caller validating a filter value that none of them exist."""
        data = self._get(f"{self.base_url}/levels", {})
        return _level_list(data, f"GET {self.base_url}/levels")

    def create_log_entry(
        self,
        title: str,
        logbooks: list[str],
        description: str | None = None,
        level: str | None = None,
        tags: list[str] | None = None,
        in_reply_to: str | None = None,
        attachments: list[AttachmentUpload] | None = None,
    ) -> dict[str, object]:
        """Create a log entry (``PUT /logs``) and return the created entry whole.

        With *attachments* the entry is sent as ``multipart/form-data`` to ``PUT /logs/multipart``
        instead (create-with-attachments, OA1, the CS-Studio ``OlogHttpClient.save`` path); WITHOUT
        them the plain ``PUT /logs`` JSON path is byte-identical to before. Either way the response
        leaves through the same :func:`_expand_log_entry` shaping as a read, so a write can verify
        what it just wrote.

        *title* and *logbooks* are mandatory server-side (empty → HTTP 400); the named logbooks and
        tags MUST already exist (else 400). *in_reply_to* (wire query ``inReplyTo``) threads the new
        entry as a reply to an existing entry (a bad id → 400, checked server-side BEFORE the
        logbook validation). Auth rides on the session (Basic, see :func:`basic_auth_header`); the
        server sets ``owner`` from the auth Principal and IGNORES a caller-supplied owner, so this
        client never sends one.

        HTTP 400 → a clear :class:`OlogResponseError` (a named logbook/tag does not exist, an empty
        title, or a bad ``inReplyTo``, explicitly NOT "not found"); 401 → an auth error; 5xx
        propagates."""
        body: dict[str, object] = {
            "title": title,
            "logbooks": [{"name": name} for name in logbooks],
            # Always send a present description string (empty when the caller gave none): Olog's
            # save path dereferences description WITHOUT a null-guard (unlike source), so a missing
            # description 500s the server (NPE). A complete payload keeps the write robust.
            # Verified live (loopback phoebus-olog: a null description NPEs, an empty one succeeds).
            "description": description if description is not None else "",
        }
        if level is not None:
            body["level"] = level
        if tags:
            body["tags"] = [{"name": name} for name in tags]
        params: dict[str, str] = {}
        if in_reply_to is not None:
            params["inReplyTo"] = str(in_reply_to)
        try:
            if attachments:
                data = self._put_multipart(body, attachments, params)
            else:
                data = rest_put_json(
                    # The dedicated write session (no retries, env-independent), NOT self.session:
                    # a lost non-idempotent PUT must not be replayed into a duplicate entry
                    # (S23/F06),
                    # and the Basic auth header must not leak through an inherited proxy (S23/N03).
                    self._write_session,
                    f"{self.base_url}/logs",
                    body,
                    self.timeout,
                    params=params or None,
                    headers={"X-Olog-Client-Info": _CLIENT_INFO},
                    conn_exc=OlogConnectionError,
                    resp_exc=OlogResponseError,
                    # Never follow a redirect on a write: the gate approved THIS host, and a hop
                    # would
                    # carry the body and the Basic auth header somewhere it never inspected.
                    allow_redirects=False,
                )
        except OlogResponseError as exc:
            # Re-raise a clearer message for the actionable statuses, chaining from the ORIGINAL
            # transport cause (exc.__cause__, the requests HTTPError) so http_status still reads the
            # served code downstream (the audit error_code). 5xx / anything else propagates as-is:
            # note the multipart create wraps a saveAttachments failure as a bare 500, NOT a
            # 400/413.
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            if is_http_400(exc):
                raise OlogResponseError(
                    "Olog rejected the entry (HTTP 400): a logbook or tag does not exist, the "
                    "title is empty, in_reply_to does not identify an existing entry, or an "
                    "attachment is malformed / HEIC (the one file type Olog refuses)."
                ) from cause
            if http_status(exc) == 413:
                raise OlogResponseError(
                    "Olog rejected the entry (HTTP 413): an attachment or the request exceeds "
                    "the server's size limit (maxFileSize / maxRequestSize)."
                ) from cause
            if http_status(exc) == 401:
                raise OlogResponseError(
                    "Olog authentication failed (HTTP 401): check the write service-account "
                    "credentials (EPICS_MCP_OLOG_WRITE_USER / _PASSWORD)."
                ) from cause
            raise
        # S11: the write response must be the created log entry, any other non-empty dict used
        # to be projected as a fabricated write confirmation.
        which = "PUT /logs/multipart (create)" if attachments else "PUT /logs (create)"
        return _expand_log_entry(_require_entry(data, which))

    def _put_multipart(
        self,
        body: dict[str, object],
        attachments: list[AttachmentUpload],
        params: dict[str, str],
    ) -> object:
        """Send a create-with-attachments as multipart/form-data to ``PUT /logs/multipart`` (OA1).

        Mirrors CS-Studio's own client (``OlogHttpClient.save`` → ``HttpRequestMultipartBody``): the
        log JSON gains an ``attachments`` array (one ``{id, filename, fileMetadataDescription}`` per
        file, paired to a ``files`` part by ``filename``) and is sent as a ``logEntry`` text part
        plus
        one ``files`` part per attachment. ``markup=commonmark`` is added so inline-image markup
        renders (the JSON create path deliberately keeps its byte-identical no-markup behaviour).
        Uses
        ``_write_session`` for the same non-idempotent-PUT reasons as the JSON path; ``requests``
        sets
        the ``Content-Type`` boundary itself, so no manual one is passed.
        """
        log_json = dict(body)
        meta, file_parts = _attachment_parts(attachments)
        log_json["attachments"] = meta
        files: MultipartFiles = [("logEntry", (None, json.dumps(log_json), "application/json"))]
        files += file_parts
        multipart_params = {**params, "markup": "commonmark"}
        return rest_put_multipart(
            self._write_session,
            f"{self.base_url}/logs/multipart",
            files,
            self.timeout,
            params=multipart_params,
            headers={"X-Olog-Client-Info": _CLIENT_INFO},
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            allow_redirects=False,
        )

    def _attachment_max_bytes(self, max_bytes: int | None) -> int:
        """The effective download size cap: *max_bytes* if given, else the configured upload cap.

        The download reuses ``olog_attach_max_bytes`` so a fetch is bounded symmetrically with an
        upload (anti-OOM); a caller can pass a SMALLER cap (the base64 lane does, to bound the
        response tokens)."""
        return max_bytes if max_bytes is not None else get_config().olog_attach_max_bytes

    def get_attachment(
        self, log_id: str, filename: str, *, max_bytes: int | None = None
    ) -> tuple[bytes, str | None, str | None]:
        """Download one attachment by log id + filename → ``(bytes, filename, content_type)``.

        ``GET /logs/attachments/{logId}/{name}``: *name* is a single path segment, percent-encoded
        with ``quote(safe="")`` (a space becomes ``%20``, matching CS-Studio's ``URLEncoder`` +
        ``+``→``%20`` fix; a ``+`` is not valid in an Olog path element). No auth (reads are open:
        CS-Studio sends none). The body
        is
        size-capped (*max_bytes*, default the configured upload cap) so a huge object never OOMs.
        """
        # Both path segments are percent-encoded (safe="") so a space / '/' / '+' in either the log
        # id or the filename stays a single path element instead of retargeting the request.
        log_seg = quote(log_id, safe="")
        name_seg = quote(filename, safe="")
        url = f"{self.base_url}/logs/attachments/{log_seg}/{name_seg}"
        return rest_get_bytes(
            self.session,
            url,
            self.timeout,
            max_bytes=self._attachment_max_bytes(max_bytes),
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            allow_redirects=False,
        )

    def get_attachment_by_id(
        self, attachment_id: str, *, max_bytes: int | None = None
    ) -> tuple[bytes, str | None, str | None]:
        """Download one attachment by its GridFS id → ``(bytes, server_filename, content_type)``.

        ``GET /attachment/{id}``: the route an inline image (``![](attachment/<id>)``) resolves to.
        Same redirect refusal and size cap as :meth:`get_attachment`.
        """
        url = f"{self.base_url}/attachment/{quote(attachment_id, safe='')}"
        return rest_get_bytes(
            self.session,
            url,
            self.timeout,
            max_bytes=self._attachment_max_bytes(max_bytes),
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            allow_redirects=False,
        )

    def get_raw_entry(self, log_id: str) -> dict[str, object] | None:
        """The RAW server entry for an update/attach round-trip (OA1b), NOT reshaped.

        ``add_attachment`` and ``update`` must resubmit the entry's FULL content, because the
        server's ``POST /logs/multipart`` runs a destructive ``updateLog`` (prunes any attachment
        not resubmitted, overwrites the fields). Returns ``None`` on a definitive 404
        (unknown/deleted id); a 2xx that is not a readable log entry raises (never a fabricated
        round-trip source)."""
        try:
            data = self._get(f"{self.base_url}/logs/{quote(log_id, safe='')}", {})
        except OlogResponseError as exc:
            if is_http_404(exc):
                return None
            raise
        return _require_entry(data, "GET /logs/{id} (raw round-trip source)")

    def add_attachment(
        self,
        log_id: str,
        raw_entry: dict[str, object],
        uploads: list[AttachmentUpload],
        inline_markup: str = "",
    ) -> dict[str, object]:
        """Attach *uploads* to an EXISTING entry (OA1b, ``POST /logs/multipart``) and return the
        result whole (+ derived fields).

        ``POST /logs/multipart`` is NOT additive: it runs the full ``updateLog``, which
        ``retainAll``-prunes every attachment not resubmitted and overwrites
        title/description/source/level/logbooks/tags/properties with the submitted values
        (LogResource.java:537-558). So this ROUND-TRIPS *raw_entry* verbatim, existing attachment
        metadata re-listed so ``retainAll`` keeps them (the bytes stay server-side, nothing is
        resent), and every overwrite-field carried through, and only APPENDS the new attachment
        metadata + the new ``files`` parts. The write is thus additive for CONTENT: nothing pruned,
        no content
        field wiped.

        ⚠️ That retention is **filename-keyed, not id-keyed** (``Attachment.compareTo`` inside a
        ``TreeSet``; see :func:`attachment_round_trip`), an earlier version of this docstring said
        "equality is by id", wrong about the mechanism, and what let a matching bug hide.
        An entry whose attachments cannot survive that match is REFUSED here rather than attached
        to, because the attach would silently drop one of the existing files.

        *inline_markup* (from an ``embed_image_base64``) is appended to the round-tripped ``source``
        so the new inline image renders; ``markup=commonmark`` regenerates ``description`` from
        ``source`` server-side. *raw_entry* comes from :meth:`get_raw_entry`."""
        existing_meta, unsafe = attachment_round_trip(raw_entry)
        if unsafe:
            raise OlogRoundTripUnsafe(
                "Refusing to attach to this Olog entry: its existing attachments cannot survive "
                f"the required round-trip ({', '.join(unsafe)}). Olog matches attachments by "
                "filename (case-insensitively), so one of them would be silently dropped."
            )
        new_meta, file_parts = _attachment_parts(uploads)
        source = raw_entry.get("source")
        if inline_markup:
            source = (source if isinstance(source, str) else "") + inline_markup
        log_json: dict[str, object] = {
            # LogResource.updateLog(multipart) rejects a null/absent/negative id (:577), a NUMBER.
            "id": int(log_id),
            "title": raw_entry.get("title"),
            "description": raw_entry.get("description"),
            "source": source,
            "level": raw_entry.get("level"),
            # Round-trip verbatim: updateLog STORES exactly what is sent (a reshape would drop the
            # logbook/tag owner+state and any property). The Log Entry Group property is preserved
            # server-side regardless, updateLog re-adds the persisted one.
            "logbooks": raw_entry.get("logbooks"),
            "tags": raw_entry.get("tags"),
            "properties": raw_entry.get("properties"),
            "attachments": existing_meta + new_meta,
        }
        files: MultipartFiles = [("logEntry", (None, json.dumps(log_json), "application/json"))]
        files += file_parts
        data = rest_post_multipart(
            self._write_session,
            f"{self.base_url}/logs/multipart",
            files,
            self.timeout,
            params={"markup": "commonmark"},
            headers={"X-Olog-Client-Info": _CLIENT_INFO},
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            allow_redirects=False,
        )
        return _expand_log_entry(_require_entry(data, "POST /logs/multipart (add attachment)"))

    def update_log_entry(
        self,
        raw_entry: dict[str, object],
        *,
        title: str | None = None,
        description: str | None = None,
        level: str | None = None,
        logbooks: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        """Edit an EXISTING entry's fields (OA3) and return the result whole (+ derived fields).

        Sent as ``POST /logs/multipart`` with the ``logEntry`` part and NO file parts. That is the
        same server core as OA1b's attach, the multipart handler delegates straight into the JSON
        ``updateLog`` (LogResource.java:602), so this reuses the ONE multipart write path that is
        already live-verified instead of introducing a second, unprobed endpoint.

        ``updateLog`` is a FULL REPLACE: every editable field is overwritten with what is submitted,
        and a field left out is set to NULL (LogResource.java:550-558); every attachment missing
        from the submitted list is pruned (``retainAll``, :537-538). So this ROUND-TRIPS the whole
        *raw_entry*, title/source/description/level/logbooks/tags/properties plus the existing
        attachment metadata, and overlays ONLY the fields the caller passed (``None`` = keep
        unchanged). Nothing is lost that the caller did not deliberately change; that is the entire
        reason a field edit needs the full entry.

        A body edit is written to ``source``, NOT ``description``: under ``markup=commonmark`` the
        server regenerates ``description`` from ``source`` (CommonmarkCleaner.java:46-49), so a new
        ``description`` sitting next to a stale ``source`` would be silently overwritten and the
        edit would vanish. Both are set to the new text, so the two never disagree even if the
        markup step were skipped.

        Attachment retention is **filename-keyed**, not id-keyed (see
        :func:`unroundtrippable_attachment_filenames`); an entry whose attachments cannot survive
        that match is refused up front by the service, and re-checked here as a backstop.

        Caveats worth knowing at the call site: the server re-sets ``owner`` to the write service
        account on EVERY update (LogResource.java:550), the original author survives only in the
        archived version (:531), which this server cannot read, so recovery is manual, and it does
        NOT validate logbook/tag existence on update (unlike
        create) nor the ``level`` on ANY write path, which is why the service checks edited names
        and the level itself (see ``_reject_unknown_level``). *raw_entry* comes from
        :meth:`get_raw_entry`; the server's own numeric id is round-tripped
        verbatim (the multipart handler reads the id from the BODY, :577, so no path/body id
        mismatch is possible).
        """
        # Payload and verdict from ONE pass, so the re-listed set and the refusal can never
        # disagree (an omitted attachment IS an offender by construction).
        existing_meta, unsafe = attachment_round_trip(raw_entry)
        if unsafe:
            raise OlogRoundTripUnsafe(
                "Refusing to update this Olog entry: its attachments cannot survive the required "
                f"round-trip ({', '.join(unsafe)}). Olog matches attachments by filename "
                "(case-insensitively), so a duplicate, missing or unreadable one would be "
                "silently dropped by the update."
            )
        if description is not None:
            # The body edit IS the new source (body-of-truth under markup=commonmark); description
            # is set alongside so the two cannot disagree.
            new_source: object = description
            new_description: object = description
        else:
            new_source = raw_entry.get("source")
            raw_description = raw_entry.get("description")
            # Never send a null description: Olog's save path dereferences it without a null-guard
            # (the same reason create always sends a present string).
            new_description = raw_description if isinstance(raw_description, str) else ""
        log_json: dict[str, object] = {
            # Verbatim server id: the multipart handler rejects a null/absent/negative id (:577).
            "id": raw_entry.get("id"),
            "title": title if title is not None else raw_entry.get("title"),
            "description": new_description,
            "source": new_source,
            "level": level if level is not None else raw_entry.get("level"),
            # Edited names are reshaped to the wire form; UNedited lists ride through verbatim so a
            # reshape cannot drop a logbook/tag owner or state the server stored.
            "logbooks": (
                [{"name": name} for name in logbooks]
                if logbooks is not None
                else raw_entry.get("logbooks")
            ),
            "tags": (
                [{"name": name} for name in tags] if tags is not None else raw_entry.get("tags")
            ),
            # Full replace: omitting properties would wipe every one of them. The Log Entry Group
            # property is server-managed (stripped from the payload and re-added, :540-548).
            "properties": raw_entry.get("properties"),
            "attachments": existing_meta,
        }
        files: MultipartFiles = [("logEntry", (None, json.dumps(log_json), "application/json"))]
        data = rest_post_multipart(
            # The dedicated write session (no retries, env-independent), as for every Olog write.
            self._write_session,
            f"{self.base_url}/logs/multipart",
            files,
            self.timeout,
            params={"markup": "commonmark"},
            headers={"X-Olog-Client-Info": _CLIENT_INFO},
            conn_exc=OlogConnectionError,
            resp_exc=OlogResponseError,
            # Never follow a redirect on a write: the gate approved THIS host.
            allow_redirects=False,
        )
        return _expand_log_entry(_require_entry(data, "POST /logs/multipart (update)"))
