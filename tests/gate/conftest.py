"""A throwaway git worktree whose make targets behave as each test scripts."""

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from refinery.gate.runner import GateConfig, GateRunner, Step
from refinery.proc.supervisor import Supervisor
from refinery.run.model import Action, Phase, RunRecord
from refinery.run.store import RunStore
from tests.run.helpers import HAPPY_PATH, accepted, request, run_async

A = Action
CHANGE = "demo"
RUN = "demo.1"

MAKEFILE = """\
BEHAVIOUR_DIR ?= /nonexistent
TLA_JAR ?=
test types lint tla:
\t@sh "$(BEHAVIOUR_DIR)/$@.sh" "$(TLA_JAR)"
.PHONY: test types lint tla
"""

# Isolate git from the developer's own configuration (signing, hooks, ...).
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def make_repo(root: Path) -> str:
    """A repository with spec/, tests/, src/ and the scripted Makefile."""
    root.mkdir()
    for rel, text in {
        "Makefile": MAKEFILE,
        ".gitignore": "__pycache__/\n.refinery/\n",
        "spec/Model.tla": "---- MODULE Model ----\n====\n",
        "openspec/specs/cap/spec.md": "# cap\n",
        f"openspec/changes/{CHANGE}/specs/cap/spec.md": "# delta\n",
        "openspec/changes/other/specs/cap/spec.md": "# other change\n",
        "tests/test_x.py": "def test_x() -> None:\n    assert True\n",
        "src/x.py": "X = 1\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return git(root, "rev-parse", "HEAD")


@dataclass
class Worktree:
    """A repo, a store with run demo.1 in verify, and a gate runner."""

    root: Path
    data: Path
    behaviours: Path
    head: str
    store: RunStore
    supervisor: Supervisor

    def behave(self, step: Step, script: str) -> None:
        """What ``make <step>`` does: a shell script; $1 is TLA_JAR."""
        (self.behaviours / f"{step}.sh").write_text(script + "\n")

    def config(
        self,
        timeouts: dict[Step, float] | None = None,
        make: str = "make",
        tla_jar: Path | None = None,
    ) -> GateConfig:
        limits = dict.fromkeys(Step, 10.0) | (timeouts or {})
        env = {**os.environ, **GIT_ENV, "BEHAVIOUR_DIR": str(self.behaviours)}
        return GateConfig(timeouts=limits, make=make, tla_jar=tla_jar, env=env)

    def runner(self, config: GateConfig | None = None) -> GateRunner:
        return GateRunner(
            self.store, self.supervisor, config or self.config(), self.data
        )

    def record(self) -> RunRecord:
        return self.store.get(RUN)

    def gate_dir(self, gate_id: str) -> Path:
        return self.data / "runs" / RUN / "gates" / gate_id


async def drive_to_verify(store: RunStore, root: Path, head: str) -> None:
    accepted(await store.start_run(CHANGE, root, request(A.START_RUN)))
    for minute, name in enumerate(HAPPY_PATH[:6], start=1):  # ... CompleteImpl
        commit = head if name in (A.APPROVE_SPEC, A.APPROVE_TESTS) else None
        accepted(
            await store.transition(RUN, request(name, minute=minute, commit=commit))
        )
    assert store.get(RUN).phase is Phase.VERIFY


@pytest.fixture
def wt(tmp_path: Path) -> Iterator[Worktree]:
    root, data, behaviours = (
        tmp_path / "repo",
        tmp_path / "data",
        tmp_path / "behaviours",
    )
    head = make_repo(root)
    behaviours.mkdir()

    async def open_store() -> RunStore:
        store = await RunStore.open(data, [root])
        await drive_to_verify(store, root, head)
        return store

    store = run_async(open_store())
    worktree = Worktree(
        root, data, behaviours, head, store, Supervisor(data, grace_s=0.5)
    )
    for step in (Step.TEST, Step.TYPES, Step.LINT, Step.TLA):
        worktree.behave(step, "exit 0")
    yield worktree
    store.close()
