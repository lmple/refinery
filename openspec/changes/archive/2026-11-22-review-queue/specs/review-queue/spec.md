# Spec Delta

## Purpose

Defines how the human decides at review gates through factoryd. Covers what
a decision records, how the reviewed commit is fixed, the rule that makes
every review a review of a commit, and the queue of runs waiting for the
human. The behaviour is modelled by `spec/Review.tla`.

## ADDED Requirements

### Requirement: Decisions at review gates
A human SHALL approve or reject a run waiting at a review gate (`spec_review`,
`tests_review` or `final_review`). factoryd SHALL pick the transition from the
phase: `ApproveSpec`, `ApproveTests` or `ApproveFinal` to approve, and the
matching `Reject*` to reject. A rejection SHALL carry a non-empty note.

#### Scenario: Approve the spec
- **GIVEN** run `demo.1` in `spec_review`
- **WHEN** the human approves it
- **THEN** the run is in `tests` and its last event is `ApproveSpec`

#### Scenario: Reject the tests with a note
- **GIVEN** run `demo.1` in `tests_review`
- **WHEN** the human rejects it with note "test 4 tests nothing"
- **THEN** the run is in `tests` and its `RejectTests` event carries that note

#### Scenario: Nothing to decide
- **GIVEN** run `demo.1` in `impl`
- **WHEN** the human approves it
- **THEN** the request is refused and the run is unchanged

### Requirement: factoryd records the reviewed commit
On `ApproveSpec` and `ApproveTests`, factoryd SHALL record the run worktree's
`HEAD` as the approved commit. A human SHALL NOT supply one. Any approval,
`ApproveFinal` included, SHALL be refused if the worktree has uncommitted
changes outside `.refinery/`. It SHALL also be refused if `HEAD` is not the
commit factoryd published for this gate, because the human would then approve
a commit they never saw.

#### Scenario: Commit recorded on approval
- **GIVEN** run `demo.1` in `spec_review` whose worktree is clean at commit C
- **WHEN** the human approves it
- **THEN** the run record's approved spec commit is C

#### Scenario: New commit after publishing
- **GIVEN** run `demo.1` in `spec_review` published at commit C, and the agent committed D on top afterwards
- **WHEN** the human approves it
- **THEN** the request is refused, naming C and D, and the run is unchanged

#### Scenario: Dirty worktree at approval
- **GIVEN** run `demo.1` in `tests_review` with an uncommitted edit under `tests/`
- **WHEN** the human approves it
- **THEN** the request is refused, saying the worktree has uncommitted changes, and the run is unchanged

### Requirement: Every review is of a commit
An agent submission that leads into review SHALL be refused while the
worktree has uncommitted changes, or untracked files that aren't ignored,
outside `.refinery/`. Those submissions are `CompleteModel`, `SkipModel`,
`CompleteTests` and `CompleteImpl`.

#### Scenario: Submit with uncommitted work
- **GIVEN** run `demo.1` in `tests` with an uncommitted new test file
- **WHEN** the agent calls `submit_for_review`
- **THEN** the call fails, naming the uncommitted path, and the run stays in `tests`

#### Scenario: The run record does not count
- **GIVEN** run `demo.1` in `model` whose only uncommitted file is `.refinery/runs/demo.1.json`
- **WHEN** the agent calls `submit_for_review`
- **THEN** the run is in `spec_review`

### Requirement: Recovery decisions
A human SHALL be able to:
- cancel any unfinished run;
- resolve an escalation to `spec`, `tests` or `impl`;
- retry an archive whose trace validation failed.

Each SHALL be refused, with the run unchanged, when the run is not in a phase
that allows it.

#### Scenario: Resolve to tests
- **GIVEN** run `demo.1` in `escalated` after a test bug
- **WHEN** the human resolves it to `tests`
- **THEN** the run is in `tests` and its last event is `ResolveToTests`

#### Scenario: Resolve outside an escalation
- **GIVEN** run `demo.1` in `impl`
- **WHEN** the human resolves it to `spec`
- **THEN** the request is refused and the run is unchanged

### Requirement: The review queue
The review queue SHALL list every unfinished run whose next move is the
human's, with run id, change, phase and how long it has waited. These are
runs in:
- `spec_review`, `tests_review` or `final_review`;
- `escalated`;
- `archive` after a failed archive.

#### Scenario: Queue contents
- **GIVEN** run `a.1` in `spec_review`, run `b.1` in `impl` and run `c.1` in `escalated`
- **WHEN** the human lists the queue
- **THEN** it holds `a.1` and `c.1` and not `b.1`
