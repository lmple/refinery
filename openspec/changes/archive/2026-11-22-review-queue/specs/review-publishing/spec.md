# Spec Delta

## Purpose

Defines how factoryd makes each review gate reviewable on GitHub, and how it
finishes a run once the human approves it finally:
- committing the run record;
- pushing the branch;
- opening the PR;
- archiving the OpenSpec change;
- validating the trace.

The behaviour is modelled by `spec/Review.tla`.

## ADDED Requirements

### Requirement: Publishing at review gates
When a run enters `spec_review`, `tests_review`, `final_review` or
`escalated`, factoryd SHALL do three things:
1. Commit the run record (`.refinery/runs/<run-id>.json`) on the run's branch
   if it changed.
2. Push the branch to `origin`.
3. At the first such gate, open a draft PR against the default branch with
   `gh`.

The PR URL SHALL be kept in `<data>/runs/<run-id>/publish.json`.

#### Scenario: First review gate
- **WHEN** run `demo.1` enters `spec_review` for the first time
- **THEN** the branch is pushed with the run record committed, a draft PR exists, and its URL is in `publish.json`

#### Scenario: Later gates reuse the PR
- **GIVEN** run `demo.1` already has a PR
- **WHEN** it enters `tests_review`
- **THEN** the branch is pushed again and no second PR is created

### Requirement: Publishing fails open
If committing, pushing or opening the PR fails, factoryd SHALL record the
error in `publish.json` and show it with the run. The run SHALL still enter
the review phase, and the human SHALL still be able to decide.

#### Scenario: Push rejected
- **GIVEN** a remote that rejects the push
- **WHEN** run `demo.1` enters `spec_review`
- **THEN** the run is in `spec_review`, the run's status shows the push error, and the human can approve it

### Requirement: Runs need a branch worktree
A run SHALL start only in a worktree that is on a named branch other than the
default branch (`main`), so that publishing never pushes `main`.

#### Scenario: Start on main
- **GIVEN** a worktree with `main` checked out
- **WHEN** a human starts a run in it
- **THEN** the request is refused and no run is created

#### Scenario: Start on a detached HEAD
- **GIVEN** a worktree with a detached HEAD
- **WHEN** a human starts a run in it
- **THEN** the request is refused

### Requirement: Archive sequence
In `archive`, factoryd SHALL perform these steps in order:
1. Run `openspec archive <change> --yes` in the worktree and commit the
   result.
2. Perform `Archive`, with trace validation by TLC.
3. Commit the final run record, push, and mark the PR ready for review.

A failure at any step SHALL leave the run in `archive` and record the error.
Each step SHALL be skipped when it was already done, so that a retry, or a
restart after a crash, resumes where the sequence stopped.

#### Scenario: Final approval archives
- **GIVEN** run `demo.1` in `final_review`, change `demo` not yet archived
- **WHEN** the human approves it
- **THEN** `openspec/changes/demo/` is gone, `openspec/changes/archive/<date>-demo/` exists, the run is `archived` with trace verdict `pass`, the branch is pushed, and the PR is ready

#### Scenario: OpenSpec archive fails
- **GIVEN** `openspec archive demo --yes` exits non-zero
- **WHEN** the archive sequence runs
- **THEN** the run stays in `archive`, the error is shown, and no `Archive` event is recorded

#### Scenario: Resume after a crash
- **GIVEN** factoryd crashed after archiving the OpenSpec change but before `Archive`
- **WHEN** factoryd restarts
- **THEN** it does not run `openspec archive` again, and it records `Archive`

#### Scenario: Trace validation fails
- **GIVEN** the event log is not a behaviour of `spec/Run.tla`
- **WHEN** the archive sequence runs
- **THEN** the run stays in `archive` with verdict `fail`, and after `RetryArchive` the sequence resumes at trace validation
