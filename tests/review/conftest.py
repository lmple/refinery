"""A factoryd over a repo with a local bare remote, a fake ``gh`` and a fake
``openspec``, for the review-queue tests. Scenarios run in one event loop
(``ReviewFactory.run``): gate runs and archive sequences are background tasks.

Fakes:
- ``gh`` logs every call to ``fakes/gh.log`` and plays one PR: ``pr view``
  answers once ``pr create`` ran, ``pr ready`` clears the draft flag.
- ``openspec archive <change> --yes`` moves ``openspec/changes/<change>`` into
  ``openspec/changes/archive/<date>-<change>``; it logs to ``fakes/openspec.log``
  and fails while ``fakes/openspec_fails`` exists.
- The remote rejects pushes while ``fakes/push_rejected`` exists.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from refinery.auth.tokens import Principal
from refinery.factoryd.config import ServerConfig
from refinery.factoryd.workflow import Workflow
from refinery.gate.runner import Step
from refinery.review.repo import Tools
from refinery.review.service import Review
from refinery.run.archive import ValidationResult
from refinery.run.model import Event, Phase
from tests.factoryd.conftest import Factory, start, to_phase
from tests.gate.conftest import CHANGE, git, make_repo

P = Phase
PR_URL = "https://github.example/lmple/refinery/pull/7"

FAKE_GH = """#!/bin/sh
echo "$*" >> "{fakes}/gh.log"
case "$1 $2" in
  "pr view")
    [ -f "{fakes}/pr" ] || {{ echo "no pull requests found" >&2; exit 1; }}
    if [ -f "{fakes}/ready" ]; then draft=false; else draft=true; fi
    url=$(cat "{fakes}/pr")
    printf '{{"url":"%s","isDraft":%s}}\\n' "$url" "$draft" ;;
  "pr create") echo "{url}" > "{fakes}/pr"; echo "{url}" ;;
  "pr ready") touch "{fakes}/ready" ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
"""

FAKE_OPENSPEC = """#!/bin/sh
echo "$*" >> "{fakes}/openspec.log"
[ -f "{fakes}/openspec_fails" ] && {{ echo "openspec: simulated failure" >&2; exit 1; }}
if [ "$1" != archive ] || [ "$3" != --yes ]; then
  echo "unexpected openspec call: $*" >&2; exit 2
fi
[ -d "openspec/changes/$2" ] || {{ echo "no change $2" >&2; exit 1; }}
mkdir -p openspec/changes/archive
mv "openspec/changes/$2" "openspec/changes/archive/$(date +%Y-%m-%d)-$2"
"""

PRE_RECEIVE = """#!/bin/sh
[ -f "{fakes}/push_rejected" ] && {{ echo "push rejected (test)" >&2; exit 1; }}
exit 0
"""


class SwitchableValidator:
    """A trace validator whose verdict the test sets (TLC is tested elsewhere)."""

    def __init__(self) -> None:
        self.passed = True
        self.calls = 0

    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        self.calls += 1
        return ValidationResult(self.passed, "" if self.passed else "rejected (test)")


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


@dataclass
class ReviewFactory(Factory):
    """Factory plus a bare remote, fake gh and openspec, and a validator."""

    fakes: Path = Path()
    remote: Path = Path()
    validator: SwitchableValidator = dataclasses.field(
        default_factory=SwitchableValidator
    )

    @property
    def config(self) -> ServerConfig:
        base = super().config
        tools = Tools(
            gh=str(self.fakes / "gh"),
            openspec=str(self.fakes / "openspec"),
            timeout_s=20,
        )
        return dataclasses.replace(base, tools=tools)

    def flag(self, name: str, on: bool = True) -> None:
        path = self.fakes / name
        if on:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def log(self, tool: str) -> list[str]:
        path = self.fakes / f"{tool}.log"
        return path.read_text().splitlines() if path.exists() else []

    def remote_head(self, branch: str = "change/demo") -> str | None:
        out = git(
            self.remote,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/heads/{branch}",
        )
        return out or None

    def current_head(self) -> str:
        return git(self.root, "rev-parse", "HEAD")

    def run_review[T](self, scenario: Callable[[Workflow, Review], Awaitable[T]]) -> T:
        """Open a Workflow (with this validator) and a Review; one event loop."""

        async def go() -> T:
            workflow = await Workflow.open(self.config, validator=self.validator)
            try:
                return await scenario(workflow, Review(workflow))
            finally:
                await workflow.close()

        return asyncio.run(go())


@pytest.fixture
def rf(tmp_path: Path) -> ReviewFactory:
    root, data, behaviours = (
        tmp_path / "repo",
        tmp_path / "data",
        tmp_path / "behaviours",
    )
    fakes, remote = tmp_path / "fakes", tmp_path / "remote.git"
    fakes.mkdir()
    behaviours.mkdir()
    head = make_repo(root)
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    _script(remote / "hooks" / "pre-receive", PRE_RECEIVE.format(fakes=fakes))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "origin", "main")
    git(root, "remote", "set-head", "origin", "main")
    _script(fakes / "gh", FAKE_GH.format(fakes=fakes, url=PR_URL))
    _script(fakes / "openspec", FAKE_OPENSPEC.format(fakes=fakes))
    factory = ReviewFactory(root, data, behaviours, head, fakes=fakes, remote=remote)
    for step in (Step.TEST, Step.TYPES, Step.LINT, Step.TLA):
        factory.behave(step, "exit 0")
    return factory


async def started(
    wf: Workflow, rf: ReviewFactory, change: str = CHANGE
) -> tuple[str, Principal]:
    return await start(wf, rf, change)


async def to_review(
    wf: Workflow, rf: ReviewFactory, run_id: str, agent: Principal
) -> None:
    """Agent submissions up to spec_review (spec, then model)."""
    await to_phase(wf, rf, run_id, agent, P.SPEC_REVIEW)


async def to_final_review(
    wf: Workflow, review: Review, rf: ReviewFactory, run_id: str, agent: Principal
) -> None:
    """Through both review gates (approved via Review) and a passing gate run."""
    await to_review(wf, rf, run_id, agent)
    await review.approve(run_id)
    await wf.submit_for_review(agent)  # tests -> tests_review
    await review.approve(run_id)
    await wf.submit_for_review(agent)  # impl -> verify, gate starts
    await wf.wait_idle()
    assert wf.store.get(run_id).phase is P.FINAL_REVIEW


def commit_file(rf: ReviewFactory, rel: str, text: str, message: str) -> str:
    path = rf.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(rf.root, "add", rel)
    git(rf.root, "commit", "-q", "-m", message)
    return rf.current_head()
