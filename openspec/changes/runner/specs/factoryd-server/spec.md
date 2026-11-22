# Spec Delta

## MODIFIED Requirements

### Requirement: Startup order
On startup, before accepting any request, factoryd SHALL do three things in
this order:
1. Reap registered process groups in its own registry (never the runner's).
2. Recover and index runs from the configured roots, their worktree
   directories (see run-worktrees) and the data directory.
3. Schedule the gate of every run in `verify`.

#### Scenario: Restart with an orphan and a run in verify
- **GIVEN** a registered process group left by a crash, and run `demo.1` in `verify` with no gate result
- **WHEN** factoryd starts
- **THEN** the orphan group is killed before `demo.1`'s new gate run starts, and that gate run records a result

#### Scenario: Restart finds runs in worktrees
- **GIVEN** run `demo.1` with its record only in `/src/repo.worktrees/demo`
- **WHEN** factoryd starts with root `/src/repo`
- **THEN** `demo.1` is indexed before the first request is answered
