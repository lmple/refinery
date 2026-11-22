# Spec Delta

## MODIFIED Requirements

### Requirement: Starting a workflow
`start_workflow(change, root)` SHALL create or reuse the run's worktree (see
run-worktrees), then perform `StartRun` on `change` with the run's record in
that worktree. It SHALL return the run id, the phase and the worktree, and no
token: agent tokens come from job claims. `root` SHALL be one of factoryd's
configured roots.

#### Scenario: Start
- **WHEN** a human starts a workflow on change `demo` in configured root `/src/repo`
- **THEN** the response has run id `demo.1`, phase `spec` and worktree `/src/repo.worktrees/demo`, and no agent token

#### Scenario: Worktree outside the roots
- **WHEN** `root` is not a configured root
- **THEN** the call fails and no run is created

#### Scenario: Change already has a run
- **GIVEN** change `demo` has an unfinished run
- **WHEN** a human starts another workflow on `demo`
- **THEN** the call fails naming the existing run
