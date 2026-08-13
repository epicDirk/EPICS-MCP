"""Offline tests for the Olog ENTRY-EDIT surface (OA3), helper, client, gate, service.

No network. Olog's update is destructive (it prunes any attachment not resubmitted and NULLS any
field not sent), so every test here defends the round-trip that makes a field edit safe. Red-proofs
for the guards:

* a non-numeric log_id and an all-unchanged call are refused before any I/O,
* an empty title is refused (Olog's update, unlike create, would silently accept it),
* UNedited fields + attachments + properties survive verbatim; only edited fields change,
* a body edit is written to `source` (not `description`, which the server regenerates),
* the logbook allowlist is keyed on the UNION of current+resulting logbooks (move in AND out),
* an entry may not be left without a logbook,
* an entry whose attachments have duplicate/missing filenames is refused (filename-keyed retention),
* unknown logbook/tag names are refused (Olog's update does not validate them),
* a legacy entry without a raw `source` yields a warning.

All host/URL/person/file tokens are SYNTHETIC (facility-agnostic guard).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

import epics_mcp.config as config_module
import epics_mcp.olog_safety as olog_safety_module
import epics_mcp.services.checkers_olog as checkers_module
import epics_mcp.services.olog_client as olog_client_module
from epics_mcp.config import EpicsConfig
from epics_mcp.errors import EpicsError, OlogWriteDeniedError
from epics_mcp.services import _http
from epics_mcp.services.checkers import query_olog_update
from epics_mcp.services.olog_client import (
    OlogClient,
    attachment_round_trip,
    unroundtrippable_attachment_filenames,
)
from epics_mcp.services.olog_exceptions import (
    OlogConnectionError,
    OlogResponseError,
    OlogRoundTripUnsafe,
)

_AUDIT_LOGGER = "epics_mcp.olog_audit"
_LOOPBACK = "http://localhost:8080/Olog"

# A canonical raw entry: every field the destructive update would otherwise wipe.
_RAW_ENTRY: dict[str, object] = {
    "id": 17,
    "title": "existing title",
    "description": "raw body",
    "source": "raw **body**",
    "level": "Info",
    "owner": "someone",
    "logbooks": [{"name": "Ops", "owner": None, "state": "Active"}],
    "tags": [{"name": "shift"}],
    "properties": [{"name": "Log Entry Group", "attributes": [{"name": "id", "value": "g1"}]}],
    "attachments": [{"id": "old1", "filename": "old1_a.png", "fileMetadataDescription": "image"}],
}

#: The ordered phrase the read-modify-write signpost carries on every surface that teaches the
#: round trip. THE ORDER IS THE WHOLE GUARD: the bare word "source" already stands in both read
#: descriptions today, in the flat field list "title, description, owner, source and properties",
#: so an assertion on that word alone is green before anyone writes a thing.
_SIGNPOST = "read source, not description"
#: The refuted wording. It carries every token of the correct one, so it needs its own assertion.
_SIGNPOST_REFUTED = "read description, not source"


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset the config + Olog-write-gate singletons for each test (so each builds fresh)."""
    config_module._config = None
    olog_safety_module._olog_safety = None
    yield
    config_module._config = None
    olog_safety_module._olog_safety = None


def _write_config(
    *,
    olog_url: str = _LOOPBACK,
    olog_write_logbooks: str = "Ops",
    olog_write_rate_limit: int = 5,
) -> EpicsConfig:
    """A config with Olog write enabled against the loopback sandbox (facility-agnostic tokens)."""
    return EpicsConfig(
        olog_url=olog_url,
        allow_olog_write=True,
        olog_write_logbooks=olog_write_logbooks,
        olog_write_rate_limit=olog_write_rate_limit,
        olog_write_user="epics-pv-logbook-svc",
        olog_write_password="pw",
    )


def _set_config(**overrides: object) -> None:
    """Install a fresh config singleton."""
    config_module._config = EpicsConfig(**overrides)  # type: ignore[arg-type]


# ======================================================================================
# Helper: which attachments cannot survive the filename-keyed round-trip
# ======================================================================================


