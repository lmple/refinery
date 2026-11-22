"""The run's worktree and branch, created by factoryd at start (runner D1).

A run on change ``<change>`` in root ``<root>`` lives in
``<root parent>/<root name>.worktrees/<change>`` on ``change/<change>``. Every
git call goes through the supervisor (``Repo``); any failure refuses the start.
"""

from pathlib import Path

from refinery.review.repo import Repo
from refinery.run.record import worktrees_dir

TOOL_JAR = Path("tools") / "tla2tools.jar"


class WorktreeError(Exception):
    """The run's worktree cannot be created or reused; nothing was started."""


def branch_name(change: str) -> str:
    """The run's branch."""
    return f"change/{change}"


def worktree_path(root: Path, change: str) -> Path:
    """Where the run's worktree lives."""
    return worktrees_dir(root) / change


async def ensure_worktree(root_repo: Repo, root: Path, change: str) -> Path:
    """Reuse, add, or create the branch and add the worktree; its path.

    WorktreeError if the path is something else or the branch is checked out
    elsewhere; GitError if a git call fails.
    """
    raise NotImplementedError("worktree creation (runner D1)")


def link_tools(root: Path, worktree: Path) -> None:
    """Symlink the root's TLA+ jar into the worktree, if it has none."""
    raise NotImplementedError("shared tool jar (runner D1)")
