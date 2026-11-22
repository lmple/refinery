# Spec Delta

## Purpose

Defines the bearer tokens through which humans and agents reach factoryd:
- what roles they carry;
- how they are issued and stored;
- how agent tokens are bound to a single run and stop working when it ends;
- how any token is revoked.

## ADDED Requirements

### Requirement: Every request needs a valid token
Every HTTP request to `/mcp` SHALL carry a bearer token that factoryd issued
and has not revoked. A request without one SHALL be rejected with HTTP 401
before any tool runs.

#### Scenario: No token
- **WHEN** a client calls `/mcp` without an `Authorization` header
- **THEN** the response is 401 and nothing changes

#### Scenario: Unknown token
- **WHEN** a client presents `rft_a_00000000_bogus`
- **THEN** the response is 401

### Requirement: Token roles
A token SHALL carry exactly one role: `human`, or `agent` bound to one run.
No token SHALL carry the factoryd role, which only factoryd itself acts as.

#### Scenario: Role is fixed at issue
- **WHEN** a human token is created
- **THEN** it can call `start_workflow` and no agent tool

### Requirement: Human tokens from the admin command
`refinery token create --role human` SHALL print a new human token once.
`refinery token list` SHALL show each token's id, role, run, creation time
and revocation state, and never the secret. `refinery token revoke <id>`
SHALL make that token fail from then on.

#### Scenario: Create and revoke
- **GIVEN** a human token created with the admin command
- **WHEN** it is revoked by id
- **THEN** requests with it are rejected with 401, and `token list` shows it revoked

#### Scenario: Agent tokens cannot be created by hand
- **WHEN** `refinery token create --role agent` is run
- **THEN** it fails, explaining that agent tokens come from `start_workflow`

### Requirement: Agent tokens live and die with their run
`start_workflow` SHALL issue an agent token bound to the new run. The token
SHALL work only while that run exists and is not `archived` or `cancelled`.

#### Scenario: Token of a cancelled run
- **GIVEN** the agent token of run `demo.1`
- **WHEN** `demo.1` is cancelled
- **THEN** the token's next request is rejected with 401

### Requirement: Only hashes are stored
factoryd SHALL store a token's sha256 hash, never the token itself, in
`<data>/tokens/`, in files readable by the owner only. Verification SHALL use
a constant-time comparison.

#### Scenario: Token files hold no secret
- **WHEN** a token is created
- **THEN** no file under the data directory contains the token string, and its token file has mode 0600
