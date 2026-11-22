# Tasks

Phases follow AGENTS.md:
- **Group 1 (tests phase):** an API skeleton plus tests written failing, then
  frozen at the tests review gate.
- **Group 2 (implementation):** must not touch `spec/`, `openspec/specs/` or
  the frozen tests.

## 1. Tests (written failing; frozen after the tests review gate)

- [x] 1.0 Write the API skeleton:
      - `src/refinery/proc/` (`Supervisor`, `CallResult`, `Outcome`,
        `Capture`, `StreamTo`, the registry, `reap`);
      - `src/refinery/gate/` (`GateRunner`, `GateConfig`, `GateReport`,
        `StepResult`, `frozen_paths`);
      - `src/refinery/run/tlc.py` (`TlcTraceValidator`);
      - the new model fields `commit`, `spec_commit` and `tests_commit`,
        plus `RunStore.worktree`.

      New behaviour raises `NotImplementedError`. Verify: `uv run pyright` is
      clean.
- [x] 1.1 Update the `run-lifecycle` tests for approvals that carry a commit.
      In `tests/run/helpers.py`, the default `ApproveSpec`/`ApproveTests`
      payloads get a commit. The property oracle (`spec_allows`) requires a
      valid commit, and its step generator sometimes omits or malforms one.
      Verify: the diff touches only those places.
- [x] 1.2 Close the replay-validator gap: in `test_transition_properties.py`,
      the corrupted-log property also corrupts `from_phase`. Verify: with the
      `from_phase` check deleted from `archive.py` in a scratch copy, the test
      fails.
- [x] 1.3 In `tests/run/test_approved_commits.py`, write the scenarios of
      "Approvals record the reviewed commit", including the record JSON
      round trip and a record without `approved`. Verify: they fail with
      `NotImplementedError`.
- [x] 1.4 In `tests/proc/test_supervisor.py`, write one test per
      process-supervision scenario: process group, background child,
      timeout, SIGTERM ignored, cancellation, outcomes, capture and stream,
      registry entry lifetime, latch with the supervisor dying before
      registration (a child Python process killed with SIGKILL), reaping an
      orphan, and sparing a reused pid. Also the source scan for
      process-starting calls. Verify: they fail with `NotImplementedError`.
- [x] 1.5 In `tests/gate/test_gate.py`, write one test per verify-gate
      scenario. Each uses a throwaway git worktree whose Makefile targets are
      scripted to pass, fail, hang or be missing, a `RunStore` run driven to
      `verify`, and short time limits. Verify: they fail with
      `NotImplementedError`.
- [x] 1.6 In `tests/gate/test_frozen.py`, write the frozen-check scenarios
      against a real git repository: edited frozen test, committed and
      uncommitted; new untracked spec file; ignored file; only `src/`
      changed. Verify: they fail with `NotImplementedError`.
- [x] 1.7 In `tests/run/test_tlc_validator.py`, test real TLC:
      - a valid run log passes;
      - the skipped-gate log fails;
      - a missing Java binary fails, with the reason naming the failure to
        start;
      - a time limit too short to finish kills TLC and fails.

      These tests skip if `tools/tla2tools.jar` is absent. Verify: they fail
      with `NotImplementedError`.
- [x] 1.8 Run `uv run pytest -q`. Every new test fails for the stated reason,
      the updated `run-lifecycle` tests fail only where approvals now need
      code, and nothing errors in collection. Then stop for the tests review
      gate.

## 2. Implementation

- [x] 2.1 Add reviewed commits to `refinery.run`: the model, `approve_*` and
      `resolve_*` in `transitions`, `record`, `eventlog` and `index` (D6).
      Verify: 1.1 and 1.3 pass, and the whole `tests/run` suite still passes.
- [x] 2.2 In `src/refinery/proc/`, implement the supervisor: process group,
      latch, registry, kill escalation, cancellation, outcomes, capture and
      stream, and reaping (D1–D3). Verify: 1.4 passes.
- [x] 2.3 In `src/refinery/gate/`, implement the frozen check through the
      supervisor (D5). Verify: 1.6 passes.
- [x] 2.4 In `src/refinery/gate/`, implement `GateRunner`: steps, records,
      decision, transition, cancellation, ids after a crash, and
      `RunStore.worktree` (D4). Verify: 1.5 passes.
- [x] 2.5 In `src/refinery/run/tlc.py`, implement `TlcTraceValidator` (D7).
      Verify: 1.7 passes.

## 3. Verify

- [x] 3.1 Verify: `make gate` is green, and `make tla-big` reports no
      violation.
- [x] 3.2 End to end, by hand: run a real `RunStore` run to `verify` in a
      scratch git worktree of this repository and run the gate. Then archive
      it with `TlcTraceValidator`. Verify: `PassGate` is recorded, archiving
      succeeds, and no process is left in the registry.

## Workflow follow-up

- Review gate 1 (spec + model): now. Wait for approval before group 1.
- Review gate 2 (tests): after 1.8.
- Final review, then archive the change, commit and open a PR.
