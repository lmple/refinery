"""Publishing review gates, and the archive sequence (D3, D4).

``spec/Review.tla`` models both. Publishing is PublishRecord then PublishPush
or PushFails: it fails open, recording the error, and the gate counts as
published at HEAD either way. The archive sequence is ArchiveSpecs,
ValidateTrace, then FinishRecord, FinishPush and FinishReady. Each step is
skipped once done, so a retry or a restart resumes it, and a failure stops
it in place (fail closed).
"""

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from refinery.review.repo import GitError, Repo
from refinery.run.archive import TraceValidator
from refinery.run.model import Accepted, Phase, RunRecord, Verdict
from refinery.run.store import RunStore


@dataclass(frozen=True)
class PublishState:
    """``<data>/runs/<run-id>/publish.json``."""

    branch: str | None = None
    published: str | None = None  # HEAD published for the current gate
    pr: str | None = None
    last_error: str | None = None
    finished: bool = False  # the archive sequence completed


@dataclass(frozen=True)
class ArchiveResult:
    """Where the archive sequence got to."""

    archived: bool
    step: str  # "openspec", "trace", "finish" or "done"
    error: str | None = None


def publish_path(data_dir: Path, run_id: str) -> Path:
    """Where a run's publishing state lives."""
    return data_dir / "runs" / run_id / "publish.json"


def read_state(data_dir: Path, run_id: str) -> PublishState:
    """The run's publishing state (empty if never published)."""
    path = publish_path(data_dir, run_id)
    try:
        raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return PublishState()

    def text(key: str) -> str | None:
        value = raw.get(key)
        return None if value is None else str(value)

    return PublishState(
        branch=text("branch"),
        published=text("published"),
        pr=text("pr"),
        last_error=text("last_error"),
        finished=bool(raw.get("finished")),
    )


def write_state(data_dir: Path, run_id: str, state: PublishState) -> None:
    """Atomically replace the run's publishing state."""
    path = publish_path(data_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(asdict(state), file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _record_rel(record: RunRecord) -> str:
    return f".refinery/runs/{record.run_id}.json"


def _pr_body(record: RunRecord) -> str:
    return (
        f"Refinery run `{record.run_id}` for OpenSpec change `{record.change}`.\n\n"
        f"Proposal: `openspec/changes/{record.change}/proposal.md`.\n\n"
        "Opened by factoryd; review decisions are made with `refine`."
    )


async def publish(
    repo: Repo, store: RunStore, data_dir: Path, run_id: str
) -> PublishState:
    """Commit the run record, push, open the PR if needed; fails open."""
    record = store.get(run_id)
    state = replace(read_state(data_dir, run_id), last_error=None, finished=False)
    try:
        branch = await repo.branch()
        if branch is None:
            raise GitError("the worktree is on a detached HEAD")
        state = replace(state, branch=branch)
        try:
            await repo.commit([_record_rel(record)], f"run: {run_id} {record.phase}")
        finally:
            # Published at HEAD whatever happens next: the human can always
            # review locally (Review.tla: PushFails).
            state = replace(state, published=await repo.head())
        await repo.push(branch)
        pr = await repo.pr_url(branch)
        if pr is None:
            base = await repo.default_branch()
            pr = await repo.pr_create(branch, base, record.change, _pr_body(record))
        state = replace(state, pr=pr)
    except GitError as exc:
        state = replace(state, last_error=f"{datetime.now(UTC):%H:%M:%S} {exc}")
    write_state(data_dir, run_id, state)
    return state


async def archive_sequence(
    repo: Repo,
    store: RunStore,
    data_dir: Path,
    run_id: str,
    validator: TraceValidator,
    *,
    timeout_s: float,
) -> ArchiveResult:
    """openspec archive + commit, Archive (trace), final commit, push, PR ready."""
    state = read_state(data_dir, run_id)

    def stop(step: str, error: str) -> ArchiveResult:
        write_state(data_dir, run_id, replace(state, last_error=error, finished=False))
        return ArchiveResult(False, step, error)

    record = store.get(run_id)
    # a. The OpenSpec change: done once its directory is gone (committed).
    if record.phase is Phase.ARCHIVE:
        if (repo.worktree / "openspec" / "changes" / record.change).exists():
            try:
                await repo.openspec_archive(record.change)
                await repo.commit(
                    ["openspec"], f"{record.change}: archive the OpenSpec change"
                )
            except GitError as exc:
                return stop("openspec", f"openspec archive failed: {exc}")
        # b. Archive with trace validation: done once the run is archived.
        if record.trace.verdict is Verdict.FAIL:
            return stop("trace", "trace validation failed; retry-archive after a fix")
        outcome = await store.archive(run_id, validator, datetime.now(UTC), timeout_s)
        if not isinstance(outcome, Accepted):
            return stop("trace", outcome.reason)
        record = outcome.record
    if record.phase is not Phase.ARCHIVED:
        return stop("trace", f"cannot archive a run in {record.phase}")
    # c. Final record commit, push, PR ready: each idempotent.
    try:
        branch = state.branch or await repo.branch()
        if branch is None:
            raise GitError("the worktree is on a detached HEAD")
        await repo.commit([_record_rel(record)], f"run: {run_id} archived")
        await repo.push(branch)
        info = await repo.pr_info(branch)
        if info is None:
            base = await repo.default_branch()
            url = await repo.pr_create(branch, base, record.change, _pr_body(record))
            info = (url, True)
        if info[1]:
            await repo.pr_ready(branch)
        state = replace(state, branch=branch, pr=info[0])
    except GitError as exc:
        return stop("finish", f"finishing the archive failed: {exc}")
    write_state(data_dir, run_id, replace(state, last_error=None, finished=True))
    return ArchiveResult(True, "done")
