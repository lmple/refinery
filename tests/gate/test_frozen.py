"""Requirement "Frozen artifacts are unchanged" (verify-gate) against a real
git repository, through check_frozen directly."""

import dataclasses
import os
from pathlib import Path

import pytest

from refinery.gate.frozen import FrozenResult, check_frozen, frozen_paths
from refinery.proc.supervisor import Supervisor
from refinery.run.model import Artifact, RunRecord
from tests.gate.conftest import CHANGE, GIT_ENV, git, make_repo
from tests.run.helpers import run_async, started

BOTH = frozenset({Artifact.SPEC, Artifact.TESTS})


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    return root, make_repo(root)


def frozen_record(head: str, artifacts: frozenset[Artifact] = BOTH) -> RunRecord:
    base = started("demo.1", CHANGE).record
    return dataclasses.replace(
        base,
        frozen=artifacts,
        spec_commit=head if Artifact.SPEC in artifacts else None,
        tests_commit=head if Artifact.TESTS in artifacts else None,
    )


def check(root: Path, rec: RunRecord, git_program: str = "git") -> FrozenResult:
    supervisor = Supervisor(root.parent / "data", grace_s=0.5)
    env = {**os.environ, **GIT_ENV}
    return run_async(
        check_frozen(supervisor, root, rec, git=git_program, timeout_s=30, env=env)
    )


def test_paths_per_artifact() -> None:
    assert frozen_paths("demo") == {
        Artifact.SPEC: ("spec/", "openspec/specs/", "openspec/changes/demo/specs/"),
        Artifact.TESTS: ("tests/",),
    }


def test_untouched_worktree_passes(repo: tuple[Path, str]) -> None:
    root, head = repo
    result = check(root, frozen_record(head))
    assert result.passed
    assert result.changed == ()


def test_edited_frozen_test_uncommitted(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "tests" / "test_x.py").write_text("# changed\n")
    result = check(root, frozen_record(head))
    assert not result.passed
    assert result.changed == (Artifact.TESTS,)
    assert "tests" in result.detail


def test_edited_frozen_test_committed(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "tests" / "test_x.py").write_text("# changed\n")
    git(root, "commit", "-q", "-am", "weaken the test")
    result = check(root, frozen_record(head))
    assert result.changed == (Artifact.TESTS,)


def test_deleted_frozen_test(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "tests" / "test_x.py").unlink()
    assert check(root, frozen_record(head)).changed == (Artifact.TESTS,)


def test_new_untracked_spec_file(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "spec" / "Extra.tla").write_text("---- MODULE Extra ----\n====\n")
    result = check(root, frozen_record(head))
    assert result.changed == (Artifact.SPEC,)


@pytest.mark.parametrize(
    "rel",
    ["openspec/specs/cap/spec.md", f"openspec/changes/{CHANGE}/specs/cap/spec.md"],
)
def test_spec_covers_main_specs_and_the_change_delta(
    repo: tuple[Path, str], rel: str
) -> None:
    root, head = repo
    (root / rel).write_text("# edited\n")
    assert check(root, frozen_record(head)).changed == (Artifact.SPEC,)


def test_both_artifacts_reported(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "spec" / "Model.tla").write_text("changed\n")
    (root / "tests" / "test_y.py").write_text("new\n")
    result = check(root, frozen_record(head))
    assert set(result.changed) == {Artifact.SPEC, Artifact.TESTS}


def test_ignored_file_does_not_count(repo: tuple[Path, str]) -> None:
    root, head = repo
    cache = root / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "test_x.cpython-312.pyc").write_bytes(b"\0")
    assert check(root, frozen_record(head)).passed


def test_only_unfrozen_paths_changed(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "src" / "x.py").write_text("X = 2\n")
    (root / "src" / "y.py").write_text("Y = 1\n")
    (root / "openspec/changes/other/specs/cap/spec.md").write_text("# other edit\n")
    git(root, "add", "src/x.py")
    git(root, "commit", "-q", "-m", "implement")
    assert check(root, frozen_record(head)).passed


def test_unfrozen_artifact_is_not_checked(repo: tuple[Path, str]) -> None:
    root, head = repo
    (root / "tests" / "test_x.py").write_text("# still being written\n")
    assert check(root, frozen_record(head, frozenset({Artifact.SPEC}))).passed


def test_compares_against_each_artifacts_own_commit(repo: tuple[Path, str]) -> None:
    root, first = repo
    (root / "tests" / "test_x.py").write_text("# tests approved later\n")
    git(root, "commit", "-q", "-am", "tests")
    second = git(root, "rev-parse", "HEAD")
    rec = dataclasses.replace(frozen_record(first), tests_commit=second)
    assert check(root, rec).passed


def test_missing_git_fails_closed(repo: tuple[Path, str]) -> None:
    root, head = repo
    result = check(root, frozen_record(head), git_program="does-not-exist-git")
    assert not result.passed


def test_unknown_commit_fails_closed(repo: tuple[Path, str]) -> None:
    root, head = repo
    rec = dataclasses.replace(frozen_record(head), spec_commit="f" * 40)
    result = check(root, rec)
    assert not result.passed
