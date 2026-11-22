"""Scenarios of specs/run-worktrees/spec.md (runner), and the runner change's
deltas to review-publishing ("Runs need a branch worktree") and
factoryd-server ("Startup order"), against a repo with a bare remote.

The fixture's root is on ``change/demo``; most scenarios first put it on
``main``, the way a human starts runs from the main checkout.
"""

import asyncio
import dataclasses
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from refinery.auth.tokens import Role
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.workflow import Workflow, WorkflowError
from refinery.review.service import Review
from refinery.run.model import Phase
from tests.factoryd.conftest import HUMAN
from tests.gate.conftest import BRANCH, CHANGE, git
from tests.review.conftest import ReviewFactory

P = Phase


def worktree_of(rf: ReviewFactory, change: str = CHANGE) -> Path:
    return rf.root.parent / f"{rf.root.name}.worktrees" / change


def on_main(rf: ReviewFactory, *, keep_branch: bool = False) -> None:
    """Put the root on main; drop change/demo unless ``keep_branch``."""
    git(rf.root, "checkout", "-q", "main")
    if not keep_branch:
        git(rf.root, "branch", "-q", "-D", BRANCH)


def run_with[T](
    rf: ReviewFactory,
    scenario: Callable[[Workflow, Review], Awaitable[T]],
    config: ServerConfig | None = None,
) -> T:
    async def go() -> T:
        workflow = await Workflow.open(config or rf.config, validator=rf.validator)
        try:
            return await scenario(workflow, Review(workflow))
        finally:
            await workflow.close()

    return asyncio.run(go())


def branch_of(path: Path) -> str:
    return git(path, "rev-parse", "--abbrev-ref", "HEAD")


def dirty_outside_records(path: Path) -> list[str]:
    """``git status`` lines, minus the run records (review-queue: clean)."""
    lines = git(path, "status", "--porcelain", "--untracked-files=all").splitlines()
    return [line for line in lines if not line[3:].startswith(".refinery/")]


# Requirement: factoryd creates the run's worktree


def test_first_run_of_a_change(rf: ReviewFactory) -> None:
    on_main(rf)
    main_head = git(rf.root, "rev-parse", "main")
    path = worktree_of(rf)

    async def scenario(wf: Workflow, review: Review) -> None:
        started = await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        assert (started["run_id"], started["phase"]) == ("demo.1", "spec")
        assert started["worktree"] == str(path)
        assert "agent_token" not in started
        assert wf.store.worktree("demo.1") == path

    run_with(rf, scenario)
    assert branch_of(path) == BRANCH
    assert git(path, "rev-parse", "HEAD") == main_head
    assert (path / ".refinery" / "runs" / "demo.1.json").is_file()
    assert branch_of(rf.root) == "main"


def test_a_new_run_after_a_cancel_continues_the_branch(rf: ReviewFactory) -> None:
    on_main(rf)
    path = worktree_of(rf)

    async def first(wf: Workflow, review: Review) -> str:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        (path / "work.txt").write_text("progress\n")
        git(path, "add", "work.txt")
        git(path, "commit", "-q", "-m", "work")
        await review.cancel("demo.1")
        return git(path, "rev-parse", "HEAD")

    commit = run_with(rf, first)

    async def second(wf: Workflow, review: Review) -> None:
        started = await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        assert (started["run_id"], started["worktree"]) == ("demo.2", str(path))
        assert wf.store.worktree("demo.2") == path

    run_with(rf, second)
    assert git(path, "rev-parse", "HEAD") == commit
    assert branch_of(path) == BRANCH


def test_branch_exists_without_a_worktree(rf: ReviewFactory) -> None:
    (rf.root / "branch-only.txt").write_text("on the branch\n")
    git(rf.root, "add", "branch-only.txt")
    git(rf.root, "commit", "-q", "-m", "branch work")
    branch_head = git(rf.root, "rev-parse", "HEAD")
    on_main(rf, keep_branch=True)
    path = worktree_of(rf)

    async def scenario(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))

    run_with(rf, scenario)
    assert branch_of(path) == BRANCH
    assert git(path, "rev-parse", "HEAD") == branch_head
    branches = git(rf.root, "branch", "--format=%(refname:short)").splitlines()
    assert sorted(branches) == sorted(["main", BRANCH])


# Requirement: Worktree creation fails closed


def test_branch_checked_out_in_the_root(rf: ReviewFactory) -> None:
    assert branch_of(rf.root) == BRANCH  # the fixture's root

    async def scenario(wf: Workflow, review: Review) -> None:
        with pytest.raises(WorkflowError, match=BRANCH):
            await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        assert wf.store.runs() == []

    run_with(rf, scenario)
    assert not worktree_of(rf).exists()


def test_path_taken_by_something_else(rf: ReviewFactory) -> None:
    on_main(rf)
    path = worktree_of(rf)
    path.mkdir(parents=True)
    (path / "notes.txt").write_text("mine\n")

    async def scenario(wf: Workflow, review: Review) -> None:
        with pytest.raises(WorkflowError, match=r"repo\.worktrees"):
            await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        assert wf.store.runs() == []

    run_with(rf, scenario)
    assert sorted(p.name for p in path.iterdir()) == ["notes.txt"]
    assert (path / "notes.txt").read_text() == "mine\n"