class TestUnroundtrippableFilenames:
    def test_clean_entry_round_trips(self) -> None:
        entry: dict[str, object] = {
            "attachments": [{"id": "1", "filename": "a.png"}, {"id": "2", "filename": "b.png"}]
        }
        assert unroundtrippable_attachment_filenames(entry) == []

    def test_case_insensitive_collision_is_flagged(self) -> None:
        # RED-PROOF (guard i): Attachment.compareTo uses filename.compareToIgnoreCase inside a
        # TreeSet, so "a.png" and "A.PNG" collapse to ONE element, the id never disambiguates them.
        entry: dict[str, object] = {
            "attachments": [{"id": "1", "filename": "a.png"}, {"id": "2", "filename": "A.PNG"}]
        }
        assert unroundtrippable_attachment_filenames(entry) == ["A.PNG"]

    def test_missing_filename_is_flagged(self) -> None:
        entry: dict[str, object] = {"attachments": [{"id": "1"}]}
        assert unroundtrippable_attachment_filenames(entry) == ["<no filename>"]

    def test_no_attachments_is_clean(self) -> None:
        assert unroundtrippable_attachment_filenames({"id": 1}) == []

    def test_id_less_attachment_is_flagged(self) -> None:
        # RED-PROOF: the payload builder cannot resubmit a record without an id, so the guard MUST
        # flag it. When guard and builder were two separate passes this shape passed as "safe" and
        # was then silently omitted from the payload → pruned by the server (adversarial review
        # finding, probe-measured). This is the regression test for that exact drift.
        entry: dict[str, object] = {"attachments": [{"filename": "plot.png"}]}
        assert unroundtrippable_attachment_filenames(entry) == ["plot.png"]

    def test_non_list_attachments_is_flagged(self) -> None:
        # Present but not enumerable: submitting nothing would prune EVERYTHING, so refuse.
        entry: dict[str, object] = {"attachments": {"0": {"id": "1", "filename": "a.png"}}}
        assert unroundtrippable_attachment_filenames(entry) == ["<unreadable attachment list>"]

    def test_non_dict_attachment_is_flagged(self) -> None:
        entry: dict[str, object] = {"attachments": ["plot.png"]}
        assert unroundtrippable_attachment_filenames(entry) == ["<unreadable attachment>"]

    def test_sharp_s_is_not_a_false_collision(self) -> None:
        # casefold() folds ß→ss and would refuse this pair; Java's compareToIgnoreCase folds per
        # character and does not, so these two DO round-trip, refusing them would be a false alarm.
        entry: dict[str, object] = {
            "attachments": [
                {"id": "1", "filename": "straße.txt"},
                {"id": "2", "filename": "strasse.txt"},
            ]
        }
        assert unroundtrippable_attachment_filenames(entry) == []

    def test_payload_and_verdict_can_never_disagree(self) -> None:
        # The structural invariant that replaced the drift: every attachment the payload omits is,
        # by construction, reported as an offender: "guard says safe" implies "payload complete".
        entry: dict[str, object] = {
            "attachments": [
                {"id": "1", "filename": "keep.png"},
                {"filename": "no-id.png"},
                {"id": "3"},
                "junk",
            ]
        }
        meta, offenders = attachment_round_trip(entry)
        assert [m["filename"] for m in meta] == ["keep.png"]
        assert len(offenders) == 3  # nothing is dropped quietly; all three are refused


# ======================================================================================
# Client: the round-trip body
# ======================================================================================


