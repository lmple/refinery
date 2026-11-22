# Spec Delta

## ADDED Requirements

### Requirement: factoryd creates the run's worktree
Starting a run on change `<change>` in configured root `<root>` SHALL give
the run a git worktree at `<root parent>/<root name>.worktrees/<change>`, on
branch `change/<change>`, and SHALL write the run's record there. factoryd
SHALL:
- reuse that path if it is already a worktree on that branch;
- else add a worktree for the branch if the branch exists;
- else create the branch from the default branch and add its worktree.

Every git call SHALL go through the supervisor.

#### Scenario: First run of a change
- **GIVEN** root `/src/repo` on `main`, and no branch `change/demo`
- **WHEN** a human starts a run on `demo` in `/src/repo`
- **THEN** `/src/repo.worktrees/demo` is a worktree on the new branch `change/demo`, created from `main`, and holds `.refinery/runs/demo.1.json`

#### Scenario: A new run after a cancel continues the branch
- **GIVEN** run `demo.1` was cancelled, and its worktree `/src/repo.worktrees/demo` has a commit on `change/demo`
- **WHEN** a human starts a run on `demo`
- **THEN** run `demo.2` uses the same worktree, and that commit is still `HEAD`

#### Scenario: Branch exists without a worktree
- **GIVEN** branch `change/demo` exists and `/src/repo.worktrees/demo` does not
- **WHEN** a human starts a run on `demo`
- **THEN** the worktree is added on the existing branch, and no new branch is created

### Requirement: Worktree creation fails closed
If the worktree cannot be created or reused, the start SHALL be refused and
no run SHALL be created. Examples: the path exists but is not a worktree on
the branch, the branch is checked out elsewhere, or a git call fails. A start
refused because the change already has an unfinished run SHALL be refused
before any git call.

#### Scenario: Branch checked out in the root
- **GIVEN** root `/src/repo` has `change/demo` checked out
- **WHEN** a human starts a run on `demo` in `/src/repo`
- **THEN** the start is refused naming the branch, and no run is created

#### Scenario: Path taken by something else
- **GIVEN** `/src/repo.worktrees/demo` is an ordinary directory
- **WHEN** a human starts a run on `demo`
- **THEN** the start is refused and the directory is untouched

#### Scenario: Change already running
- **GIVEN** change `demo` has an unfinished run
- **WHEN** a human starts another run on `demo`
- **THEN** the start is refused naming the existing run, and no git command runs

### Requirement: The TLA+ tool jar is shared
When the root has `tools/tla2tools.jar` and the worktree has none, factoryd
SHALL symlink the root's jar into the worktree. The worktree SHALL stay
clean.

#### Scenario: Jar linked
- **GIVEN** root `/src/repo` has `tools/tla2tools.jar`
- **WHEN** a run on `demo` starts
- **THEN** `/src/repo.worktrees/demo/tools/tla2tools.jar` resolves to the root's jar, and `git status` in the worktree shows nothing

### Requirement: Records in worktree directories are discovered
At startup, factoryd SHALL recover and index run records found under each
configured root, and under each direct subdirectory of `<root
parent>/<root name>.worktrees/`. Of two copies of a record, the one with the
higher `seq` SHALL win.

#### Scenario: Restart keeps a run in a worktree
- **GIVEN** run `demo.1` in `tests_review`, with its record in `/src/repo.worktrees/demo`
- **WHEN** factoryd restarts with root `/src/repo`
- **THEN** `demo.1` is listed in `tests_review`, and its worktree is `/src/repo.worktrees/demo`
