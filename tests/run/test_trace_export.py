"""Trace export (design.md, D7) and name parity with spec/Run.tla.

The golden modules were checked with TLC 1.7.4 when they were written:
happy, detour and retry are accepted (TraceIncomplete violated), and a
variant skipping the tests review gate is rejected (deadlock).
"""

import re
from pathlib import Path

import pytest

from refinery.run.model import Action, Event, Phase
from refinery.run.trace_export import export_trace
from tests.run.helpers import ROLE_OF, at

A = Action
P = Phase
ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "golden"

HAPPY = [
    (A.START_RUN, P.NONE, P.SPEC),
    (A.COMPLETE_SPEC, P.SPEC, P.MODEL),
    (A.COMPLETE_MODEL, P.MODEL, P.SPEC_REVIEW),
    (A.APPROVE_SPEC, P.SPEC_REVIEW, P.TESTS),
    (A.COMPLETE_TESTS, P.TESTS, P.TESTS_REVIEW),
    (A.APPROVE_TESTS, P.TESTS_REVIEW, P.IMPL),
    (A.COMPLETE_IMPL, P.IMPL, P.VERIFY),
    (A.PASS_GATE, P.VERIFY, P.FINAL_REVIEW),
    (A.APPROVE_FINAL, P.FINAL_REVIEW, P.ARCHIVE),
    (A.ARCHIVE, P.ARCHIVE, P.ARCHIVED),
]
DETOUR = [
    (A.START_RUN, P.NONE, P.SPEC),
    (A.COMPLETE_SPEC, P.SPEC, P.MODEL),
    (A.SKIP_MODEL, P.MODEL, P.SPEC_REVIEW),
    (A.REJECT_SPEC, P.SPEC_REVIEW, P.SPEC),
    (A.COMPLETE_SPEC, P.SPEC, P.MODEL),
    (A.COMPLETE_MODEL, P.MODEL, P.SPEC_REVIEW),
    (A.APPROVE_SPEC, P.SPEC_REVIEW, P.TESTS),
    (A.COMPLETE_TESTS, P.TESTS, P.TESTS_REVIEW),
    (A.APPROVE_TESTS, P.TESTS_REVIEW, P.IMPL),
    (A.ESCALATE, P.IMPL, P.ESCALATED),
    (A.RESOLVE_TO_TESTS, P.ESCALATED, P.TESTS),
    (A.COMPLETE_TESTS, P.TESTS, P.TESTS_REVIEW),
    (A.APPROVE_TESTS, P.TESTS_REVIEW, P.IMPL),
    (A.COMPLETE_IMPL, P.IMPL, P.VERIFY),
    (A.FAIL_GATE, P.VERIFY, P.IMPL),
    (A.COMPLETE_IMPL, P.IMPL, P.VERIFY),
    (A.PASS_GATE, P.VERIFY, P.FINAL_REVIEW),
    (A.REJECT_FINAL, P.FINAL_REVIEW, P.IMPL),
    (A.COMPLETE_IMPL, P.IMPL, P.VERIFY),
    (A.PASS_GATE, P.VERIFY, P.FINAL_REVIEW),
    (A.APPROVE_FINAL, P.FINAL_REVIEW, P.ARCHIVE),
    (A.ARCHIVE, P.ARCHIVE, P.ARCHIVED),
]
RETRY = [*HAPPY[:9], (A.RETRY_ARCHIVE, P.ARCHIVE, P.ARCHIVE), HAPPY[9]]


def events(rows: list[tuple[Action, Phase, Phase]]) -> list[Event]:
    return [
        Event(seq, name, src, dst, ROLE_OF[name], at(seq))
        for seq, (name, src, dst) in enumerate(rows, start=1)
    ]


@pytest.mark.parametrize(
    ("run_id", "rows", "module"),
    [
        ("run-lifecycle.1", HAPPY, "Trace_run_lifecycle_1"),
        ("run-lifecycle.2", DETOUR, "Trace_run_lifecycle_2"),
        ("run-lifecycle.4", RETRY, "Trace_run_lifecycle_4"),
    ],
    ids=["happy", "detour", "retry"],
)
def test_export_matches_golden(
    run_id: str, rows: list[tuple[Action, Phase, Phase]], module: str
) -> None:
    exported = export_trace(run_id, events(rows))
    assert exported.name == module
    assert exported.tla == (GOLDEN / f"{module}.tla").read_text()
    assert exported.cfg == (GOLDEN / f"{module}.cfg").read_text()


def test_export_ignores_payload_and_timestamps() -> None:
    plain = events(HAPPY)
    noisy = [
        Event(
            e.seq, e.name, e.from_phase, e.to_phase, e.actor, at(100 + e.seq), note="x"
        )
        for e in plain
    ]
    assert export_trace("r.1", plain) == export_trace("r.1", noisy)


def _tla_strings(definition: str) -> set[str]:
    """String literals in the definition of ``definition`` in spec/Run.tla."""
    text = (ROOT / "spec" / "Run.tla").read_text()
    match = re.search(
        rf"^{definition}\s*==(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL
    )
    assert match, definition
    return set(re.findall(r'"([a-z_A-Z]+)"', match.group(1)))


def test_action_names_equal_the_spec() -> None:
    assert {a.value for a in Action} == _tla_strings("Acts")


def test_phase_names_equal_the_spec() -> None:
    groups = ("AgentPhases", "ReviewPhases", "ServerPhases", "Terminal", "Phases")
    spec_phases = set[str]().union(*(_tla_strings(g) for g in groups))
    assert {p.value for p in Phase} == spec_phases
