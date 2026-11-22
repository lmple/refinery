"""git, gh and openspec over the supervisor, for one worktree (D2, D3, D4)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from refinery.proc.supervisor import Supervisor

RECORD_PREFIX = ".refinery/"


class GitError(Exception):
    """A git, gh or openspec call did not succeed; the message says why."""


@dataclass(frozen=True)
class Tools:
    """The external programs factoryd drives, and their time limit."""

    git: str = "git"
    gh: str = "gh"
    openspec: str = "openspec"
    timeout_s: float = 30.0


class Repo:
    """One run's worktree, as seen by factoryd."""

    def __init__(
        self,
        supervisor: Supervisor,
        worktree: Path,
        tools: Tools,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """``env`` is passed to every program (None: factoryd's environment)."""
        self.supervisor = supervisor
        self.worktree = worktree
        self.tools = tools
        self.env = env

    async def head(self) -> str:
        """The full SHA of HEAD."""
        raise NotImplementedError

    async def branch(self) -> str | None:
        """The checked-out branch, or None on a detached HEAD."""
        raise NotImplementedError

    async def default_branch(self) -> str:
        """origin's default branch, falling back to ``main``."""
        raise NotImplementedError

    async def dirty_paths(self) -> list[str]:
        """Uncommitted or untracked, non-ignored paths outside ``.refinery/``."""
        raise NotImplementedError

    async def commit(self, paths: Sequence[str], message: str) -> bool:
        """Commit ``paths`` if they changed; whether a commit was made."""
        raise NotImplementedError

    async def push(self, branch: str) -> None:
        """Push ``branch`` to origin (fast-forward only)."""
        raise NotImplementedError

    async def pr_url(self, branch: str) -> str | None:
        """The URL of ``branch``'s PR, or None."""
        raise NotImplementedError

    async def pr_create(self, branch: str, base: str, title: str, body: str) -> str:
        """Open a draft PR; its URL."""
        raise NotImplementedError

    async def pr_ready(self, branch: str) -> None:
        """Mark ``branch``'s PR ready for review."""
        raise NotImplementedError

    async def openspec_archive(self, change: str) -> None:
        """``openspec archive <change> --yes`` in the worktree."""
        raise NotImplementedError
