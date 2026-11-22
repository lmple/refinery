# Spec Delta

## ADDED Requirements

### Requirement: Runner configuration
`refinery runner` SHALL be configured only from the environment, with these
variables and defaults:

| Variable | Default |
|---|---|
| `REFINE_URL` | `http://127.0.0.1:8740` |
| `REFINERY_RUNNER_TOKEN` | required |
| `REFINERY_RUNNER_DIR` | `$XDG_DATA_HOME/refinery-runner` |
| `REFINERY_RUNNER_MAX_JOBS` | 1 |
| `REFINERY_RUNNER_POLL` | 10 seconds |
| `REFINERY_RUNNER_CLAIM_INTERVAL` | 5 seconds |
| `REFINERY_AGENT_TIMEOUT` | 4 hours |

#### Scenario: Defaults
- **GIVEN** only `REFINERY_RUNNER_TOKEN` is set
- **WHEN** the runner reads its configuration
- **THEN** it uses factoryd at `http://127.0.0.1:8740`, one job at a time, polls every 10 seconds and gives agents 4 hours

### Requirement: The runner needs its token
`refinery runner` SHALL exit non-zero, saying the token is needed, when
`REFINERY_RUNNER_TOKEN` is unset or empty.

#### Scenario: No token
- **WHEN** `refinery runner` starts with `REFINERY_RUNNER_TOKEN` unset
- **THEN** it exits non-zero, naming `REFINERY_RUNNER_TOKEN`

### Requirement: Reap, then hello, then claim
On startup, the runner SHALL:
1. kill every process group in its own registry (in `REFINERY_RUNNER_DIR`)
   that is verifiably still its own;
2. say hello to factoryd, retrying until it answers;
3. only then claim jobs.

The runner's registry SHALL be separate from factoryd's.

#### Scenario: Restart after a crash with an agent left running
- **GIVEN** runner `x` crashed while its agent for job `demo.1.j1` kept running
- **WHEN** `x` starts again
- **THEN** that agent's process group is gone before `x` says hello, and `x` claims `demo.1` again afterwards

#### Scenario: factoryd restart does not kill agents
- **GIVEN** a runner's agent is running
- **WHEN** factoryd restarts and reaps its registry
- **THEN** the agent is still running

### Requirement: Launching the agent
For each claimed job, the runner SHALL:
- write an MCP config file naming server `refinery`, factoryd's MCP URL and
  the job's agent token, with mode 0600 from its creation, in
  `REFINERY_RUNNER_DIR/jobs/<job>/`;
- run the Claude Code adapter's argv through the supervisor, in the run's
  worktree, with the agent time limit;
- stream the output to `jobs/<job>/agent.log`.


#### Scenario: Agent launched
- **GIVEN** a fake `claude` on `PATH` that records its argv, environment and working directory
- **WHEN** the runner claims job `demo.1.j1`
- **THEN** the fake ran in the run's worktree with `-p`, `--mcp-config <file>`, `--strict-mcp-config` and `--output-format stream-json`, and the file had mode 0600 and the job's token

### Requirement: The agent never sees other tokens
The agent's environment SHALL NOT contain `REFINERY_RUNNER_TOKEN` or
`REFINE_TOKEN`, even when the runner's own environment has them.

#### Scenario: Tokens stripped
- **GIVEN** a runner whose environment has `REFINERY_RUNNER_TOKEN` and `REFINE_TOKEN`
- **WHEN** it launches an agent
- **THEN** the agent's recorded environment has neither variable

### Requirement: Concurrent jobs are bounded
The runner SHALL run at most `REFINERY_RUNNER_MAX_JOBS` jobs at once, and
SHALL not claim while at that limit.

#### Scenario: One job at a time by default
- **GIVEN** two runs wanting an agent, and a runner with default settings
- **WHEN** the runner has claimed one
- **THEN** it claims no second job until the first ends

### Requirement: The Claude Code adapter
The adapter SHALL build this argv:

```
<REFINERY_AGENT_COMMAND> -p <prompt> --mcp-config <file> --strict-mcp-config
  --output-format stream-json --verbose
  --permission-mode <REFINERY_AGENT_PERMISSION_MODE> -n <run-id>
```

It SHALL add `--model` when `REFINERY_AGENT_MODEL` is set, and
`--max-budget-usd` when `REFINERY_AGENT_MAX_BUDGET_USD` is set. The command
SHALL default to `claude`, and the permission mode to `auto`. The prompt
SHALL tell the agent to call `get_task`, follow it, and stop when it is not
the agent's turn.

#### Scenario: Defaults
- **WHEN** the adapter builds the argv for run `demo.1` with no variables set
- **THEN** it starts with `claude -p`, has `--permission-mode auto` and `-n demo.1`, and has no `--model`

### Requirement: Stop when factoryd says so
While its agent runs, the runner SHALL poll the job at the poll interval.
On `stop`, it SHALL cancel the agent call, so that the supervisor terminates
and then kills the agent's process group. While factoryd is unreachable, it
SHALL keep the agent running and keep polling.

#### Scenario: Cancel kills the agent
- **GIVEN** a running agent for job `demo.1.j1`
- **WHEN** the human cancels `demo.1`
- **THEN** within two poll intervals and the supervisor's grace period, the agent's process group is gone

#### Scenario: factoryd down
- **GIVEN** a running agent
- **WHEN** factoryd is stopped for three poll intervals and started again
- **THEN** the agent was never stopped, and polling resumes

### Requirement: Reporting the end of a job
When the agent call ends, the runner SHALL delete the job's MCP config file
and then send `finish`, with the supervisor's outcome and exit code and the
log path. It SHALL retry the finish with backoff until factoryd answers or
the runner shuts down.

#### Scenario: Agent exits without submitting
- **GIVEN** a fake `claude` that exits 1 at once
- **WHEN** the runner runs job `demo.1.j1`
- **THEN** factoryd records `Finish` with outcome `exited` and exit code 1, the MCP config file is gone, and the job's token no longer works

#### Scenario: Agent exceeds the time limit
- **GIVEN** `REFINERY_AGENT_TIMEOUT` of 1 second and a fake `claude` that sleeps
- **WHEN** the runner runs a job
- **THEN** the agent's group is gone and `Finish` has outcome `timed_out`

### Requirement: Runner shutdown
On SIGINT or SIGTERM, the runner SHALL:
1. cancel every running agent;
2. delete their MCP config files;
3. send `release` for each job, best effort;
4. exit.

#### Scenario: Shutdown with a running agent
- **GIVEN** a running agent for job `demo.1.j1`
- **WHEN** the runner gets SIGTERM
- **THEN** the agent's group is gone, the job log has `Release` with reason `runner_shutdown`, and `demo.1` is not stuck
