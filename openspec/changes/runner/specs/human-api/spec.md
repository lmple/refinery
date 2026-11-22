# Spec Delta

## MODIFIED Requirements

### Requirement: Reading runs
The API SHALL list runs with `GET /api/runs`, where `?waiting=1` gives the
review queue. It SHALL show one run with `GET /api/runs/{id}`: its phase,
frozen commits, last decision, PR, publishing error and latest gate. It SHALL
also show its job: state (`waiting`, `running`, `stuck` or `none`), job id,
runner, the runner's last-seen time, attempts and log path.

#### Scenario: Show a run
- **GIVEN** run `demo.1` in `spec_review` with a PR
- **WHEN** a human GETs `/api/runs/demo.1`
- **THEN** the response is 200 with phase `spec_review`, the PR URL, and job state `none`

#### Scenario: Show a running job
- **GIVEN** run `demo.1` in `impl`, with job `demo.1.j3` held by runner `x`
- **WHEN** a human GETs `/api/runs/demo.1`
- **THEN** the job is `running`, with id `demo.1.j3`, runner `x` and its log path

### Requirement: Acting on runs
The API SHALL accept these operations:

| Method and path | Does |
|---|---|
| `POST /api/runs` | start a run (`change`, `root`); returns run id, phase, worktree |
| `POST /api/runs/{id}/approve` | approve, with an optional `note` |
| `POST /api/runs/{id}/reject` | reject, with a `note` |
| `POST /api/runs/{id}/cancel` | cancel |
| `POST /api/runs/{id}/resolve` | resolve an escalation (`to`) |
| `POST /api/runs/{id}/retry-archive` | retry a failed archive |

#### Scenario: Approve through the API
- **GIVEN** run `demo.1` in `spec_review`
- **WHEN** a human POSTs to `/api/runs/demo.1/approve`
- **THEN** the response is 200 with the run now in `tests`

#### Scenario: Start through the API
- **WHEN** a human POSTs `{"change": "demo", "root": "/src/repo"}` to `/api/runs`
- **THEN** the response is 200 with run id `demo.1`, phase `spec` and worktree `/src/repo.worktrees/demo`, and has no `agent_token`

### Requirement: The refine CLI
`refine` SHALL offer `start` (`--root`, default the current directory),
`runs` (`--waiting` for the queue), `status`, `approve`, `reject --note`,
`cancel`, `resolve --to`, `retry-archive` and `retry`, each calling the API.
It SHALL read the server URL from `REFINE_URL` (default
`http://127.0.0.1:8740`) and the token from `REFINE_TOKEN`. It SHALL exit
non-zero, printing the API's error, when a request fails.

#### Scenario: Approve from the CLI
- **GIVEN** run `demo.1` in `tests_review` and a human token in `REFINE_TOKEN`
- **WHEN** `refine approve demo.1` runs
- **THEN** it exits 0, prints the run's new phase `impl`, and the approved tests commit is the worktree's `HEAD`

#### Scenario: Start from the CLI
- **GIVEN** the current directory is configured root `/src/repo`
- **WHEN** `refine start demo` runs
- **THEN** it exits 0 and prints `demo.1: spec` and the worktree `/src/repo.worktrees/demo`, and no token

#### Scenario: Retry from the CLI
- **GIVEN** run `demo.1` is stuck
- **WHEN** `refine retry demo.1` runs
- **THEN** it exits 0, and `refine status demo.1` shows the job `waiting`

#### Scenario: Refused from the CLI
- **WHEN** `refine reject demo.1` runs without `--note`
- **THEN** it exits non-zero and the run is unchanged

#### Scenario: No token
- **WHEN** `refine runs` runs with `REFINE_TOKEN` unset
- **THEN** it exits non-zero, saying `REFINE_TOKEN` is needed
