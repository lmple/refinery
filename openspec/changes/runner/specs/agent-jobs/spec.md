# Spec Delta

## ADDED Requirements

### Requirement: Runs in an agent phase want an agent
A run SHALL want an agent when:
- its phase is `spec`, `model`, `tests` or `impl`;
- it has no open job;
- it is not stuck.

No other run SHALL be handed to a runner.

#### Scenario: Waiting for review
- **GIVEN** run `demo.1` in `spec_review`
- **WHEN** a runner claims
- **THEN** it gets no job for `demo.1`

#### Scenario: After a rejection
- **GIVEN** run `demo.1` was in `tests_review`, and the human rejected it, so it is in `tests`
- **WHEN** a runner claims
- **THEN** it gets a job for `demo.1` in phase `tests`

### Requirement: Claiming a job
`POST /api/runner/claim` SHALL hand the calling runner a job for the run that
has wanted an agent longest, or answer 204 when none does. Claims SHALL be
serialised, so that two runners never get the same run. A claim SHALL return
the job id, run id, change, phase, worktree, agent token, factoryd's MCP URL
and the agent prompt.

#### Scenario: Claim
- **GIVEN** run `demo.1` in `spec` with no job
- **WHEN** runner `x` claims
- **THEN** it gets job `demo.1.j1` with an agent token that works for `demo.1`, and the job log's last event is `Claim` by `x`

#### Scenario: Two runners, one run
- **GIVEN** one run wanting an agent
- **WHEN** runners `x` and `y` claim at the same time
- **THEN** exactly one gets the job, and the other gets 204

### Requirement: A claim issues the run's only agent token
A claim SHALL issue a new agent token bound to the run, and revoke every
other agent token of that run. It SHALL then append a `Claim` event to the
run's job log, with the job id, the runner, the token id and the run's `seq`.
If the event cannot be appended, the new token SHALL be revoked and the
claim refused.

#### Scenario: Claim recorded
- **GIVEN** run `demo.1` in `spec` with `seq` 1
- **WHEN** runner `x` claims it
- **THEN** the job log's last event is `Claim` with job `demo.1.j1`, runner `x`, the new token's id and `claim_seq` 1

#### Scenario: Claim cannot be recorded
- **GIVEN** the run's job log cannot be written
- **WHEN** a runner claims
- **THEN** the answer is 503, and the token issued for the claim no longer works

### Requirement: Jobs end only through their runner
A job SHALL stay open until its own runner does one of these:
- finishes it (`POST /api/runner/jobs/{job}/finish`, with the agent's outcome
  and exit code);
- releases it (`POST /api/runner/jobs/{job}/release`);
- says hello (`POST /api/runner/hello`), which releases every open job of
  that runner;