def _capture_post(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the multipart write and hand back what would have gone on the wire."""
    captured: dict[str, Any] = {}

    def fake_post(
        session: object, url: str, files: _http.MultipartFiles, *a: object, **k: object
    ) -> object:
        captured["url"] = url
        captured["files"] = files
        captured["params"] = k.get("params")
        captured["allow_redirects"] = k.get("allow_redirects")
        return {"id": 17, "title": "updated", "logbooks": ["Ops"]}

    monkeypatch.setattr(olog_client_module, "rest_post_multipart", fake_post)
    return captured


def _sent_log_json(captured: dict[str, Any]) -> dict[str, Any]:
    """The parsed logEntry part of the captured multipart request."""
    name0, (fn0, body0, ct0) = captured["files"][0]
    assert name0 == "logEntry" and fn0 is None and ct0 == "application/json"
    parsed: dict[str, Any] = json.loads(body0)
    return parsed


class TestClientUpdate:
    def test_unedited_fields_round_trip_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard c, inverse): a title-only edit must leave EVERY other field byte-for-byte
        # as it was, updateLog is a full replace, so anything not resubmitted is nulled.
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry(_RAW_ENTRY, title="new title")
        log_json = _sent_log_json(captured)
        assert log_json["title"] == "new title"  # the ONE edited field
        assert log_json["source"] == "raw **body**"
        assert log_json["description"] == "raw body"
        assert log_json["level"] == "Info"
        assert log_json["logbooks"] == [{"name": "Ops", "owner": None, "state": "Active"}]
        assert log_json["tags"] == [{"name": "shift"}]
        assert log_json["properties"] == _RAW_ENTRY["properties"]
        assert log_json["id"] == 17  # numeric, read from the BODY by the multipart handler

    def test_attachments_are_relisted_so_retainall_keeps_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (guard g): drop the existing_meta round-trip and the server's retainAll prunes
        # every attachment on a plain field edit.
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry(_RAW_ENTRY, title="new title")
        log_json = _sent_log_json(captured)
        assert log_json["attachments"] == [
            {"id": "old1", "filename": "old1_a.png", "fileMetadataDescription": "image"}
        ]
        assert len(captured["files"]) == 1  # logEntry only, no file parts, no bytes re-sent

    def test_body_edit_is_written_to_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard j): under markup=commonmark the server regenerates description FROM
        # source, so writing the new body to description next to a stale source would lose the edit.
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry(_RAW_ENTRY, description="new **body**")
        log_json = _sent_log_json(captured)
        assert log_json["source"] == "new **body**"
        assert log_json["description"] == "new **body**"  # kept consistent, never stale
        assert captured["params"] == {"markup": "commonmark"}

    def test_edited_logbooks_and_tags_are_reshaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry(_RAW_ENTRY, logbooks=["Ops", "Commissioning"], tags=[])
        log_json = _sent_log_json(captured)
        assert log_json["logbooks"] == [{"name": "Ops"}, {"name": "Commissioning"}]
        assert log_json["tags"] == []  # explicit clear

    def test_write_never_follows_a_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry(_RAW_ENTRY, title="new title")
        assert captured["allow_redirects"] is False
        assert captured["url"].endswith("/logs/multipart")

    def test_legacy_entry_without_source_sends_a_present_description(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Olog's save path dereferences description without a null-guard: never send null.
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry({"id": 9, "title": "t"}, title="new")
        log_json = _sent_log_json(captured)
        assert log_json["description"] == ""
        assert log_json["source"] is None

    def test_refuses_unroundtrippable_attachments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard i, client backstop): even if the service check were bypassed, the client
        # refuses rather than silently letting the server drop a colliding attachment.
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        entry = dict(_RAW_ENTRY)
        entry["attachments"] = [
            {"id": "1", "filename": "plot.png"},
            {"id": "2", "filename": "PLOT.PNG"},
        ]
        with pytest.raises(OlogRoundTripUnsafe, match="filename"):
            client.update_log_entry(entry, title="new")
        assert captured == {}  # nothing was written

    def test_edited_level_overlays_the_raw_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (the level overlay): every OTHER update_log_entry call in this file omits
        # `level`, so only the ELSE branch of the overlay was ever executed, sabotaging it to
        # `raw_entry.get("level")` left the whole suite green. The value must differ from
        # _RAW_ENTRY["level"] ("Info"), or both branches produce the same payload and the mutant
        # survives. The sibling service-level assertion (TestServiceUpdate) cannot cover this: it
        # monkeypatches OlogClient away, so the wire payload is never built.
        captured = _capture_post(monkeypatch)
        client = OlogClient(_LOOPBACK)
        client.update_log_entry(_RAW_ENTRY, level="Problem")
        log_json = _sent_log_json(captured)
        assert log_json["level"] == "Problem"  # the caller's level wins over the raw entry's
        assert log_json["title"] == "existing title"  # ...and nothing else moved
        assert log_json["source"] == "raw **body**"


# ======================================================================================
# OQ11: the signpost that keeps a read-modify-write from destroying the entry it edits
# ======================================================================================


class TestReadModifyWriteSignpost:
    """A caller who reads an entry, appends to it and writes it back destroys it, and nothing on
    the wire used to say which of the two same-named body fields is the safe one to read.

    The behaviour the signpost points at is proven next door, by
    ``TestClientUpdate.test_body_edit_is_written_to_source``, and ``_RAW_ENTRY`` carries the very
    difference the signpost is about (``description="raw body"`` beside ``source="raw **body**"``).
    That the two fields diverge on a real server, and what that divergence does and does not
    prove, is measured in ONE place, the docstring of
    ``services.olog_client._expand_log_entry``; it is not restated here, because a figure copied
    is a figure that drifts.

    READ OFF THE WIRE, never off the source text. The field description is assembled from two
    adjacent string literals, so grepping the source for the joined phrase does not match the
    ``.py`` file at all: it matches stale ``__pycache__`` instead, which reads like a hit.
    """

    @pytest.mark.asyncio
    async def test_the_signpost_reaches_the_wire_on_every_surface(self) -> None:
        from epics_mcp.server import mcp

        tools = {tool.name: tool.to_mcp_tool() for tool in await mcp.list_tools()}
        # The premise first. A tool that fell off the wire has to SAY so; without this the test
        # dies of a KeyError whose cause nobody can read. All three are registered
        # unconditionally, so this holds in the core-only lane as well as the full one.
        carriers = ("update_log_entry", "search_logbook", "get_log_entry")
        missing = [name for name in carriers if name not in tools]
        assert not missing, f"not on the wire: {missing}, so this test cannot make its claim"

        # The exact FIELD node on the write side, never the serialised tool: the same sentence
        # moved into a neighbouring field description would otherwise pass unnoticed.
        write_field = tools["update_log_entry"].inputSchema["properties"]["description"]
        surfaces = {
            "update_log_entry.description (field)": write_field.get("description", ""),
            "search_logbook (tool description)": tools["search_logbook"].description or "",
            "get_log_entry (tool description)": tools["get_log_entry"].description or "",
        }
        for where, wire_text in surfaces.items():
            # Whitespace-normalised: the two read descriptions are wrapped docstrings, so asserting
            # on the raw text would pin the line breaks rather than the claim. Named per surface,
            # because a signpost on two of the three is the failure this loop exists to find.
            text = " ".join(wire_text.split()).lower()
            assert _SIGNPOST in text, (
                f"{where}: the read-modify-write signpost is gone. A caller who reads an entry and "
                f"writes its description back destroys the markup. Wire text was: {text!r}"
            )
            assert _SIGNPOST_REFUTED not in text, (
                f"{where}: the refuted wording is back. description is the server's RENDERED plain "
                "text, regenerated from source, so writing it back drops markup and inline images. "
                "See TestClientUpdate.test_body_edit_is_written_to_source."
            )


# ======================================================================================
# _olog_error_code: the discrete token reported to the caller AND written to the audit
# ======================================================================================


class TestOlogErrorCode:
    """The mapper had NO direct test, which is why its fallback branch could stay wrong."""

    def test_refusals_carry_their_own_code_not_internal(self) -> None:
        # A permanent refusal that never wraps an HTTP response; it used to fall through to
        # INTERNAL, i.e. "transient, try again".
        assert checkers_module._olog_error_code(OlogRoundTripUnsafe("x")) == "INVALID_INPUT"

    def test_connection_and_response_branches_survive_the_new_one(self) -> None:
        # ORDER REGRESSION: OlogConnectionError and OlogResponseError are themselves OlogError
        # subclasses, so a naively hoisted `isinstance(exc, OlogError)` branch would swallow both:
        # and with them the HTTP-status resolution. This pins the precedence.
        assert checkers_module._olog_error_code(OlogConnectionError("x")) == "OLOG_CONNECTION_ERROR"
        assert checkers_module._olog_error_code(OlogResponseError("x")) == "OLOG_RESPONSE_ERROR"

    def test_unknown_exception_still_falls_back_to_internal(self) -> None:
        # The fallback is still right for what it was meant for: something genuinely unclassified.
        assert checkers_module._olog_error_code(RuntimeError("boom")) == "INTERNAL"

    def test_code_never_carries_the_exception_message(self) -> None:
        # SEC-5: the audit is metadata-only. A code derived from the message would leak free text
        # into the audit trail through the back door.
        code = checkers_module._olog_error_code(OlogRoundTripUnsafe("secret file name.png"))
        assert "secret" not in code and "png" not in code


# ======================================================================================
# Service: guards, gate, audit
# ======================================================================================


class _UpdateCaptureClient:
    """A fake OlogClient for update service tests: a canned raw entry,
    canned logbook/tag listings, and a recording update_log_entry."""

    raw: ClassVar[dict[str, object] | None] = None
    logbooks_available: ClassVar[list[str]] = ["Ops", "Commissioning"]
    tags_available: ClassVar[list[str]] = ["shift", "fault"]
    levels_available: ClassVar[list[str]] = ["Info", "Problem", "Request"]
    calls: ClassVar[dict[str, Any]] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def get_raw_entry(self, log_id: str) -> dict[str, object] | None:
        return _UpdateCaptureClient.raw

    def list_logbooks(self) -> list[str]:
        return _UpdateCaptureClient.logbooks_available

    def list_tags(self) -> list[str]:
        return _UpdateCaptureClient.tags_available

    def list_log_levels(self) -> tuple[list[str], str | None, str | None]:
        # Same 3-tuple shape as the real client, the write-side check reads [0] (the names).
        return _UpdateCaptureClient.levels_available, "Info", None

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
        _UpdateCaptureClient.calls = {
            "raw": raw_entry,
            "title": title,
            "description": description,
            "level": level,
            "logbooks": logbooks,
            "tags": tags,
        }
        return {"id": 17, "title": "updated", "logbooks": ["Ops"]}


def _install_fake(monkeypatch: pytest.MonkeyPatch, raw: dict[str, object] | None = None) -> None:
    _UpdateCaptureClient.raw = _RAW_ENTRY if raw is None else raw
    _UpdateCaptureClient.logbooks_available = ["Ops", "Commissioning"]
    _UpdateCaptureClient.tags_available = ["shift", "fault"]
    _UpdateCaptureClient.levels_available = ["Info", "Problem", "Request"]
    _UpdateCaptureClient.calls = {}
    monkeypatch.setattr(checkers_module, "OlogClient", _UpdateCaptureClient)


class TestServiceUpdate:
    @pytest.mark.asyncio
    async def test_disabled_without_url(self) -> None:
        _set_config(olog_url="")
        result = await query_olog_update("17", title="x")
        assert result["enabled"] is False
        assert result["updated"] is False

    @pytest.mark.asyncio
    async def test_needs_at_least_one_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard f): an all-unchanged call would still rewrite the entry server-side
        # (new modifyDate + an archived version) for no reason, refuse before any I/O.
        config_module._config = _write_config()
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17")
        assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_numeric_id_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard b): refused before any network, and isascii() keeps non-ASCII digits
        # (which int() handles inconsistently) out too.
        config_module._config = _write_config()
        _install_fake(monkeypatch)
        for bad in ("not-a-number", "../logbooks", "٣"):
            with pytest.raises(EpicsError) as exc:
                await query_olog_update(bad, title="x")
            assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_empty_title_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Olog's update, unlike create, does NOT reject an empty title, so we must.
        config_module._config = _write_config()
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", title="   ")
        assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_missing_entry_is_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_module._config = _write_config()
        _install_fake(monkeypatch)
        _UpdateCaptureClient.raw = None
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", title="x")
        assert exc.value.error_code == "OLOG_HTTP_404"

    @pytest.mark.asyncio
    async def test_failed_write_is_audited_then_reraised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # RED-PROOF: a write that raises must still leave a FAILED audit record, an attempted
        # write that vanishes from the audit trail is the "silent mistake" the gate exists against.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)

        def boom(*args: object, **kwargs: object) -> dict[str, object]:
            raise OlogResponseError("Olog rejected the update (HTTP 400)")

        monkeypatch.setattr(_UpdateCaptureClient, "update_log_entry", boom)
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER), pytest.raises(EpicsError):
            await query_olog_update("17", title="corrected title")
        assert "event=FAILED" in caplog.text
        assert "caller=update_log_entry" in caplog.text
        assert "corrected title" not in caplog.text  # metadata only, never the text

    @pytest.mark.asyncio
    async def test_failed_edit_audit_names_the_entry(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # RED-PROOF: the server archives and mutates BEFORE answering, so a timeout leaves an
        # APPLIED edit in front of a client that sees FAILED. Without the id, the only record of the
        # attempt cannot say WHICH entry may now be altered. (audit_write_failed omitted entry_id on
        # a create-specific rationale, "none exists for a failed create", that does not hold for
        # an edit.)
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)

        def boom(*args: object, **kwargs: object) -> dict[str, object]:
            raise OlogResponseError("Olog timed out (HTTP 504)")

        monkeypatch.setattr(_UpdateCaptureClient, "update_log_entry", boom)
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER), pytest.raises(EpicsError):
            await query_olog_update("17", title="corrected title")
        assert "event=FAILED" in caplog.text
        assert "entry_id=17" in caplog.text
        assert "owner=" not in caplog.text  # SEC-5 unchanged: the id, never the owner

    @pytest.mark.asyncio
    async def test_refusal_error_codes_are_not_internal(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # RED-PROOF (OQ5): a REFUSAL reported as INTERNAL reads as a transient server fault and
        # invites a retry, and every retry burns a rate token and writes another FAILED line for a
        # write that never happened. Both the raised error AND the audit must carry the honest code,
        # which is why the fix sits in _olog_error_code (the audit calls it directly) and not in an
        # except clause.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)

        def boom(*args: object, **kwargs: object) -> dict[str, object]:
            raise OlogRoundTripUnsafe("attachments would not survive the round-trip")

        monkeypatch.setattr(_UpdateCaptureClient, "update_log_entry", boom)
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER), pytest.raises(EpicsError) as exc:
            await query_olog_update("17", title="x")
        assert exc.value.error_code == "INVALID_INPUT"
        assert "error_code=INVALID_INPUT" in caplog.text
        assert "INTERNAL" not in caplog.text

    @pytest.mark.asyncio
    async def test_gate_deny_when_current_logbook_not_allowlisted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (guard d): the entry's OWN logbooks must be covered by the allowlist.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["logbooks"] = [{"name": "SecretBook"}]
        _install_fake(monkeypatch, raw=entry)
        with pytest.raises(OlogWriteDeniedError, match="allowlist"):
            await query_olog_update("17", title="x")
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_gate_deny_when_moving_INTO_a_non_allowlisted_logbook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (guard e, direction 1): gating on the CURRENT logbooks alone would let a caller
        # move an entry into a logbook they may not write to.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        _UpdateCaptureClient.logbooks_available = ["Ops", "SecretBook"]
        with pytest.raises(OlogWriteDeniedError, match="allowlist"):
            await query_olog_update("17", logbooks=["SecretBook"])
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_gate_deny_when_pulling_OUT_of_a_non_allowlisted_logbook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (guard e, direction 2): gating on the RESULTING logbooks alone would let a
        # caller quietly remove an entry from a logbook they may not write to.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["logbooks"] = [{"name": "Ops"}, {"name": "SecretBook"}]
        _install_fake(monkeypatch, raw=entry)
        with pytest.raises(OlogWriteDeniedError, match="allowlist"):
            await query_olog_update("17", logbooks=["Ops"])
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_entry_may_not_be_left_without_a_logbook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (guard h): the union would still be non-empty, so the gate's own non-empty
        # check cannot catch this, the EFFECTIVE set has to be checked separately.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", logbooks=[])
        assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_refuses_unroundtrippable_attachments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (guard i, service): refused BEFORE the gate, so no rate token is burnt either.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["attachments"] = [
            {"id": "1", "filename": "plot.png"},
            {"id": "2", "filename": "PLOT.PNG"},
        ]
        _install_fake(monkeypatch, raw=entry)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", title="x")
        assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_unknown_logbook_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard l): Olog's update stores an unknown name silently as a phantom ref.
        config_module._config = _write_config(olog_write_logbooks="Ops,Ghost")
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", logbooks=["Ghost"])
        assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_unknown_tag_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", tags=["nosuchtag"])
        assert exc.value.error_code == "INVALID_INPUT"
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_unknown_level_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF: Olog validates the level on NEITHER write path, "Urgnet" comes back HTTP 200
        # and the entry then matches no level filter at all. level was the one of the three
        # server-managed fields that went through unchecked.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", level="Urgnet")
        assert exc.value.error_code == "INVALID_INPUT"
        assert "Urgnet" in str(exc.value)
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_blank_level_refused_with_its_own_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF: a blank level is not "no change", the server stores it and the entry silently
        # loses its triage level. Distinct message from the vocabulary refusal on purpose: the two
        # failures need different fixes from the caller.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        with pytest.raises(EpicsError) as exc:
            await query_olog_update("17", level="   ")
        assert exc.value.error_code == "INVALID_INPUT"
        assert "empty" in str(exc.value) and "clearing" in str(exc.value)
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_level_match_is_exact_not_search_semantics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A WRITTEN level is a scalar. split_level_values (OR-split + trim) and _unknown_level_note
        # (casefold + wildcards) are READ-side helpers; either one here would wave through values
        # the server stores literally and no filter ever finds again.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        for bad in ("Info,Problem", " Info", "info", "Inf*"):
            with pytest.raises(EpicsError) as exc:
                await query_olog_update("17", level=bad)
            assert exc.value.error_code == "INVALID_INPUT", bad
        assert _UpdateCaptureClient.calls == {}

    @pytest.mark.asyncio
    async def test_legacy_entry_without_source_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED-PROOF (guard k): the server will re-render the visible body through CommonMark.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        del entry["source"]
        _install_fake(monkeypatch, raw=entry)
        result = await query_olog_update("17", title="x")
        warnings = result["warnings"]
        assert isinstance(warnings, list) and "CommonMark" in warnings[0]

    @pytest.mark.asyncio
    async def test_body_edit_does_not_warn_about_missing_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A body edit sets source explicitly, so the legacy trap does not apply.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        del entry["source"]
        _install_fake(monkeypatch, raw=entry)
        result = await query_olog_update("17", description="new body")
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_a_rendered_body_written_back_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RED-PROOF (OQ11): _RAW_ENTRY's description is the rendered plain text of its source, so a
        # new body that STARTS with it is a rendered body going back over the raw one. This is the
        # commonest shape by far: read the entry, append a line, write it back.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        result = await query_olog_update("17", description="raw body\n\nand one more line")
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        # Case-folded: the emphasis capitals are formatting, the claim is the phrase.
        assert any("rendered plain text" in warning.lower() for warning in warnings), warnings

    @pytest.mark.asyncio
    async def test_an_unchanged_rendering_is_reported_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The degenerate shape of the same mistake: the read value handed straight back.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        result = await query_olog_update("17", description="raw body")
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        # Case-folded: the emphasis capitals are formatting, the claim is the phrase.
        assert any("rendered plain text" in warning.lower() for warning in warnings), warnings

    @pytest.mark.asyncio
    async def test_an_entry_whose_rendering_lost_nothing_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A body with no markup renders to itself, so writing the read value back loses NOTHING.
        # Warning there would be the field claiming more than it checks (decision WP).
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["source"] = entry["description"]
        _install_fake(monkeypatch, raw=entry)
        result = await query_olog_update("17", description="raw body and one more line")
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_the_rendering_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A genuinely new body neither equals nor extends the rendering. What the guard checks is
        # that PREFIX and nothing else, so anything past it must stay silent.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        result = await query_olog_update("17", description="a completely different **body**")
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_a_body_merely_CONTAINING_the_rendering_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The discriminator between a PREFIX test and a containment test, and it is the difference
        # between the guard's own wording and a lie: the warning says "STARTS WITH" and declares a
        # mid-body rewrite undetectable. Relaxing ``startswith`` to ``in`` made this shape warn
        # while the whole suite stayed green (adversarial review), so the guard claimed a reach it
        # had disowned in writing. The neighbouring case, a body that does not contain the
        # rendering at all, cannot tell those two apart.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        _install_fake(monkeypatch)
        result = await query_olog_update("17", description="Update: raw body is now obsolete")
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_the_prefix_is_LITERAL_in_every_direction_it_could_be_softened(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The warning SAYS "STARTS WITH" and declares a mid-body rewrite undetectable, so the
        # comparison has to be exactly that, or the wording claims a reach the code disowned.
        # A mutation sweep found the removal of any conjunct caught and every SOFTENING of this one
        # free: case-folded, whitespace-folded, and first-line-only each survived the whole suite.
        # Each row below is a body that a softened comparison would report and a literal one must
        # not, and the third row is the one that matters most: a completely rewritten multi-line
        # body that happens to open on the same line.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["description"] = "Beam lost at 12:00\nsector 3"
        entry["source"] = "Beam lost at 12:00\n**sector 3**"
        _install_fake(monkeypatch, raw=entry)
        softenings = {
            "case-folded": "beam lost at 12:00\nsector 3 and more",
            "whitespace-folded": "  Beam lost at 12:00\nsector 3 and more",
            "first-line-only": "Beam lost at 12:00\nsomething entirely different",
        }
        for softening, body in softenings.items():
            result = await query_olog_update("17", description=body)
            assert "warnings" not in result, (
                f"a {softening} comparison would have reported this body, but the warning claims a "
                f"literal prefix: {body!r}"
            )

    @pytest.mark.asyncio
    async def test_a_caller_who_read_source_is_not_accused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE FALSE POSITIVE THIS GUARD ALMOST SHIPPED WITH, and it sat on the CORRECT path. This
        # server's own add_log_attachment appends the image markup to the END of ``source``, so
        # such an entry's rendering is a PREFIX of its source. A caller who then does exactly what
        # every surface here tells it to, read source and append, hands over a body starting with
        # the rendering as well, and was reported as the mistake it had just avoided.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["description"] = "Beam lost at 12:00"
        entry["source"] = "Beam lost at 12:00\n\n![shot](attachment/uid)"
        _install_fake(monkeypatch, raw=entry)
        correct = "Beam lost at 12:00\n\n![shot](attachment/uid)\n\nUpdate: recovered"
        result = await query_olog_update("17", description=correct)
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_the_same_entry_still_catches_the_rendered_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The positive control for the case above, on the SAME entry: without it, the fix that
        # silenced the false positive could have silenced the finding as well and both would read
        # as one green test.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["description"] = "Beam lost at 12:00"
        entry["source"] = "Beam lost at 12:00\n\n![shot](attachment/uid)"
        _install_fake(monkeypatch, raw=entry)
        result = await query_olog_update(
            "17", description="Beam lost at 12:00\n\nUpdate: recovered"
        )
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        assert any("rendered plain text" in warning.lower() for warning in warnings), warnings

    @pytest.mark.asyncio
    async def test_an_empty_rendering_is_not_a_prefix_of_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # str.startswith("") is True for every string, so an entry with a blank description would
        # make the guard fire on every body edit there is.
        config_module._config = _write_config(olog_write_logbooks="Ops")
        entry = dict(_RAW_ENTRY)
        entry["description"] = ""
        _install_fake(monkeypatch, raw=entry)
        result = await query_olog_update("17", description="a new body")
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_happy_path_passes_edits_and_audits_metadata_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_module._config = _write_config(olog_write_logbooks="Ops,Commissioning")
        _install_fake(monkeypatch)
        with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
            result = await query_olog_update(
                "17", title="corrected title", level="Problem", tags=["fault"]
            )
        assert result["updated"] is True
        # only the edited fields are handed down; the rest stay None = unchanged
        assert _UpdateCaptureClient.calls["title"] == "corrected title"
        assert _UpdateCaptureClient.calls["level"] == "Problem"
        assert _UpdateCaptureClient.calls["tags"] == ["fault"]
        assert _UpdateCaptureClient.calls["description"] is None
        assert _UpdateCaptureClient.calls["logbooks"] is None
        # the raw entry reached the client as the round-trip source
        assert _UpdateCaptureClient.calls["raw"]["id"] == 17
        # audit carries the title LENGTH, never the text
        assert "caller=update_log_entry" in caplog.text
        assert "corrected title" not in caplog.text
        assert f"title_len={len('corrected title')}" in caplog.text
