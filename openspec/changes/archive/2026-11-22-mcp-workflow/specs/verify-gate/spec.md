# Spec Delta

## ADDED Requirements

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
