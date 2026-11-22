# Spec Delta

## MODIFIED Requirements

### Requirement: Runs need a branch worktree
A run SHALL live in a worktree on its own branch `change/<change>`, never on
the default branch (`main`) or a detached HEAD, so that publishing never
pushes `main`. Because factoryd creates the worktree on that branch (see
run-worktrees), the root may itself be on `main`.

#### Scenario: Start on main
- **GIVEN** root `/src/repo` with `main` checked out
- **WHEN** a human starts a run on `demo` in it
- **THEN** the run is created, its worktree is on `change/demo`, and the root is still on `main`

#### Scenario: Start on a detached HEAD
- **GIVEN** root `/src/repo` with a detached HEAD
- **WHEN** a human starts a run on `demo` in it
- **THEN** the run's worktree is on `change/demo`, created from the default branch, and the root's HEAD is unchanged

#### Scenario: Publishing pushes the run's branch
- **GIVEN** run `demo.1` entering `spec_review`
- **WHEN** factoryd publishes it
- **THEN** it pushes `change/demo` and nothing else
