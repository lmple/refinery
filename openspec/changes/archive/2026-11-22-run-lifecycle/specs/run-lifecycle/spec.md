# Spec Delta

## Purpose

Defines the states a Refinery run moves through while an OpenSpec change is
specified, modelled, tested, implemented, verified and archived. It also
defines who may move it, and which artifacts are frozen at each point. The
behaviour is modelled by `spec/Run.tla`; transition names equal its action
names.

## ADDED Requirements

### Requirement: Run phases
A run SHALL always be in exactly one phase: `spec`, `model`, `spec_review`,
`tests`, `tests_review`, `impl`, `verify`, `final_review`, `archive`,
`escalated`, `archived` or `cancelled`. `archived` and `cancelled` are
terminal.

#### Scenario: New run starts in the spec phase
- **WHEN** a human starts a run on an OpenSpec change
- **THEN** the run exists in phase `spec` with nothing frozen

### Requirement: One unfinished run per change
The system SHALL refuse to start a run on a change that already has a run in
a non-terminal phase, including a run whose start is still being recorded.

#### Scenario: Second start is refused
- **GIVEN** change `c` has a run in phase `tests`
- **WHEN** a human starts another run on `c`
- **THEN** the start is refused with an error naming the existing run, and no run is created

#### Scenario: Start after cancellation
- **GIVEN** the only run on change `c` is `cancelled`
- **WHEN** a human starts a run on `c`
- **THEN** a new run with a new run id is created in phase `spec`

#### Scenario: Concurrent starts
- **WHEN** two starts on the same change are requested concurrently
- **THEN** exactly one succeeds and the other is refused

### Requirement: Work transitions
The system SHALL move a run forward when its work for a phase is complete:
`CompleteSpec` (spec → model), `CompleteModel` or `SkipModel`
(model → spec_review), `CompleteTests` (tests → tests_review) and
`CompleteImpl` (impl → verify).

#### Scenario: Model completed
- **GIVEN** a run in phase `model`
- **WHEN** `CompleteModel` is accepted
- **THEN** the run is in `spec_review`

### Requirement: Review gate transitions
At each review gate the system SHALL accept an approval or a rejection:
`ApproveSpec` (spec_review → tests) or `RejectSpec` (→ spec); `ApproveTests`
(tests_review → impl) or `RejectTests` (→ tests); `ApproveFinal`
(final_review → archive) or `RejectFinal` (→ impl).

#### Scenario: Tests rejected
- **GIVEN** a run in `tests_review`
- **WHEN** `RejectTests` is accepted
- **THEN** the run is back in `tests`

### Requirement: Gate result transitions
In `verify` the system SHALL move the run to `final_review` on `PassGate` and
back to `impl` on `FailGate`.

#### Scenario: Gate fails
- **GIVEN** a run in `verify`
- **WHEN** `FailGate` is accepted
- **THEN** the run is in `impl`

### Requirement: Other transitions are refused
The system SHALL refuse any transition not allowed from the run's current
phase by this specification, leaving the run record and event log unchanged.

#### Scenario: Happy path
- **WHEN** a run receives StartRun, CompleteSpec, CompleteModel, ApproveSpec, CompleteTests, ApproveTests, CompleteImpl, PassGate, ApproveFinal and Archive in that order
- **THEN** every transition is accepted and the run ends `archived`

#### Scenario: Out-of-order transition is refused
- **GIVEN** a run in phase `spec`
- **WHEN** `CompleteImpl` is requested
- **THEN** it is refused with an error naming the current phase, and the run record and event log are unchanged

### Requirement: Transitions are restricted by role
Each transition SHALL belong to exactly one role, and the system SHALL refuse
a transition requested by any other role.

- human: StartRun, ApproveSpec, RejectSpec, ApproveTests, RejectTests,
  ApproveFinal, RejectFinal, ResolveToSpec, ResolveToTests, ResolveToImpl,
  RetryArchive, Cancel
- agent: CompleteSpec, CompleteModel, SkipModel, CompleteTests, CompleteImpl,
  Escalate
- factoryd: PassGate, FailGate, Archive

#### Scenario: Agent cannot approve
- **GIVEN** a run in phase `spec_review`
- **WHEN** the agent role requests `ApproveSpec`
- **THEN** it is refused and the run stays in `spec_review`

#### Scenario: Agent cannot report its own gate result
- **GIVEN** a run in phase `verify`
- **WHEN** the agent role requests `PassGate`
- **THEN** it is refused

### Requirement: Review decisions carry a note
A rejection SHALL carry a non-empty note from the human, and an approval MAY
carry one. The note SHALL be recorded with the transition's event.

#### Scenario: Rejection without a note is refused
- **GIVEN** a run in phase `tests_review`
- **WHEN** `RejectTests` is requested with an empty note
- **THEN** it is refused

#### Scenario: Rejection note is recorded
- **WHEN** `RejectSpec` is accepted with note "scenario 3 has no THEN"
- **THEN** the run's `RejectSpec` event contains that note

### Requirement: Skipping the model needs a reason
`SkipModel` SHALL carry a non-empty reason stating why the change does not
affect behaviour, so the human sees it at the spec review gate.

#### Scenario: Skip without a reason is refused
- **GIVEN** a run in phase `model`
- **WHEN** `SkipModel` is requested with an empty reason
- **THEN** it is refused and the run stays in `model`

### Requirement: Approved artifacts are frozen
`ApproveSpec` SHALL freeze the `spec` artifact and `ApproveTests` SHALL freeze
the `tests` artifact. Frozen artifacts SHALL be unfrozen only by
`ResolveToSpec` (both) or `ResolveToTests` (tests only). The run record SHALL
show the frozen set.

#### Scenario: Both frozen during implementation
- **WHEN** a run is in `impl`, `verify`, `final_review`, `archive` or `archived`
- **THEN** its frozen set is exactly {spec, tests}

#### Scenario: Rejection does not unfreeze
- **GIVEN** a run in `final_review` with {spec, tests} frozen
- **WHEN** `RejectFinal` is accepted
- **THEN** the run is in `impl` with {spec, tests} still frozen

### Requirement: Escalation to the human
From `tests` or `impl`, the agent SHALL be able to `Escalate` a suspected spec
bug or test bug, with that classification and a reason. In `escalated` the
human SHALL resolve with `ResolveToSpec` (→ spec, nothing frozen),
`ResolveToTests` (→ tests, spec frozen) or `ResolveToImpl` (→ impl, only if
tests are frozen).

#### Scenario: Test bug sent back to tests
- **GIVEN** a run in `impl` that escalated a test bug
- **WHEN** the human resolves with `ResolveToTests`
- **THEN** the run is in `tests` with only `spec` frozen

#### Scenario: ResolveToImpl before tests were approved
- **GIVEN** a run that escalated from `tests`
- **WHEN** the human requests `ResolveToImpl`
- **THEN** it is refused because tests are not frozen

#### Scenario: Escalation without classification is refused
- **WHEN** `Escalate` is requested without a classification of spec bug or test bug
- **THEN** it is refused

### Requirement: Cancellation
A human SHALL be able to `Cancel` a run in any non-terminal phase, which moves
it to `cancelled`.

#### Scenario: Cancel during review
- **GIVEN** a run in `spec_review`
- **WHEN** the human requests `Cancel`
- **THEN** the run is `cancelled`

### Requirement: Terminal phases are final
A run in `archived` or `cancelled` SHALL refuse every transition.

#### Scenario: No transition after archive
- **GIVEN** an `archived` run
- **WHEN** any transition, including `Cancel`, is requested
- **THEN** it is refused and the run stays `archived`
