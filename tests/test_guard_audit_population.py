"""S40 / GQ-403: the sham audit sees a client double wherever pytest installs it for a test.

Until GQ-403 ``scripts/guard_audit.py`` counted a test only when its OWN BODY replaced a client
class, and its docstring named the blind spot: three helpers and one autouse fixture installed
doubles nothing saw (``_install_fake`` in ``test_olog_update.py``, ``_patch_naming_client`` in
``test_diagnose.py``, ``_wire_starved_archiver`` and ``_identity_never_touches_the_network`` in
``test_doctor.py``). This module pins the reach the audit follows now, and each rule has the case
that would go wrong without it.

Two halves:

* a STAND-IN test tree under ``tmp_path``, swapped in through ``guard_audit._TESTS``, with one
  small module per rule. The sources are STRING constants on purpose: ``_claiming_at`` parses this
  very file as part of the real tree, and a ``setattr(..., "DemoClient", ...)`` written as code
  anywhere in its reach here, a test body, a helper a test calls or a fixture, would move the real
  population. Each tree is written completely before the
  first call reads it, because ``_claiming_at`` caches per directory;
* the four measured cases on the REAL tree, each checked for still being what it was chosen for,
  so a refactor of those test modules names the pick instead of turning a proof into a no-op.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import guard_audit  # noqa: E402 - needs sys.path above

# What every stand-in module starts with: a module to patch, a replacement class, and a ``_boom``
# negative control of the house shape. None of it is imported or run; only its syntax is read.
_HEADER = """\
import pytest

from demo import services


class _Fake:
    pass


def _boom(*_args, **_kwargs):
    raise AssertionError("no client may be built")
