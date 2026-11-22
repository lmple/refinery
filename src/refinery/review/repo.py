"""git, gh and openspec over the supervisor, for one worktree (D2, D3, D4).

Every call goes through the supervisor (one entry point for subprocesses),
with captured output and the tools' time limit. Any outcome other than the
expected exit codes raises GitError: callers decide whether that fails open
(publishing) or closed (approvals, the archive sequence).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from refinery.proc.supervisor import CallResult, Capture, Outcome, Supervisor

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

    async def _run(self, argv: Sequence[str], ok: tuple[int, ...] = (0,)) -> CallResult:
        result = await self.supervisor.run(
            argv,
            cwd=self.worktree,
            timeout_s=self.tools.timeout_s,
            output=Capture(),
            env=self.env,
        )
        if result.outcome is not Outcome.EXITED or result.exit_code not in ok:
            detail = (result.stderr or result.stdout or result.reason or "").strip()
            code = f" {result.exit_code}" if result.exit_code is not None else ""
            raise GitError(f"{' '.join(argv[:3])}: {result.outcome}{code}: {detail}")
        return result

    async def _git(self, *args: str, ok: tuple[int, ...] = (0,)) -> CallResult:
        return await self._run([self.tools.git, *args], ok)

    async def head(self) -> str:
        """The full SHA of HEAD."""
        return _text(await self._git("rev-parse", "HEAD"))

    async def branch(self) -> str | None:
        """The checked-out branch, or None on a detached HEAD."""
        result = await self._git("symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
        return _text(result) if result.exit_code == 0 else None

    async def default_branch(self) -> str:
        """origin's default branch, falling back to ``main``."""
        result = await self._git(
            "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", ok=(0, 1, 128)
        )
        name = _text(result) if result.exit_code == 0 else ""
        return name.removeprefix("origin/") or "main"

    async def dirty_paths(self) -> list[str]:
        """Uncommitted or untracked, non-ignored paths outside ``.refinery/``."""
        result = await self._git("status", "--porcelain", "-z", "--untracked-files=all")
        entries = (result.stdout or "").split("\0")
        paths: list[str] = []
        skip_next = False
        for entry in entries:
            if skip_next:  # the source path of a rename or copy
                skip_next = False
                continue
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            skip_next = status[0] in "RC"
            if not path.startswith(RECORD_PREFIX):
                paths.append(path)
        return paths

    async def commit(self, paths: Sequence[str], message: str) -> bool:
        """Commit ``paths`` if they changed; whether a commit was made."""
        await self._git("add", "--", *paths)
        staged = await self._git("diff", "--cached", "--quiet", "--", *paths, ok=(0, 1))
        if staged.exit_code == 0:
            return False
        await self._git("commit", "-q", "-m", message, "--", *paths)
        return True

    async def push(self, branch: str) -> None:
        """Push ``branch`` to origin (fast-forward only: never forced)."""
        await self._git("push", "-q", "-u", "origin", branch)

    async def pr_info(self, branch: str) -> tuple[str, bool] | None:
        """``(url, is_draft)`` of ``branch``'s PR, or None if it has none."""
        result = await self.supervisor.run(
            [self.tools.gh, "pr", "view", branch, "--json", "url,isDraft"],
            cwd=self.worktree,
            timeout_s=self.tools.timeout_s,
            output=Capture(),
            env=self.env,
        )
        if result.outcome is Outcome.EXITED and result.exit_code == 1:
            return None  # gh: no pull request for the branch
        if not result.ok:
            raise GitError(
                f"gh pr view: {result.outcome}: {(result.stderr or '').strip()}"
            )
        info = cast(dict[str, object], json.loads(result.stdout or "{}"))
        return str(info["url"]), bool(info.get("isDraft"))

    async def pr_url(self, branch: str) -> str | None:
        """The URL of ``branch``'s PR, or None."""
        info = await self.pr_info(branch)
        return None if info is None else info[0]

    async def pr_create(self, branch: str, base: str, title: str, body: str) -> str:
        """Open a draft PR; its URL."""
        result = await self._run(
            [
                self.tools.gh,
                "pr",
                "create",
                "--draft",
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        lines = (result.stdout or "").strip().splitlines()
        if not lines:
            raise GitError("gh pr create printed no URL")
        return lines[-1].strip()

    async def pr_ready(self, branch: str) -> None:
        """Mark ``branch``'s PR ready for review."""
        await self._run([self.tools.gh, "pr", "ready", branch])

    async def openspec_archive(self, change: str) -> None:
        """``openspec archive <change> --yes`` in the worktree."""
        await self._run([self.tools.openspec, "archive", change, "--yes"])


def _text(result: CallResult) -> str:
    return (result.stdout or "").strip()
