# verify-gate Specification

## Purpose

Defines the verify gate: what a gate run executes in a run's worktree, how it
decides between `PassGate` and `FailGate`, where it records evidence, how it
enforces frozen artifacts, and how it behaves when the run is cancelled or
factoryd crashes. The behaviour is modelled by `spec/Gate.tla`.

## Requirements

### Requirement: Gate steps
A gate run SHALL execute, one at a time and in this order, the steps `frozen`,
`test`, `types`, `lint` and `tla` in the run's worktree. The last four run
`make <step>`. Every step SHALL run even after an earlier one failed, and each
SHALL run through the process supervisor with its own time limit.

#### Scenario: All steps run despite a failure
- **GIVEN** a worktree whose `make types` fails
- **WHEN** a gate run executes
- **THEN** all five steps ran, in order, and `types` is the only failed step

### Requirement: Gate decision fails closed
A gate run SHALL pass only if every step passed. A step passes only if its
command exited with code 0. Any other exit code, a timeout, a signal, a
failure to start or an error while running the step SHALL fail it.

#### Scenario: Timeout fails the gate
- **WHEN** a step exceeds its time limit
- **THEN** that step fails with outcome `timed_out` and the gate run fails

#### Scenario: Missing make fails the gate
- **WHEN** `make` cannot be started
- **THEN** the steps that need it fail with `failed_to_start` and the gate run fails

### Requirement: Gate result becomes a transition
When a gate run finishes, factoryd SHALL record `PassGate` (all steps passed)
or `FailGate` (otherwise) on the run as role factoryd. The event's gate
reference SHALL be the gate run id, and a `FailGate` reason SHALL name the
failed steps.

#### Scenario: Passing gate
- **GIVEN** a run in `verify` whose steps all pass
- **WHEN** gate run `g1` finishes
- **THEN** the run is in `final_review` and its `PassGate` event has gate `g1`

#### Scenario: Failing gate
- **GIVEN** a run in `verify` whose `lint` and `tla` steps fail
- **WHEN** gate run `g2` finishes
- **THEN** the run is in `impl` and its `FailGate` event has gate `g2` and a reason naming `lint` and `tla`

### Requirement: Gate records
Each gate run SHALL get an id `g<n>`, numbered per run from 1. Its step logs
SHALL be at `<data>/runs/<run-id>/gates/<id>/<step>.log`. When it finishes,
`result.json` SHALL record each step's outcome, exit code and duration, plus
the overall verdict.

#### Scenario: Records after a gate run
- **WHEN** gate run `g1` of run `demo.1` finishes
- **THEN** `<data>/runs/demo.1/gates/g1/` holds one log per step and a `result.json` whose verdict matches the recorded transition

### Requirement: Frozen artifacts are unchanged
The `frozen` step SHALL fail if any path of a frozen artifact differs from
that artifact's approved commit. Uncommitted changes and new untracked,
non-ignored files count as differences. Paths per artifact:
- `spec`: `spec/`, `openspec/specs/` and `openspec/changes/<change>/specs/`
- `tests`: `tests/`

#### Scenario: Edited frozen test
- **GIVEN** a run with `tests` frozen at commit C
- **WHEN** a file under `tests/` differs from C, committed or not
- **THEN** the `frozen` step fails and names the artifact

#### Scenario: New untracked spec file
- **GIVEN** a run with `spec` frozen at commit C
- **WHEN** an untracked, non-ignored file is added under `spec/`
- **THEN** the `frozen` step fails

#### Scenario: Only unfrozen paths changed
- **GIVEN** a run with both artifacts frozen
- **WHEN** only files under `src/` changed since the approved commits
- **THEN** the `frozen` step passes

### Requirement: One gate run per run, only in verify
A gate run SHALL start only for a run in `verify`, and only if no gate run is
in progress for that run.

#### Scenario: Not in verify
- **WHEN** a gate run is requested for a run in `impl`
- **THEN** it is refused and no step runs

#### Scenario: Already running
- **WHEN** a second gate run is requested while one is in progress for the same run
- **THEN** it is refused

### Requirement: Cancelled gate records nothing
If a gate run is cancelled, or its run leaves `verify` during it (for example
`Cancel`), the running step's processes SHALL be killed, the remaining steps
SHALL NOT start, and no `PassGate` or `FailGate` SHALL be recorded.

#### Scenario: Run cancelled mid-gate
- **GIVEN** a gate run whose `test` step is running
- **WHEN** the human cancels the run
- **THEN** the `test` processes are killed, no later step starts, and the run's last event is `Cancel`

### Requirement: Crash leaves no result
A gate run interrupted by a factoryd crash SHALL leave no `PassGate` or
`FailGate` and no `result.json`. After a restart, the run is still in `verify`
and a new gate run SHALL get the next id.

#### Scenario: Restart after a crash mid-gate
- **GIVEN** gate run `g1` was interrupted by a factoryd crash
- **WHEN** factoryd restarts and a gate run is requested
- **THEN** the run is still in `verify`, `g1` has no `result.json`, and the new gate run is `g2`

### Requirement: Gate starts automatically
When a run enters `verify`, factoryd SHALL start its gate run without being
asked. After a restart, it SHALL do the same for every run found in `verify`.
No run SHALL stay in `verify` without a gate run (`VerifyResolves` in
`spec/Workflow.tla`).

#### Scenario: Entering verify
- **WHEN** `CompleteImpl` is accepted for a run
- **THEN** a gate run for it is in progress, and it later records `PassGate` or `FailGate`

#### Scenario: After a restart
- **GIVEN** a run in `verify` whose gate run was interrupted by a crash
- **WHEN** factoryd restarts
- **THEN** a new gate run starts for it

### Requirement: Dry-run gates
A dry-run gate SHALL run the same steps as a gate run, for a run in `tests` or
`impl`. Its logs and `result.json` SHALL go to
`<data>/runs/<run-id>/dry-runs/d<n>/`, and it SHALL NOT record any transition.
If the run leaves `tests` or `impl` during it, its running step SHALL be
killed.

#### Scenario: Dry run leaves the run alone
- **GIVEN** a run in `impl` whose `types` step would fail
- **WHEN** a dry run executes
- **THEN** its result shows `types` failed, `dry-runs/d1/result.json` exists, and the run is still in `impl` with no new event

### Requirement: Dry runs and gate runs never overlap
For one run, a dry run SHALL NOT start while a gate run or another dry run is
in progress. `CompleteImpl` SHALL be refused while a dry run is in progress,
so that the gate run can never start during one.

#### Scenario: Submit during a dry run
- **GIVEN** a dry run in progress for a run in `impl`
- **WHEN** the agent submits for review
- **THEN** the submission fails and the run is still in `impl`

### Requirement: Gate errors fail closed
If a gate run hits an internal error (anything other than a step's own
result), the step in progress and every step not yet run SHALL count as
failed. The gate run SHALL then finish as usual, recording `FailGate` with
reason `gate error: <detail>`, rather than leave the run in `verify`. In
`spec/Gate.tla` terms these are failed steps of a finished gate run.

#### Scenario: Internal error
- **GIVEN** a gate run whose records cannot be written
- **WHEN** it runs
- **THEN** the run is in `impl` and its `FailGate` reason starts with `gate error:`
