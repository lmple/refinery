# Proposal: gate-runner

## Why

A run in `verify` has no way to get a gate result yet, and AGENTS.md requires
every subprocess (agents, TLC, git) to go through one wrapper with a timeout,
cancellation and a process-group kill. That wrapper doesn't exist either.

Two gaps in `run-lifecycle` also land here:
- Archive has no validator that actually checks the log against
  `spec/Run.tla`.
- "Frozen" spec and test files are recorded but never enforced.

## What Changes

- **Process supervisor:** the single entry point for subprocesses.
  - Each process runs in its own process group, with a time limit (SIGTERM,
    a grace period, then SIGKILL) and cancellation.
  - Nothing a call started may outlive it, including children the command
    left behind.
  - Output is captured or streamed to a log file.
  - Every process group is recorded in an on-disk registry before its command
    may run, behind a start latch. On startup, factoryd kills every
    registered group still alive, so a factoryd crash leaves no orphans.
- **Verify gate:** a gate run executes these steps one at a time in the run's
  worktree, continuing after a failure:
  - `frozen` (the frozen-file check);
  - `make test`, `make types`, `make lint` and `make tla`.

  It passes only if every step passes; a timeout, a failure to start or a
  signal counts as a failure (fail closed). The result becomes `PassGate` or
  `FailGate`, referencing the gate run, whose per-step logs and `result.json`
  live in the data directory. Cancelling the run mid-gate kills the running
  step and records nothing.
- **Reviewed commits:** `ApproveSpec` and `ApproveTests` must carry the full
  SHA of the reviewed commit. The run record keeps the approved commit per
  frozen artifact, and the `frozen` step fails if any frozen path differs from
  it, including uncommitted and untracked files.
- **TLC trace validator:**
  - exports the log as a trace module;
  - runs TLC through the supervisor, against the `Run.tla` that factoryd
    implements;
  - passes only if TLC reports `TraceIncomplete` violated. Anything else
    fails.
- **TLA+:** `spec/Gate.tla` models the gate run, process groups, the registry
  and start latch, cancellation and factoryd crashes. It comes with
  `Gate.cfg`, `Gate_big.cfg` and `Gate_reach.cfg`.
- **Test gap from the `run-lifecycle` final review:** the replay validator's
  `from_phase` check gets a test. This edits the archived change's
  `tests/run/test_transition_properties.py`.
- **Approval tests:** the existing run tests are updated where approvals now
  need a commit (`tests/run/helpers.py` request defaults and the property
  oracle).

## Capabilities

### New Capabilities

- `process-supervision`: how Refinery starts, limits, cancels and cleans up
  subprocesses, including across a factoryd crash.
- `verify-gate`: what a gate run executes, how it decides, where it records
  its results, the frozen-file check, cancellation and crash behaviour.

### Modified Capabilities

- `run-lifecycle`: adds the requirement that approvals carry the reviewed
  commit.
- `run-record`: adds the requirement that trace validation is done by TLC
  against `spec/Run.tla`.

## Impact

- **New packages:**
  - `src/refinery/proc/` (supervisor, registry, reaping);
  - `src/refinery/gate/` (gate runner, frozen check);
  - `src/refinery/run/tlc.py` (TLC validator).
- **Changed:**
  - `refinery.run.model`: `TransitionRequest.commit`, `Event.commit`, and
    `RunRecord.spec_commit` / `tests_commit`;
  - `transitions`, `record` and `eventlog`, for those fields;
  - `RunStore`, which now exposes a run's worktree.
- **Run record format:** a new `approved` object. The schema stays 1, because
  the field is additive and no records exist yet.
- **No new dependencies:**
  - Python: stdlib only (`asyncio`, `os`, `signal`);
  - external tools: `/bin/sh` (the start latch), `git`, `make`, Java and
    `tla2tools.jar`, all already used.
- **Linux-only:** the supervisor reads `/proc/<pid>/stat` to tell a reused PID
  from the original process. CI already runs on Ubuntu.
