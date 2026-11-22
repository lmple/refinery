"""The ``frozen`` step: frozen artifacts unchanged since their approval (D5)."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from refinery.proc.supervisor import Capture, Outcome, Supervisor
from refinery.run.model import Artifact, RunRecord


@dataclass(frozen=True)
class FrozenResult:
    """Whether every frozen artifact matches its approved commit."""

    passed: bool
    changed: tuple[Artifact, ...]
    detail: str


def frozen_paths(change: str) -> dict[Artifact, tuple[str, ...]]:
    """The paths each artifact covers (verify-gate spec)."""
    return {
        Artifact.SPEC: (
            "spec/",
            "openspec/specs/",
            f"openspec/changes/{change}/specs/",
        ),
        Artifact.TESTS: ("tests/",),
    }


def _approved_commit(record: RunRecord, artifact: Artifact) -> str | None:
    return record.spec_commit if artifact is Artifact.SPEC else record.tests_commit


async def check_frozen(
    supervisor: Supervisor,
    worktree: Path,
    record: RunRecord,
    *,
    git: str,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> FrozenResult:
    """Compare each frozen artifact's paths with its approved commit.

    Fails closed: a git call that does not give a clear answer fails the step.
    """
    paths = frozen_paths(record.change)
    changed: list[Artifact] = []
    lines: list[str] = []
    errors = False

    async def git_call(*args: str) -> tuple[int | None, str, str]:
        result = await supervisor.run(
            [git, *args], cwd=worktree, timeout_s=timeout_s, output=Capture(), env=env
        )
        code = result.exit_code if result.outcome is Outcome.EXITED else None
        why = result.reason or (result.stderr or "").strip()
        return code, result.stdout or "", f"{result.outcome}: {why}".strip(": ")

    for artifact in sorted(record.frozen):
        commit = _approved_commit(record, artifact)
        if commit is None:
            errors = True
            lines.append(f"{artifact}: frozen without an approved commit")
            continue
        code, _, why = await git_call("diff", "--quiet", commit, "--", *paths[artifact])
        untracked_code, untracked, untracked_why = await git_call(
            "ls-files", "--others", "--exclude-standard", "--", *paths[artifact]
        )
        if code not in (0, 1) or untracked_code != 0:
            errors = True
            problem = why or untracked_why
            lines.append(f"{artifact}: git could not compare with {commit}: {problem}")
            continue
        new_files = untracked.split()
        if code == 1 or new_files:
            changed.append(artifact)
            extra = f"; new untracked: {', '.join(new_files)}" if new_files else ""
            lines.append(f"{artifact}: differs from approved commit {commit}{extra}")
        else:
            lines.append(f"{artifact}: unchanged since {commit}")
    return FrozenResult(
        passed=not errors and not changed,
        changed=tuple(changed),
        detail="\n".join(lines),
    )