"""

_AUTOUSE = "autouse _identity_never_touches_the_network"


def _population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modules: dict[str, str]
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    """``{"file::test id": (edge claim, routes)}`` for a stand-in tree holding *modules*."""
    root = tmp_path / "tests"
    root.mkdir()
    for name, source in modules.items():
        body = source if name == "conftest.py" else _HEADER + textwrap.dedent(source)
        (root / name).write_bytes(body.encode("utf-8"))
    monkeypatch.setattr(guard_audit, "_TESTS", root)
    routes = guard_audit.claiming_routes()
    return {
        f"{file}::{test_id}": (edge, routes[(file, test_id)])
        for file, entries in guard_audit.claiming_tests().items()
        for test_id, edge in entries
    }


def test_a_helper_that_installs_a_double_puts_its_caller_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct and two hops deep, named by the FIRST hop; a helper with no double is the control."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_helpers.py": """
            def _install(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def _outer(monkeypatch):
                _install(monkeypatch)

            def _harmless(monkeypatch):
                monkeypatch.setattr(services, "TIMEOUT", 3)

            def test_direct(monkeypatch):
                _install(monkeypatch)

            def test_two_hops(monkeypatch):
                _outer(monkeypatch)

            def test_control(monkeypatch):
                _harmless(monkeypatch)
            """
        },
    )

    assert population == {
        "test_helpers.py::test_direct": (False, ("helper _install",)),
        "test_helpers.py::test_two_hops": (False, ("helper _outer",)),
    }


def test_recursion_terminates_and_a_recursive_installer_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on 2026-09-14, ten test helpers call themselves; the walk remembers its visits."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_recursion.py": """
            def _walk(node):
                return [_walk(child) for child in node]

            def _ping(monkeypatch, depth):
                _pong(monkeypatch, depth)

            def _pong(monkeypatch, depth):
                if depth:
                    _ping(monkeypatch, depth - 1)
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def test_plain_recursion():
                _walk([[], [[]]])

            def test_mutual_recursion_that_installs(monkeypatch):
                _ping(monkeypatch, 2)
            """
        },
    )

    assert population == {
        "test_recursion.py::test_mutual_recursion_that_installs": (False, ("helper _ping",)),
    }


def test_a_boom_is_a_negative_control_only_when_every_double_in_reach_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``_boom`` through a helper stays out; beside a real double from an autouse fixture it does
    not exclude the test any more, because the real double IS installed for it."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_boom.py": """
            def _refuse(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _boom)

            def test_boom_through_a_helper(monkeypatch):
                _refuse(monkeypatch)
            """,
            "test_boom_beside_autouse.py": """
            @pytest.fixture(autouse=True)
            def _off_the_network(monkeypatch):
                monkeypatch.setattr(services, "OtherClient", _Fake)

            def test_boom_in_the_body(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _boom)
            """,
        },
    )

    assert population == {
        "test_boom_beside_autouse.py::test_boom_in_the_body": (
            False,
            ("autouse _off_the_network",),
        ),
    }


def test_fixtures_are_followed_by_request_and_by_chain_but_helper_parameters_are_not_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test parameter requests a fixture, and so does a fixture parameter; a HELPER parameter is
    a value its caller passes, measured on 2026-09-14 in eleven live helpers named like a fixture.
    The name pytest registers (``name=``) is the name a request uses."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_fixtures.py": """
            @pytest.fixture
            def faked(monkeypatch):
                monkeypatch.setattr("demo.services.DemoClient", _Fake)

            @pytest.fixture
            def outer(faked):
                return faked

            @pytest.fixture(name="renamed")
            def _renamed_impl(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def _takes_a_value(faked):
                return faked

            def test_requests_the_fixture(faked):
                pass

            def test_requests_a_chain(outer):
                pass

            def test_requests_by_registered_name(renamed):
                pass

            def test_passes_a_value_named_like_the_fixture():
                _takes_a_value(None)
            """
        },
    )

    assert population == {
        "test_fixtures.py::test_requests_the_fixture": (False, ("fixture faked",)),
        "test_fixtures.py::test_requests_a_chain": (False, ("fixture outer",)),
        "test_fixtures.py::test_requests_by_registered_name": (False, ("fixture renamed",)),
    }


def test_autouse_scope_follows_pytest_module_class_and_conftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module autouse fixture reaches the tests of its classes too; a class autouse fixture does
    not reach the module test beside the class (measured 2026-09-14: five class-level autouse
    fixtures in ``test_cli_init.py``); a ``conftest.py`` autouse fixture reaches every module beside
    it."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "conftest.py": textwrap.dedent(
                """\
                import pytest

                from demo import services


                class _Fake:
                    pass


                @pytest.fixture(autouse=True)
                def _everywhere(monkeypatch):
                    monkeypatch.setattr(services, "SharedClient", _Fake)
                """
            ),
            "test_module_scope.py": """
            @pytest.fixture(autouse=True)
            def _whole_module(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            class TestInAClass:
                def test_method(self):
                    pass
            """,
            "test_class_scope.py": """
            class TestUnderItsOwnFixture:
                @pytest.fixture(autouse=True)
                def _only_this_class(self, monkeypatch):
                    monkeypatch.setattr(services, "DemoClient", _Fake)

                def test_method(self):
                    pass

            def test_beside_the_class():
                pass
            """,
        },
    )

    assert population == {
        "test_module_scope.py::TestInAClass::test_method": (
            False,
            ("autouse _everywhere", "autouse _whole_module"),
        ),
        "test_class_scope.py::TestUnderItsOwnFixture::test_method": (
            False,
            ("autouse _everywhere", "autouse _only_this_class"),
        ),
        "test_class_scope.py::test_beside_the_class": (False, ("autouse _everywhere",)),
    }


def test_a_missing_conftest_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_alone.py": """
            def test_body(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)
            """
        },
    )

    assert population == {"test_alone.py::test_body": (False, ("body",))}


def test_self_methods_resolve_in_the_class_and_a_local_name_shadows_a_module_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``self._install`` is the class method, a bare ``_install`` inside a method is the MODULE
    function (Python looks names up in the globals, not the class), and a name the test binds
    itself is its own: measured, a test in ``test_doctor.py`` defines a local ``_served_404``
    beside the module helper of that name."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_methods.py": """
            def _install(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            class TestMethods:
                def _install(self, monkeypatch):
                    monkeypatch.setattr(services, "OtherClient", _Fake)

                def _harmless(self, monkeypatch):
                    pass

                def test_through_self(self, monkeypatch):
                    self._install(monkeypatch)

                def test_bare_name_is_the_module_function(self, monkeypatch):
                    _install(monkeypatch)

                def test_a_self_method_without_a_double(self, monkeypatch):
                    self._harmless(monkeypatch)

            def test_local_definition_shadows_the_helper(monkeypatch):
                def _install(target):
                    return target

                _install(monkeypatch)
            """
        },
    )

    assert population == {
        "test_methods.py::TestMethods::test_through_self": (False, ("helper self._install",)),
        "test_methods.py::TestMethods::test_bare_name_is_the_module_function": (
            False,
            ("helper _install",),
        ),
    }


def test_the_edge_claim_is_read_from_the_test_never_from_what_it_reaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vocabulary filter orders the READING of candidates by what a test says about itself. A
    helper whose docstring talks about unreadable payloads says nothing about its callers."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_claims.py": '''
            def _install(monkeypatch):
                """Install a double so an unreadable payload never reaches the edge."""
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def test_neutral_name(monkeypatch):
                _install(monkeypatch)

            def test_quiet_name(monkeypatch):
                """The service answered garbage and the caller is told so."""
                _install(monkeypatch)
            '''
        },
    )

    assert population == {
        "test_claims.py::test_neutral_name": (False, ("helper _install",)),
        "test_claims.py::test_quiet_name": (True, ("helper _install",)),
    }


def test_two_classes_with_one_test_name_stay_two_identities_on_the_coverage_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The twin case, driven through ``cmd_sham``: only the test under the double is claiming, and
    the map records a guard-line execution for its twin against the real client alone.

    Keyed by function name, the two tests were one identity, so the twin's execution was credited
    to the claiming test and the candidate vanished from the report. ``test_olog_update.py``
    carries exactly this shape; measured on 2026-09-14 both of its twins execute the same guard
    lines themselves, so the real tree moved no figure, and this stand-in holds the case where only
    one does.
    """
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_twins.py": """
            def _install(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            class TestAgainstTheRealClient:
                def test_an_unreadable_answer_is_refused(self):
                    pass

            class TestUnderTheDouble:
                def test_an_unreadable_answer_is_refused(self, monkeypatch):
                    _install(monkeypatch)
            """
        },
    )
    candidate = "test_twins.py::TestUnderTheDouble::test_an_unreadable_answer_is_refused"
    assert population == {candidate: (True, ("helper _install",))}

    guard = guard_audit.enumerate_targets()[0]
    real_twin = (
        "tests/test_twins.py::TestAgainstTheRealClient::test_an_unreadable_answer_is_refused"
    )
    own_run = f"tests/{candidate}"
    # Two maps, and each catches a different half of the defect. The twin alone executing must NOT
    # remove the candidate: that is the collapse of two tests into one identity. The candidate
    # itself executing MUST remove it: that is the map and the population agreeing on the class, so
    # a fix that kept the class on one side only cannot pass by never matching anything.
    for executed_by, expected_count, stays in ((real_twin, 1, True), (own_run, 0, False)):
        monkeypatch.setattr(
            guard_audit,
            "load_coverage_map",
            lambda _path, hit=executed_by: {(guard.module, guard.lineno): {f"{hit}[case]"}},
        )

        assert guard_audit.main(["guard_audit.py", "sham", "--coverage-db", "synthetic"]) == 0
        reported = capsys.readouterr().err

        assert f"never executing a client-edge line: {expected_count}" in reported, reported
        assert (f"  {candidate}\n" in reported) is stays, (
            f"executed by {executed_by}: the candidate must {'stay' if stays else 'leave'}"
        )


def test_test_identity_keeps_the_class_and_cuts_the_case_at_its_first_bracket() -> None:
    """A case id may contain ``::`` and a pipe; a class or a function name holds no bracket."""
    assert guard_audit.test_identity("tests/test_x.py::TestA::test_b[a::b-'|']") == (
        "test_x.py",
        "TestA::test_b",
    )
    assert guard_audit.test_identity("tests/test_x.py::test_b[c]") == ("test_x.py", "test_b")
    assert guard_audit.test_identity("tests/test_x.py::test_b") == ("test_x.py", "test_b")


def test_fixture_resolution_climbs_class_module_conftest_and_the_nearest_name_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class test reaches a module fixture and a conftest fixture; where one name is defined on
    two levels, only the nearer definition runs, in both directions (QA of GQ-403)."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "conftest.py": textwrap.dedent(
                """\
                import pytest

                from demo import services


                class _Fake:
                    pass


                @pytest.fixture
                def from_conftest(monkeypatch):
                    monkeypatch.setattr(services, "DemoClient", _Fake)
                """
            ),
            "test_levels.py": """
            @pytest.fixture
            def from_module(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            @pytest.fixture
            def shared(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            @pytest.fixture
            def quiet():
                pass

            class TestClimbing:
                def test_module_fixture(self, from_module):
                    pass

                def test_conftest_fixture(self, from_conftest):
                    pass

            class TestNearerIsHarmless:
                @pytest.fixture
                def shared(self):
                    pass

                def test_shared(self, shared):
                    pass

            class TestNearerInstalls:
                @pytest.fixture
                def quiet(self, monkeypatch):
                    monkeypatch.setattr(services, "DemoClient", _Fake)

                def test_quiet(self, quiet):
                    pass
            """,
        },
    )

    assert population == {
        "test_levels.py::TestClimbing::test_module_fixture": (False, ("fixture from_module",)),
        "test_levels.py::TestClimbing::test_conftest_fixture": (False, ("fixture from_conftest",)),
        "test_levels.py::TestNearerInstalls::test_quiet": (False, ("fixture quiet",)),
    }


def test_autouse_is_collected_by_name_and_a_fixture_may_request_the_one_it_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest collects autouse fixtures by NAME and runs the nearest definition of each, so a module
    fixture named like an autouse fixture of conftest.py replaces it; and ``def client(client)``
    receives the fixture one scope further out (QA of GQ-403)."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "conftest.py": textwrap.dedent(
                """\
                import pytest

                from demo import services


                class _Fake:
                    pass


                @pytest.fixture(autouse=True)
                def _net(monkeypatch):
                    monkeypatch.setattr(services, "DemoClient", _Fake)


                @pytest.fixture
                def client(monkeypatch):
                    monkeypatch.setattr(services, "OtherClient", _Fake)
                """
            ),
            "test_override.py": """
            @pytest.fixture(autouse=True)
            def _net():
                pass

            @pytest.fixture
            def client(client):
                return client

            def test_overrides_the_autouse_double():
                pass

            def test_receives_the_overridden_client(client):
                pass
            """,
        },
    )

    assert population == {
        "test_override.py::test_receives_the_overridden_client": (False, ("fixture client",)),
    }


def _three_classes(defining: int) -> str:
    """A module whose autouse fixture requests ``client_patch``, which only class *defining* has."""
    lines = ["@pytest.fixture(autouse=True)", "def _auto(client_patch):"]
    lines += ["    return client_patch", ""]
    for index in range(3):
        lines.append(f"class TestNumber{index}:")
        if index == defining:
            lines += [
                "    @pytest.fixture",
                "    def client_patch(self, monkeypatch):",
                '        monkeypatch.setattr(services, "DemoClient", _Fake)',
                "",
            ]
        lines += ["    def test_x(self):", "        pass", ""]
    return "\n".join(lines)


def test_a_module_autouse_fixture_is_resolved_per_class_even_when_classes_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walker remembers a fixture walk per test SCOPE, keyed by that scope's ``id()``.

    A class scope is built per class and dropped after its tests, so without the walker holding it
    CPython hands a later class the freed address and the walker answers with the earlier class's
    result. Found by the QA of GQ-403 on these two shapes, five runs out of five: the first class
    defining the fixture put a later class under a double it does not have, and the last class
    defining it fell out of the population.
    """
    population = _population(
        tmp_path,
        monkeypatch,
        {"test_first_class.py": _three_classes(0), "test_last_class.py": _three_classes(2)},
    )

    assert population == {
        "test_first_class.py::TestNumber0::test_x": (False, ("autouse _auto",)),
        "test_last_class.py::TestNumber2::test_x": (False, ("autouse _auto",)),
    }


def test_requests_are_read_the_way_pytest_reads_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parameter with a default, a positional-only parameter and a name a direct ``parametrize``
    supplies are not fixture requests; ``request.getfixturevalue("name")`` is one (QA of GQ-403)."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_requests.py": """
            @pytest.fixture
            def faked(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def test_default(faked=None):
                pass

            def test_keyword_only_default(*, faked=None):
                pass

            def test_positional_only(faked, /):
                pass

            @pytest.mark.parametrize("faked", [1, 2])
            def test_parametrized(faked):
                pass

            @pytest.mark.parametrize(("other", "faked"), [(1, 2)])
            def test_parametrized_as_a_tuple(other, faked):
                pass

            def test_at_run_time(request):
                request.getfixturevalue("faked")
            """
        },
    )

    assert population == {
        "test_requests.py::test_at_run_time": (False, ("fixture faked",)),
    }


def test_collection_follows_pytest_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``test`` without an underscore is collected, a ``Test`` class with ``__init__`` is not, and a
    name defined twice is its last definition (QA of GQ-403). A call in a decorator runs at import,
    not for the test, so it is no route either."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_collection.py": """
            def _cases():
                monkeypatch.setattr(services, "DemoClient", _Fake)
                return [1]

            def testnounderscore(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            class TestWithInit:
                def __init__(self):
                    pass

                def test_never_collected(self, monkeypatch):
                    monkeypatch.setattr(services, "DemoClient", _Fake)

            def test_defined_twice(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def test_defined_twice(monkeypatch):
                pass

            @pytest.mark.parametrize("value", _cases())
            def test_decorator_call(value):
                pass
            """
        },
    )

    assert population == {
        "test_collection.py::testnounderscore": (False, ("body",)),
    }


def test_names_the_test_binds_itself_shadow_module_helpers_in_every_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assignment, import and ``except ... as`` bind a name as surely as a nested ``def``."""
    population = _population(
        tmp_path,
        monkeypatch,
        {
            "test_shadowing.py": """
            def _install(monkeypatch):
                monkeypatch.setattr(services, "DemoClient", _Fake)

            def test_assignment(monkeypatch):
                _install = print
                _install(monkeypatch)

            def test_import(monkeypatch):
                from builtins import print as _install
                _install(monkeypatch)

            def test_except_name(monkeypatch):
                try:
                    pass
                except ValueError as _install:
                    _install(monkeypatch)

            def test_control(monkeypatch):
                _install(monkeypatch)
            """
        },
    )

    assert population == {
        "test_shadowing.py::test_control": (False, ("helper _install",)),
    }


# --- the four measured cases on the real tree ----------------------------------------------------


def _real_routes() -> dict[str, tuple[str, ...]]:
    return {
        f"{file}::{test_id}": routes
        for (file, test_id), routes in guard_audit.claiming_routes().items()
    }


@pytest.mark.parametrize(
    ("test_id", "routes", "why"),
    [
        (
            "test_olog_update.py::TestServiceUpdate::test_needs_at_least_one_field",
            ("helper _install_fake",),
            "the helper route in a module without an autouse double",
        ),
        (
            "test_diagnose.py::test_shell_naming_enabled_splits_unregistered",
            ("helper _patch_naming_client",),
            "the dotted-string double through a helper, in a module without an autouse double",
        ),
        (
            "test_doctor.py::test_classify_retry_error_is_api_error",
            (_AUTOUSE,),
            "the autouse route alone: this test installs no double of its own",
        ),
        (
            "test_doctor.py::test_no_ingest_reaches_the_cli_without_the_confirmation_sentence",
            (_AUTOUSE, "helper _wire_starved_archiver"),
            "the UNION of both routes, which proves neither on its own",
        ),
    ],
)
def test_the_four_measured_blind_spots_are_seen_on_the_real_tree(
    test_id: str, routes: tuple[str, ...], why: str
) -> None:
    """Each case the GQ-403 entry measured, with the route it was picked to prove.

    Before GQ-403 none of them was in the population. If one fails, first read whether the test
    module still has that shape: a pick that stopped installing its double proves nothing and has
    to be replaced, not re-pinned.
    """
    found = _real_routes()
    assert test_id in found, f"{test_id} is no longer in the population; pick another for: {why}"
    assert found[test_id] == routes, f"{why}: expected {routes}, measured {found[test_id]}"
