# Spec Delta

## MODIFIED Requirements

### Requirement: Token roles
A token SHALL carry exactly one role: `human`, `runner`, or `agent` bound to
one run. No token SHALL carry the factoryd role, which only factoryd itself
acts as.

#### Scenario: Role is fixed at issue
- **WHEN** a human token is created
- **THEN** it can call `start_workflow` and no agent tool

#### Scenario: Runner token is only for the runner API
- **GIVEN** a runner token
- **WHEN** it is used on `/api/runs` and on the MCP tool `get_task`
- **THEN** the API answers 403, the tool call fails, and nothing changes

### Requirement: Human tokens from the admin command
`refinery token create --role human` SHALL print a new human token once, and
`refinery token create --role runner` a new runner token once. `refinery
token list` SHALL show each token's id, role, run, creation time and
revocation state, and never the secret. `refinery token revoke <id>` SHALL
make that token fail from then on.

#### Scenario: Create and revoke
- **GIVEN** a human token created with the admin command
- **WHEN** it is revoked by id
- **THEN** requests with it are rejected with 401, and `token list` shows it revoked

#### Scenario: Create a runner token
- **WHEN** `refinery token create --role runner` is run
- **THEN** it prints a token starting with `rft_r_`, and `token list` shows it with role `runner`

#### Scenario: Agent tokens cannot be created by hand
- **WHEN** `refinery token create --role agent` is run
- **THEN** it fails, explaining that agent tokens come from job claims

### Requirement: Agent tokens live and die with their run
Each job claim SHALL issue an agent token bound to the job's run, and SHALL
revoke that run's other agent tokens. The token SHALL work only while that
run exists and is not `archived` or `cancelled`, and only until its job is
finished or released.

#### Scenario: Token of a cancelled run
- **GIVEN** the agent token of run `demo.1`
- **WHEN** `demo.1` is cancelled
- **THEN** the token's next request is rejected with 401

#### Scenario: Token of a finished job
- **GIVEN** job `demo.1.j1` with its agent token
- **WHEN** its runner finishes the job
- **THEN** the token's next request is rejected with 401

#### Scenario: A new claim revokes the old token
- **GIVEN** job `demo.1.j1` was released, and its token was somehow still in use
- **WHEN** job `demo.1.j2` is claimed
- **THEN** only `demo.1.j2`'s token works for `demo.1`
