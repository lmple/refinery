"""Requirement "TLC trace validation" (run-record, from the gate-runner
change): the production validator runs real TLC on the exported trace."""

import shutil
from pathlib import Path

import pytest

from refinery.proc import registry
from refinery.proc.supervisor import Supervisor
from refinery.run import transitions
from refinery.run.model import Action, Event, Phase, Role
from refinery.run.tlc import TlcTraceValidator
from tests.run.helpers import (
    HAPPY_PATH,
    accepted,
    at,
    drive,
    passing_trace,
    request,
    run_async,
    started,
)

ROOT = Path(__file__).resolve().parents[2]
JAR = ROOT / "tools" / "tla2tools.jar"
RUN_SPEC = ROOT / "spec" / "Run.tla"

pytestmark = pytest.mark.skipif(
    not JAR.exists() or shutil.which("java") is None,
    reason="needs tools/tla2tools.jar (make tools) and java",
)

A = Action
P = Phase


def validator(
    tmp_path: Path, *, java: str = "java", timeout_s: float = 120
) -> TlcTraceValidator:
    return TlcTraceValidator(
        Supervisor(tmp_path / "data", grace_s=0.5),
        java=java,
        jar=JAR,
        run_spec=RUN_SPEC,
        work_dir=tmp_path / "work",
        timeout_s=timeout_s,
    )


def whole_workflow() -> list[Event]:
    """A real log: produced by the transition functions, Archive included."""
    first = started("demo.1", "demo")
    rec, events = drive(first.record, HAPPY_PATH)
    archive = accepted(
        transitions.archive(
            rec, request(A.ARCHIVE, minute=20), passing_trace(rec.seq + 1)
        )
    )
    return [first.event, *events, archive.event]


def skipped_gate() -> list[Event]:
    """ApproveTests straight from tests: the tests review never happened."""
    rows = [
        (A.START_RUN, P.NONE, P.SPEC),
        (A.COMPLETE_SPEC, P.SPEC, P.MODEL),
        (A.COMPLETE_MODEL, P.MODEL, P.SPEC_REVIEW),
        (A.APPROVE_SPEC, P.SPEC_REVIEW, P.TESTS),
        (A.APPROVE_TESTS, P.TESTS, P.IMPL),
    ]
    return [
        Event(
            seq,
            name,
            src,
            dst,
            Role.HUMAN if name.value.startswith(("Start", "Approve")) else Role.AGENT,
            at(seq),
        )
        for seq, (name, src, dst) in enumerate(rows, start=1)
    ]


def test_valid_log_passes(tmp_path: Path) -> None:
    result = run_async(validator(tmp_path).validate("demo.1", whole_workflow()))
    assert result.passed, result.reason


def test_log_that_skips_a_gate_fails(tmp_path: Path) -> None:
    result = run_async(validator(tmp_path).validate("demo.1", skipped_gate()))
    assert not result.passed
    assert "not a behaviour" in result.reason


def test_tlc_unavailable(tmp_path: Path) -> None:
    result = run_async(
        validator(tmp_path, java="does-not-exist-java").validate(
            "demo.1", whole_workflow()
        )
    )
    assert not result.passed
    assert "failed_to_start" in result.reason


def test_tlc_too_slow(tmp_path: Path) -> None:
    v = validator(tmp_path, timeout_s=0.05)
    result = run_async(v.validate("demo.1", whole_workflow()))
    assert not result.passed
    assert "timed_out" in result.reason
    assert registry.list_entries(v.supervisor.data_dir) == []


def test_work_directory_is_cleaned_up(tmp_path: Path) -> None:
    v = validator(tmp_path)
    run_async(v.validate("demo.1", whole_workflow()))
    assert not v.work_dir.exists() or list(v.work_dir.iterdir()) == []


def test_validates_against_the_configured_spec(tmp_path: Path) -> None:
    """A Run.tla in which nothing can happen accepts no log."""
    broken = tmp_path / "Run.tla"
    broken.write_text(
        RUN_SPEC.read_text().replace(
            'Begin(r, "StartRun"', 'FALSE /\\ Begin(r, "StartRun"', 1
        )
    )
    v = TlcTraceValidator(
        Supervisor(tmp_path / "data", grace_s=0.5),
        java="java",
        jar=JAR,
        run_spec=broken,
        work_dir=tmp_path / "work",
        timeout_s=120,
    )
    assert not run_async(v.validate("demo.1", whole_workflow())).passed