def test_change_already_running_runs_no_git(rf: ReviewFactory) -> None:
    on_main(rf)
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper = rf.fakes / "git"
    wrapper.write_text(
        f'#!/bin/sh\necho "$*" >> "{rf.fakes}/git.log"\nexec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    tools = dataclasses.replace(rf.config.tools, git=str(wrapper))
    config = dataclasses.replace(rf.config, tools=tools)

    async def scenario(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        calls = len(rf.log("git"))
        assert calls > 0, "the first start ran no git through the tools"
        with pytest.raises(WorkflowError, match=r"demo\.1"):
            await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        assert len(rf.log("git")) == calls

    run_with(rf, scenario, config)


# Requirement: The TLA+ tool jar is shared


def test_jar_linked(rf: ReviewFactory) -> None:
    on_main(rf)
    with (rf.root / ".gitignore").open("a") as ignore:
        ignore.write("tools/*.jar\n")
    git(rf.root, "commit", "-q", "-am", "ignore the TLA+ jar")
    jar = rf.root / "tools" / "tla2tools.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"PK\x03\x04 not really a jar")
    path = worktree_of(rf)

    async def scenario(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))

    run_with(rf, scenario)
    linked = path / "tools" / "tla2tools.jar"
    assert linked.is_symlink()
    assert linked.resolve() == jar.resolve()
    assert dirty_outside_records(path) == []


def test_no_jar_no_link(rf: ReviewFactory) -> None:
    on_main(rf)

    async def scenario(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))

    run_with(rf, scenario)
    assert not (worktree_of(rf) / "tools" / "tla2tools.jar").exists()
    assert dirty_outside_records(worktree_of(rf)) == []


# Requirement: Records in worktree directories are discovered
# (and factoryd-server, Startup order: "Restart finds runs in worktrees")


def test_restart_keeps_a_run_in_a_worktree(rf: ReviewFactory) -> None:
    on_main(rf)
    path = worktree_of(rf)

    async def first(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        agent = wf.authenticate(wf.tokens.create(Role.AGENT, run_id="demo.1").secret)
        assert agent is not None
        await wf.submit_for_review(agent)  # spec -> model
        await wf.submit_for_review(agent)  # model -> spec_review
        await review.approve("demo.1")
        await wf.submit_for_review(agent)  # tests -> tests_review

    run_with(rf, first)
    assert not (rf.root / ".refinery" / "runs" / "demo.1.json").exists()

    async def after_restart(wf: Workflow, review: Review) -> None:
        assert wf.store.get("demo.1").phase is P.TESTS_REVIEW
        assert wf.store.worktree("demo.1") == path
        assert [r["run_id"] for r in await review.runs()] == ["demo.1"]

    run_with(rf, after_restart)


# review-publishing, Requirement: Runs need a branch worktree


def test_start_on_main(rf: ReviewFactory) -> None:
    on_main(rf, keep_branch=True)

    async def scenario(wf: Workflow, review: Review) -> None:
        started = await review.start(HUMAN, CHANGE, str(rf.root))
        assert started["run_id"] == "demo.1"

    run_with(rf, scenario)
    assert branch_of(worktree_of(rf)) == BRANCH
    assert branch_of(rf.root) == "main"


def test_start_on_a_detached_head(rf: ReviewFactory) -> None:
    on_main(rf)
    git(rf.root, "checkout", "-q", "--detach")
    root_head = git(rf.root, "rev-parse", "HEAD")

    async def scenario(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))

    run_with(rf, scenario)
    path = worktree_of(rf)
    assert branch_of(path) == BRANCH
    assert git(path, "rev-parse", "HEAD") == git(rf.root, "rev-parse", "main")
    assert branch_of(rf.root) == "HEAD"  # still detached
    assert git(rf.root, "rev-parse", "HEAD") == root_head


def test_publishing_pushes_the_runs_branch(rf: ReviewFactory) -> None:
    on_main(rf)
    path = worktree_of(rf)

    async def scenario(wf: Workflow, review: Review) -> None:
        await wf.start_workflow(HUMAN, CHANGE, str(rf.root))
        agent = wf.authenticate(wf.tokens.create(Role.AGENT, run_id="demo.1").secret)
        assert agent is not None
        await wf.submit_for_review(agent)  # spec -> model
        await wf.submit_for_review(agent)  # model -> spec_review: published
        assert wf.store.get("demo.1").phase is P.SPEC_REVIEW

    run_with(rf, scenario)
    assert rf.remote_head(BRANCH) == git(path, "rev-parse", "HEAD")
    heads = git(rf.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert sorted(heads.splitlines()) == sorted(["main", BRANCH])
    assert git(rf.root, "rev-parse", "main") == rf.remote_head("main")
