# Tasks

Phases follow AGENTS.md, driven through factoryd (`review-queue.1`):
- **Group 1 (tests phase):** an API skeleton plus tests written failing, then
  frozen at the tests review gate.
- **Group 2 (implementation):** must not touch `spec/`, `openspec/specs/` or
  the frozen tests.

The work is committed before each submission into a review.

## 1. Tests (written failing; frozen after the tests review gate)

- [x] 1.0 Write the API skeleton, with new behaviour raising
      `NotImplementedError`:
      - `refinery.review` (`Review`, `ReviewError`, `git` helpers, `publish`,
        `archive_sequence`);
      - `refinery.factoryd.api`;
      - `refinery.cli.refine`;
      - the `refine` console script.

      Verify: `uv run pyright` is clean.
- [x] 1.1 In `tests/review/test_decisions.py`, write the review-queue
      scenarios against `Review`:
      - decisions per gate, with notes;
      - the reviewed commit is `HEAD`;
      - a dirty worktree is refused;
      - recovery decisions;
      - the queue contents.

      Verify: they fail with `NotImplementedError`.
- [x] 1.2 In `tests/review/test_publish.py`, write the publishing scenarios
      against a bare git remote:
      - record commit and push;
      - PR create and reuse, with a scripted fake `gh` on `PATH`;
      - a push rejected fails open;
      - starting on `main` or a detached HEAD is refused.

      Verify: they fail with `NotImplementedError`.
- [x] 1.3 In `tests/review/test_archive.py`, write the archive-sequence
      scenarios:
      - the full sequence, with a fake `openspec` that moves the change
        directory;
      - `openspec` failing;
      - resuming after a crash at each step;
      - trace failure then retry.

      Verify: they fail with `NotImplementedError`.
- [x] 1.4 In `tests/review/test_api.py`, write the human-api scenarios over
      real HTTP:
      - 401 and 403;
      - each resource;
      - error status codes and bodies.

      Verify: they fail with `NotImplementedError`.
- [x] 1.5 In `tests/review/test_refine.py`, write the `refine` scenarios against
      a served factoryd: approve, a refused reject, a missing token, and
      `runs --waiting`. Verify: they fail with `NotImplementedError`.
- [x] 1.6 Extend `tests/factoryd/test_workflow.py` with:
      - `last_decision` and `pr` in `get_task`;
      - submission refused with uncommitted work;
      - the record file doesn't count as uncommitted.

      Update existing workflow tests that relied on supplying commits
      (approvals now go through `Review`). Verify: the new tests fail.
- [x] 1.7 Run `uv run pytest -q`: every new test fails for the stated reason,
      all others pass, and nothing errors in collection. Commit, then submit
      for the tests review.

## 2. Implementation

- [x] 2.1 Implement the `git` helpers over the supervisor (D2, D3). Verify:
      the helper tests in 1.2 pass.
- [x] 2.2 Implement `Review` decisions, the queue and the start checks (D1,
      D2, D6). Verify: 1.1 passes.
- [x] 2.3 Implement publishing, and call it from `Workflow` and `Review`
      (D3). Verify: 1.2 passes.
- [x] 2.4 Implement the archive sequence, retry, and resuming on startup
      (D4). Verify: 1.3 passes.
- [x] 2.5 Implement the API routes and mount them; add `last_decision` and
      `pr` to `get_task` (D5, D7). Verify: 1.4 and 1.6 pass.
- [x] 2.6 Implement the `refine` CLI (D8). Verify: 1.5 passes.
- [x] 2.7 Update `docs/vision.md`: the change order (`review-queue` before
      `runner`) and the MVP review text. Verify: it matches this change.
- [x] 2.8 Shutdown diagnostics: `refinery serve` registers faulthandler on
      SIGUSR1 (D10). Verify: `tests/factoryd/test_shutdown.py` passes. Its
      SIGINT test is a regression guard and already passes; the SIGUSR1 test
      fails before this task.

## 3. Verify

- [x] 3.1 Verify: `make gate` is green, and `make tla-big` reports no
      violation.
- [x] 3.2 End to end, by hand: a scratch run in a clone with a local bare
      remote and a fake `gh`. Drive it with `refine` and the agent tools
      through every gate to `archived`. Verify: the pushes, the PR calls, the
      archived OpenSpec change, a trace verdict of pass, and nothing left in
      the registry.
