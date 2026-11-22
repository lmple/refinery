# Tasks

The phases follow AGENTS.md. `runner.1` is driven by hand through factoryd,
since this change is what removes the hand steps (design D10):
- **Group 1 (tests phase):** a skeleton plus tests written failing, then
  frozen at the tests review gate.
- **Group 2 (implementation):** must not touch `spec/`, `openspec/specs/` or
  the frozen tests.

The work is committed before each submission into a review.

## 1. Tests (written failing; frozen after the tests review gate)

- [ ] 1.0 Write the skeleton, with new behaviour raising
      `NotImplementedError`:
      - `refinery.jobs` (job log, `state` fold, claim, finish, release,
        retry);
      - `refinery.runner` (loop and client);
      - `refinery.adapters.claude_code`;
      - `refinery.factoryd.runner_api`;
      - `Role.RUNNER`;
      - `refinery runner`.

      Verify: `uv run pyright` is clean.
- [ ] 1.1 In `tests/jobs/test_state.py`, write unit tests and hypothesis
      properties over the fold, mirroring `Runner.tla`'s invariants:
      - at most one open job;
      - progress isn't a failure;
      - stuck after `MaxAttempts`;
      - releases aren't attempts;
      - `Retry` resets the attempts;
      - no claim while stuck.

      Verify: they fail with `NotImplementedError`.
- [ ] 1.2 In `tests/jobs/test_claims.py`, write the claim scenarios against a
      real `TokenStore` and `RunStore`:
      - claim, serialised claims, a 503 that revokes the token;
      - finish, release and hello revoke tokens;
      - another runner's job is 404;
      - a silent runner keeps its job;
      - poll answers.

      Verify: they fail.
- [ ] 1.3 In `tests/review/test_worktrees.py`, write the run-worktrees
      scenarios against a temporary repo and a bare remote: first run, reuse
      after a cancel, an existing branch, a branch checked out in the root, a
      path taken, no git call when the change is already running, the jar
      symlink with a clean status, and discovery after a restart. Verify:
      they fail.
- [ ] 1.4 In `tests/factoryd/test_runner_api.py`, write the runner API over
      real HTTP: 401 and 403 per token role, each route, and job ids that
      belong to another runner. Verify: they fail.
- [ ] 1.5 In `tests/runner/test_runner.py`, write the runner scenarios
      against a served factoryd and a fake `claude` script that records its
      argv, environment, working directory and the MCP file's mode:
      - launch, one job at a time;
      - exits 1, timeout;
      - cancel kills, factoryd down;
      - SIGTERM releases;
      - restart reaps then says hello;
      - factoryd's reap spares agents;
      - no token.

      Verify: they fail.
- [ ] 1.6 In `tests/adapters/test_claude_code.py`, write the argv defaults and
      optional flags. Verify: they fail.
- [ ] 1.7 Update the existing tests:
      - `tests/factoryd/test_workflow.py`, `tests/review/test_api.py` and
        `tests/review/test_refine.py`: start takes a root and returns no
        token; tests that used the start token get one from a claim;
      - the `retry` route and command; the job in `status`; stuck runs in
        the queue;
      - `tests/auth/test_tokens.py`: the runner role.

      Verify: the new or changed tests fail for the stated reason.
- [ ] 1.8 Run `uv run pytest -q`: every new test fails for the stated reason,
      all others pass, and nothing errors in collection. Commit, then submit
      for the tests review.

## 2. Implementation

- [ ] 2.1 The runner role and per-run revocation in `TokenStore` (D3).
      Verify: the token tests pass.
- [ ] 2.2 The job log and the `state` fold (D4). Verify: 1.1 passes.
- [ ] 2.3 Worktree creation and record discovery (D1, D2). Verify: 1.3
      passes.
- [ ] 2.4 Claims, finish, release, hello, poll and retry, under the claim
      lock (D4, D5). Verify: 1.2 passes.
- [ ] 2.5 The runner API routes, mounted in `create_app` (D5). Verify: 1.4
      passes.
- [ ] 2.6 `start_workflow` and `Review.start` with a root and no token;
      `retry`, the job in `show`, stuck runs in the queue; `refine start`,
      `retry` and `status` (D8). Verify: 1.7 passes.
- [ ] 2.7 The Claude Code adapter (D7). Verify: 1.6 passes.
- [ ] 2.8 The runner process: reap, hello, the claim loop, launch, poll and
      kill, finish with retry, shutdown (D6). Verify: 1.5 passes.
- [ ] 2.9 Update `docs/vision.md` (factoryd creates the worktree; the MVP
      start text) and the Bootstrap note in `AGENTS.md`. Verify: they match
      this change.

## 3. Verify

- [ ] 3.1 Verify: `make gate` is green, and `make tla-big` reports no
      violation.
- [ ] 3.2 End to end, by hand, in a scratch clone with a bare remote, a fake
      `gh` and a fake `claude` that calls the MCP tools: `refine start`, then
      `refinery runner`. Drive two turns through a review with `refine`, then
      one stuck turn, `refine retry`, and a cancel. Verify the job log, the
      revoked tokens, and that nothing is left in either registry.
