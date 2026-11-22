# Proposal: runner

## Why

factoryd enforces every phase, but a run still needs the human for chores
the factory should do itself:
- creating the branch by hand;
- copying the agent token into `claude mcp add`;
- launching Claude Code and relaunching it after every review.

A token copied into the wrong command (as at the start of `runner.1`) leaves
the agent locked out. This change completes the MVP (docs/vision.md): you type
`refine start <change>`, and factoryd and a local runner do the rest, up to
the review gates.

## What Changes

- **Worktree and branch at start.** `start_workflow` and `refine start` take a
  configured root (usually on `main`). factoryd creates or reuses the worktree
  `<root>.worktrees/<change>` on branch `change/<change>`, and the run's record
  lives there. The worktree directories are discovered at startup, so a
  restart loses no run. Starting no longer prints an agent token.
- **Jobs.** While a run is in an agent phase (`spec`, `model`, `tests`,
  `impl`), it wants an agent. A runner claims the run as a *job*, and the
  claim hands it a fresh agent token for that run. A job ends when its agent
  process exits.
  - **Progress.** The run moved during the job: the next job, if any, is a
    new turn.
  - **Failed attempt.** The run didn't move. After `REFINERY_AGENT_ATTEMPTS`
    failed attempts (default 2), the run is *stuck*: it waits in your queue
    until `refine retry`.
- **Jobs are released only by their runner.** A job ends when its runner
  finishes it, releases it on shutdown, or says hello after a restart, once
  it has killed the agents it left behind. There are no leases. So even
  across runner crashes, no run ever has two agents.
- **Runner process.** `refinery runner` authenticates with a new `runner`
  token. It:
  - reaps its own orphans, then says hello;
  - claims jobs (default one at a time);
  - launches `claude -p` headless in the worktree through the supervisor,
    with the agent token in a private MCP config file and a time limit;
  - polls each job, and kills the agent when the run is cancelled or no
    longer the agent's turn;
  - reports how the agent ended.

  The agent's output goes to a log per job, shown in `refine status`.
- **Runner API**, under `/api/runner/` with runner tokens only: `hello`,
  `claim`, `poll`, `finish` and `release`.
- **Human side:**
  - `refine retry <run>` and `POST /api/runs/{id}/retry` re-queue a stuck run;
  - `refine status` shows the job (waiting, running, stuck), its runner,
    attempts and log;
  - the review queue includes stuck runs.
- **Tokens:**
  - a third role, `runner`, created with `refinery token create --role
    runner`;
  - agent tokens are now issued per job, and revoked when the job ends;
  - a new agent token revokes the run's older ones.
- **TLA+:** `spec/Runner.tla` models the job claims, runner crash and
  restart, orphaned agents and reaping, failed attempts and stuck runs, and
  cancellation. It comes with `Runner.cfg`, `Runner_big.cfg` and
  `Runner_reach.cfg`.
- **`docs/vision.md`:** factoryd, not the runner, creates the worktree and
  branch; the MVP start text matches.

## Capabilities

### New Capabilities

- `run-worktrees`: the worktree and branch factoryd creates for a run, and
  how records in worktrees are discovered.
- `agent-jobs`: factoryd's side: when a run wants an agent, claims, attempts,
  stuck runs, retry, and the runner API.
- `runner`: the runner process: reap and hello, claiming, launching Claude
  Code, polling and killing, reporting, and shutdown.

### Modified Capabilities

- `access-tokens`: the `runner` role; agent tokens are issued per job and
  revoked at its end.
- `agent-tools`: `start_workflow` takes a root, creates the worktree and
  returns no token.
- `human-api`: start returns no token; `retry`; a run shows its job;
  `refine retry`.
- `review-publishing`: the branch rule holds by construction, since the
  branch is always `change/<change>`.
- `review-queue`: stuck runs are in the queue.
- `factoryd-server`: startup discovers records in worktree directories too.

## Impact

- **New:**
  - `src/refinery/jobs/` (the job log, the job state fold, and the claim,
    finish, release and retry logic);
  - `src/refinery/runner/` (the runner loop and its HTTP client);
  - `src/refinery/adapters/claude_code.py` (the argv and MCP config of a
    headless Claude Code run);
  - `src/refinery/factoryd/runner_api.py`;
  - `refinery runner` in `factoryd/cli.py`.
- **Changed:**
  - `auth.tokens` (the runner role, revoking a run's agent tokens);
  - `Workflow.start_workflow` (worktree creation, no token);
  - `review.repo` (`worktree add`, branch lookup);
  - `run.record.find_records` callers (worktree directories);
  - `Review` (`retry`, the job in `show`, the queue);
  - `refine` (`retry`, the start output, the job in `status`).
- **External tools:** `claude`, already used on this machine, run through
  the supervisor. No new Python dependencies: the runner's HTTP client is
  `httpx2`, as in `refine`.
- **Data:**
  - factoryd: `<data>/runs/<id>/jobs.jsonl`;
  - the runner: its own directory (`REFINERY_RUNNER_DIR`, default
    `$XDG_DATA_HOME/refinery-runner`), holding its process registry and
    `jobs/<job-id>/{agent.log,mcp.json}`.
