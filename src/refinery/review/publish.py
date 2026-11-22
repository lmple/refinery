"""Publishing review gates, and the archive sequence (D3, D4)."""

from dataclasses import dataclass
from pathlib import Path

from refinery.review.repo import Repo
from refinery.run.archive import TraceValidator
from refinery.run.store import RunStore


@dataclass(frozen=True)
class PublishState:
    """``<data>/runs/<run-id>/publish.json``."""

    branch: str | None = None
    published: str | None = None  # HEAD published for the current gate
    pr: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ArchiveResult:
    """Where the archive sequence got to."""

    archived: bool
    step: str  # "openspec", "trace", "finish" or "done"
    error: str | None = None


def publish_path(data_dir: Path, run_id: str) -> Path:
    """Where a run's publishing state lives."""
    raise NotImplementedError


def read_state(data_dir: Path, run_id: str) -> PublishState:
    """The run's publishing state (empty if never published)."""
    raise NotImplementedError


async def publish(
    repo: Repo, store: RunStore, data_dir: Path, run_id: str
) -> PublishState:
    """Commit the run record, push, open the PR if needed; fails open."""
    raise NotImplementedError


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
    raise NotImplementedError