- claims without listing it among the jobs it holds (see "Claims reconcile
  the runner's jobs").

#### Scenario: Hello after a restart
- **GIVEN** runner `x` holds open job `demo.1.j1` and restarts
- **WHEN** `x` says hello
- **THEN** the job log has `Release` with reason `runner_restart`, the job's token no longer works, and `demo.1` wants an agent again

#### Scenario: A silent runner keeps its job
- **GIVEN** runner `x` holds open job `demo.1.j1` and has not called for an hour
- **WHEN** runner `y` claims
- **THEN** `y` does not get `demo.1`

### Requirement: Closing a job revokes its token
Every way a job closes SHALL revoke the job's agent token. A runner SHALL
get 404 for another runner's job. Finishing or releasing a job that is
already closed SHALL succeed and change nothing.

#### Scenario: Finish twice
- **GIVEN** job `demo.1.j1` was finished
- **WHEN** its runner sends `finish` again
- **THEN** the answer is 200, and the job log is unchanged

#### Scenario: Another runner's job
- **GIVEN** job `demo.1.j1` held by runner `x`
- **WHEN** runner `y` sends `release` for it
- **THEN** the answer is 404, and the job is still open

### Requirement: Claims reconcile the runner's jobs
Every claim SHALL carry the ids of the open jobs the runner is running. Before
choosing a run, factoryd SHALL release, with reason `lost`, every open job of
that runner not in the list: its claim was recorded, but the runner never got
the response. This SHALL happen even when the claim then answers 204.

#### Scenario: A claim response is lost
- **GIVEN** factoryd recorded `Claim` of job `demo.1.j1` by runner `x`, but `x` never got the response
- **WHEN** `x` claims again, listing no jobs
- **THEN** the job log has `Release` of `demo.1.j1` with reason `lost`, its token no longer works, and `x` gets job `demo.1.j2` for `demo.1`

#### Scenario: Jobs the runner lists stay open
- **GIVEN** runner `x` is running job `a.1.j1`
- **WHEN** `x` claims, listing `a.1.j1`
- **THEN** `a.1.j1` is still open

### Requirement: Failed attempts and stuck runs
A finish SHALL be a failed attempt when the run's `seq` at the finish equals
its job's claim `seq`: nothing moved the run during the job. A run's
attempts SHALL be the failed attempts after its last `Retry` whose claim
`seq` is the run's current `seq`. A run SHALL be stuck when its attempts
reach `REFINERY_AGENT_ATTEMPTS`, which defaults to 2. Released jobs SHALL
not count as attempts.

#### Scenario: Progress is not a failure
- **GIVEN** job `demo.1.j1`, claimed in `spec`
- **WHEN** the agent submits (the run moves to `model`) and the runner finishes the job
- **THEN** the finish is not a failed attempt, and `demo.1` wants an agent in `model`

#### Scenario: Two failures make a run stuck
- **GIVEN** run `demo.1` in `impl`, and one failed attempt for that turn
- **WHEN** a second job finishes without the run moving
- **THEN** `demo.1` is stuck, and no runner gets it

#### Scenario: A released job is not an attempt
- **GIVEN** run `demo.1` with one failed attempt
- **WHEN** its next job is released by a runner shutdown
- **THEN** `demo.1` is not stuck and wants an agent

### Requirement: Retrying a stuck run
`POST /api/runs/{id}/retry` (human) SHALL append `Retry` to the job log of a
stuck run, resetting its attempts. For a run that is not stuck, it SHALL be
refused with 409.

#### Scenario: Retry
- **GIVEN** run `demo.1` is stuck
- **WHEN** the human retries it
- **THEN** `demo.1` wants an agent again

#### Scenario: Retry a run that is not stuck
- **GIVEN** run `demo.1` in `impl` with no failed attempts
- **WHEN** the human retries it
- **THEN** the answer is 409 and the job log is unchanged

### Requirement: Polling tells the runner when to stop
`POST /api/runner/jobs/{job}/poll` SHALL answer `stop` with a reason when:
- the job's run is terminal;
- the run is in no agent phase;
- the job is not open.

Otherwise it SHALL answer `continue`. Each poll SHALL record the runner's
last-seen time.

#### Scenario: Cancelled run
- **GIVEN** open job `demo.1.j1`, and the human cancels `demo.1`
- **WHEN** the runner polls the job
- **THEN** the answer is `stop`, giving the run as cancelled

#### Scenario: Agent submitted into review
- **GIVEN** open job `demo.1.j1`, and the run moved to `tests_review`
- **WHEN** the runner polls
- **THEN** the answer is `stop`

### Requirement: The job log
Every job event SHALL be appended and fsynced to
`<data>/runs/<run-id>/jobs.jsonl` before its effect is reported. The event
names SHALL be `Claim`, `Finish`, `Release` and `Retry`, matching
`spec/Runner.tla`'s actions. The job state SHALL be computed from that log
and the run record alone.

#### Scenario: Restart keeps a stuck run stuck
- **GIVEN** run `demo.1` is stuck
- **WHEN** factoryd restarts
- **THEN** `demo.1` is still stuck, and no runner gets it

### Requirement: The runner API needs a runner token
Every request under `/api/runner/` SHALL carry a runner token. A missing or
unknown token SHALL get 401. A human or agent token SHALL get 403.

#### Scenario: Human token on the runner API
- **WHEN** a request to `/api/runner/claim` carries a human token
- **THEN** the response is 403 and no job is claimed
