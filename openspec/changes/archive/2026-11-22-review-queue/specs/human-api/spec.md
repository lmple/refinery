# Spec Delta

## Purpose

Defines the HTTP JSON API through which humans act on runs, and the `refine`
command-line client built on it. Both are authenticated with human tokens,
and both refuse with clear errors rather than change anything they shouldn't.

## ADDED Requirements

### Requirement: API authentication
Every request under `/api` SHALL carry a human bearer token. A missing,
unknown or revoked token SHALL get 401. A valid agent token SHALL get 403.

#### Scenario: Agent token on the API
- **WHEN** a request to `/api/runs` carries an agent token
- **THEN** the response is 403 and nothing changes

#### Scenario: No token
- **WHEN** a request to `/api/runs` carries no token
- **THEN** the response is 401

### Requirement: Reading runs
The API SHALL list runs with `GET /api/runs`, where `?waiting=1` gives the
review queue. It SHALL show one run with `GET /api/runs/{id}`: its phase,
frozen commits, last decision, PR, publishing error and latest gate.

#### Scenario: Show a run
- **GIVEN** run `demo.1` in `spec_review` with a PR
- **WHEN** a human GETs `/api/runs/demo.1`
- **THEN** the response is 200 with phase `spec_review` and the PR URL

### Requirement: Acting on runs
The API SHALL accept these operations:

| Method and path | Does |
|---|---|
| `POST /api/runs` | start a run (`change`, `worktree`); returns the agent token once |
| `POST /api/runs/{id}/approve` | approve, with an optional `note` |
| `POST /api/runs/{id}/reject` | reject, with a `note` |
| `POST /api/runs/{id}/cancel` | cancel |
| `POST /api/runs/{id}/resolve` | resolve an escalation (`to`) |
| `POST /api/runs/{id}/retry-archive` | retry a failed archive |

#### Scenario: Approve through the API
- **GIVEN** run `demo.1` in `spec_review`
- **WHEN** a human POSTs to `/api/runs/demo.1/approve`
- **THEN** the response is 200 with the run now in `tests`

### Requirement: API errors
A refused operation SHALL return a JSON body `{"error": "<reason>"}`, with
status:
- 404 for an unknown run;
- 409 for a refused transition or a wrong phase;
- 422 for an invalid request.

#### Scenario: Unknown run
- **WHEN** a human POSTs to `/api/runs/nope.1/approve`
- **THEN** the response is 404 with an `error` naming `nope.1`

#### Scenario: Wrong phase
- **GIVEN** run `demo.1` in `impl`
- **WHEN** a human POSTs to `/api/runs/demo.1/approve`
- **THEN** the response is 409 and the run is unchanged

### Requirement: The refine CLI
`refine` SHALL offer `start`, `runs` (`--waiting` for the queue), `status`,
`approve`, `reject --note`, `cancel`, `resolve --to` and `retry-archive`, each
calling the API. It SHALL read the server URL from `REFINE_URL` (default
`http://127.0.0.1:8740`) and the token from `REFINE_TOKEN`. It SHALL exit
non-zero, printing the API's error, when a request fails.

#### Scenario: Approve from the CLI
- **GIVEN** run `demo.1` in `tests_review` and a human token in `REFINE_TOKEN`
- **WHEN** `refine approve demo.1` runs
- **THEN** it exits 0, prints the run's new phase `impl`, and the approved tests commit is the worktree's `HEAD`

#### Scenario: Refused from the CLI
- **WHEN** `refine reject demo.1` runs without `--note`
- **THEN** it exits non-zero and the run is unchanged

#### Scenario: No token
- **WHEN** `refine runs` runs with `REFINE_TOKEN` unset
- **THEN** it exits non-zero, saying `REFINE_TOKEN` is needed
