# Design: runner

## Context

Today a run lives in one of factoryd's `--root`s, already on a branch the
human created. `start_workflow` returns an agent token, which the human
registers with `claude mcp add` before launching Claude Code by hand, at every
agent turn. See proposal.md for motivation, and the six spec deltas for the
requirements. The model is `spec/Runner.tla`.

What exists:
- `Supervisor` (process groups, time limits, a registry, a reap at startup);
- `TokenStore` (human and agent roles);
- `Workflow` and `Review`, the MCP and human services;
- `Repo` (git through the supervisor);
- `RunStore` (records found under roots).

## Goals / Non-Goals

**Goals:**
- **`refine start <change>` is the whole start.** No branch, token or launch
  by hand.
- **At most one agent per run, ever.** This holds across runner crashes,
  factoryd crashes and cancellation.
- **A stuck agent reaches the human.** It is neither retried for ever nor
  dropped silently.
- **The run lifecycle is unchanged.** Jobs live beside runs; `Run.tla`, its
  events and trace validation are untouched.

**Non-Goals:**
- **tmux or interactive sessions.** The agent runs headless; you follow its
  log.
- **Runners on other machines.** The worktree is on factoryd's machine
  (vision: MVP, one machine). The API doesn't preclude remote runners.
- **Other agent adapters** (OpenCode, pi, Mistral Vibe).
- **Removing worktrees** after a run ends. You do that with `git worktree
  remove` when you want to.
- **Trace validation of the job log.** Its event names match `Runner.tla`'s
  actions (`Release` covers the model's `Hello`, `Shutdown` and claim
  reconciliation), so it can be added later.

## Approaches

Two questions shaped this change. You picked the first answer to each.

1. **How long an agent process lives.**
   - **One headless job per agent turn (chosen).** `claude -p` runs until the
     run stops being the agent's turn, and then exits. A new turn, such as
     after a review, is a new job. Every job ends by itself, which makes it
     easy to model and easy to kill. The agent restarts with fresh context,
     which `get_task`, the repo and the human's last note provide.
   - **One session per run, in tmux.** You can watch it and its context
     carries over, but it idles through reviews. It also lives outside our
     process group, so "no agent left after cancel" would rest on tmux.
2. **An agent that exits without moving the run.**
   - **One automatic retry, then stuck (chosen).** Transient failures get a
     second chance, and a deterministic one (a bad prompt, a missing tool,
     a task too big for the time limit) reaches you after two tries. "Stuck"
     is a job state, not a run phase.
   - **Escalate.** An escalation is for spec or test bugs, so a crashed agent
     would blur the queue and the run's event log.

Settled without asking (calls made while writing this design; check them at
the review gate):
- factoryd, not the runner, creates the worktree (D1);
- jobs have no leases (D4);
- the agent's permission mode defaults to `auto` (D7).

## Decisions

### D1. The worktree is created at start, by factoryd

`start_workflow(change, root)` and `POST /api/runs {change, root}`:

1. **Check.** `root` must be a configured root. `change` must have no
   unfinished run; this is checked before touching git, so a refused start
   creates nothing.
2. **Branch and path.** The branch is `change/<change>`, and the path is
   `<root parent>/<root name>.worktrees/<change>`. For the root
   `~/sources/refinery`, that's `~/sources/refinery.worktrees/runner`.
3. **Create or reuse:**
   - if the path is already a worktree on the branch, it is reused (a new run
     after a cancel continues the branch's work);
   - otherwise, if the branch exists, `git worktree add <path> <branch>`;
   - otherwise, `git worktree add -b <branch> <path> <default branch>`.

   A path that exists but is something else, or a branch checked out
   elsewhere, is refused and no run is created.
4. **Shared tool.** If `<root>/tools/tla2tools.jar` exists and the worktree
   has none, it is symlinked in. The file is gitignored, so the worktree
   stays clean, and `make tla` doesn't download it again.
5. **Start.** `StartRun`, with the record in the new worktree.

All git calls go through `Repo` and the supervisor. A git failure refuses
the start (fail closed).

Why factoryd rather than the runner (the vision said the runner): the run
record must exist at `StartRun`, since the event comes before the record (D5
of `run-lifecycle`). factoryd already owns git on worktrees for publishing,
and with one machine the runner gains nothing by doing it. `docs/vision.md`
is updated.

Rejected alternative: worktrees inside the root (`.worktrees/`). pytest,
ruff and pyright run from the root would pick them up.

### D2. Record discovery

`RunStore.open` and the index rebuild search for run records under every
root, and also under every direct subdirectory of `<root>.worktrees/`. This
is a glob rather than `git worktree list`: no subprocess at startup, and the
layout is factoryd's own. Of two copies of a record, the higher `seq` still
wins.

### D3. The runner token

- **Role.** `Role.RUNNER`, with tokens of the form `rft_r_<id>_<secret>`,
  created with `refinery token create --role runner`.
- **Identity.** The runner's identity is its token id, and jobs record it.
- **Where it works.** Only on `/api/runner/*`. A runner token on `/api` gets
  403, and on MCP every tool fails (wrong role).
- **The other routes.** Human and agent tokens on `/api/runner/*` get 403.

### D4. Jobs: state, attempts, stuck

**The job log.** `<data>/runs/<run-id>/jobs.jsonl` is append-only, with each
event fsynced. Its events match `Runner.tla`'s action names:

| Event | Fields |
|---|---|
| `Claim` | `job`, `runner`, `token_id`, `claim_seq`, `at` |
| `Finish` | `job`, `outcome` (supervisor `Outcome`), `exit_code`, `log`, `finish_seq`, `at` |
| `Release` | `job`, `why` (`runner_restart`, `runner_shutdown`, `lost`), `at` |
| `Retry` | `at` |

The job id is `<run-id>.j<n>`, with n counting this run's claims. The state
is a fold over the log, computed in one pure function (`jobs.state`):
- **Open job:** the last `Claim` with no `Finish` or `Release` after it.
- **Failed attempt:** a `Finish` whose `finish_seq` (the run's `seq` when
  factoryd recorded it) equals its job's `claim_seq`. Nothing moved the run,
  so the agent made no progress.
- **Attempts:** the failed attempts after the last `Retry`, counting only
  those whose `claim_seq` equals the run's current `seq`. A turn that moved
  starts again from zero.
- **Stuck:** attempts ≥ `max_attempts` (`REFINERY_AGENT_ATTEMPTS`, default 2).

A run **wants an agent** when all of these hold:
- it is in an agent phase (`spec`, `model`, `tests`, `impl`);
- it has no open job;
- it is not stuck.

This is level-triggered: computed from the run and its job log at claim
time, so no transition needs a hook. If the job log is lost, the effect is
only that attempts reset.

**Released jobs aren't attempts.** A runner shutdown or crash isn't the
agent's failure.

**Jobs are released only by their runner,** through `finish`, `release`,
`hello`, or a claim that doesn't list the job (D5). With no leases, factoryd never hands a run to a second runner
while the first might still have an agent running. A runner that never comes
back leaves its jobs open: the run shows "running" with a stale "runner last
seen", and you can `refine cancel` it. That's acceptable with one machine.

Rejected alternative: leases with expiry. Supervised agents run in their own
process group, so they outlive a runner crash. A lease that expires would
let a second agent start while the orphan still edits the worktree.
Revoking the token stops the orphan's MCP calls, but not its file writes.

### D5. Runner API (`/api/runner/`, runner tokens)

| Route | Does |
|---|---|
| `POST hello` | Releases (`runner_restart`) every open job of this runner and revokes their agent tokens. The runner sends it after reaping. |
| `POST claim` | Body `{holding: [job ids]}`. 200 with a job, or 204. Under a single claim lock, it first releases (`lost`) this runner's open jobs not in `holding`. It then picks the run that has wanted an agent longest (oldest last event, then run id), issues an agent token for the run, revokes the run's other agent tokens, and appends `Claim`. If the append fails, the token is revoked and the answer is 503. |
| `POST jobs/{job}/poll` | `{"action": "continue"}`, or `{"action": "stop", "why": ...}` when the run is terminal or not in an agent phase, or the job isn't open. It records the runner's last-seen time. |
| `POST jobs/{job}/finish` | Body `{outcome, exit_code, log}`. Appends `Finish` and revokes the job's token. Idempotent: on a closed job it answers 200 and does nothing. |
| `POST jobs/{job}/release` | Appends `Release(runner_shutdown)` and revokes the token. Idempotent. |

A job id that belongs to another runner gets 404.

A claimed job returns:
- `job`, `run_id`, `change`, `phase` and `worktree`;
- `agent_token`;
- `mcp_url` (factoryd's own `/mcp` URL, from its host and port);
- `prompt` (D7).

### D6. The runner process (`refinery runner`)

**Configuration**, from the environment (tokens never come from flags or
files):

| Variable | Default |
|---|---|
| `REFINE_URL` | `http://127.0.0.1:8740` |
| `REFINERY_RUNNER_TOKEN` | required |
| `REFINERY_RUNNER_DIR` | `$XDG_DATA_HOME/refinery-runner` |
| `REFINERY_RUNNER_MAX_JOBS` | 1 |
| `REFINERY_RUNNER_POLL` | 10 s |
| `REFINERY_RUNNER_CLAIM_INTERVAL` | 5 s |
| `REFINERY_AGENT_TIMEOUT` | 4 h |

**The directory.** The runner has its own directory, so its `Supervisor`
registry is separate. factoryd's startup reap must never kill agents, and
the runner's reap must never kill gates.

**Startup:** reap, then `hello` (retried until factoryd answers), then the
loop.

**Loop**, one `TaskGroup`. While fewer than `max_jobs` jobs run, it claims
every claim interval, one claim at a time, listing the ids of the jobs it is
running. That list is how factoryd learns of a claim response the runner
never got (D5); the model found the case (D9). Each job runs in a task:
1. Write `jobs/<job>/mcp.json` with mode 0600, created that way rather than
   chmodded after: a server `refinery` with type `http`, the URL, and the
   `Authorization: Bearer` header.
2. Run the agent argv (D7) through `Supervisor.run`, with `cwd` the worktree,
   `StreamTo(jobs/<job>/agent.log)` and `timeout_s` the agent timeout. Its
   environment is the runner's minus `REFINERY_RUNNER_TOKEN` and
   `REFINE_TOKEN`: the agent never sees the runner's or the human's token.
3. Alongside, poll every poll interval. On `stop`, cancel the agent call:
   the supervisor terminates the group, waits a grace period, then kills it.
   If factoryd is unreachable, keep the agent running and poll again. Its
   MCP calls fail meanwhile anyway.
4. Delete `mcp.json`, then send `finish` with the outcome, retried with
   backoff until factoryd answers or the runner shuts down.

**Shutdown** (SIGINT or SIGTERM): cancel every job task (their agents die),
delete their `mcp.json` files, and send `release` for each. That last step is
best effort, since the next `hello` covers it.

### D7. The Claude Code adapter

`refinery.adapters.claude_code.argv(prompt, mcp_config, run_id, settings)`
builds this argv:

```
claude -p <prompt> --mcp-config <mcp.json> --strict-mcp-config
       --output-format stream-json --verbose --permission-mode <mode>
       -n <run-id> [--model <model>] [--max-budget-usd <usd>]
```

The settings come from these variables:

| Variable | Default |
|---|---|
| `REFINERY_AGENT_COMMAND` | `claude` |
| `REFINERY_AGENT_PERMISSION_MODE` | `auto` |
| `REFINERY_AGENT_MODEL` | none |
| `REFINERY_AGENT_MAX_BUDGET_USD` | none |

`--strict-mcp-config` keeps the user's own `refinery` entry, which may hold a
stale token, out of the session.

**The prompt** is fixed:

> You are the Refinery agent for run `<run-id>`. Call the `get_task` tool of
> the `refinery` MCP server and follow its instructions. When `get_task` says
> it is not the agent's turn, stop.

**Why `auto`.** In `auto`, a classifier reviews each action, as in the
interactive sessions so far. Enforcement stays in factoryd and git (vision,
goal 5), and the permission mode is a seatbelt, not the gate.
`bypassPermissions` is available through the variable.

### D8. Human side

- **`Review.show`** gains `job`: `{id, state: waiting|running|stuck|none,
  runner, runner_last_seen, attempts, log}`.
- **`Review.retry(run)`:** appends `Retry` if the run is stuck, else 409.
- **`Review.queue()`** includes stuck runs, waiting since their last
  `Finish`.
- **`refine start <change> [--root .]`** prints the run id, phase and
  worktree.
- **`refine retry <run>`** is new.
- **`refine status`** prints the job line.

### D9. Model (`spec/Runner.tla`)

**Abstraction.** Runs are coarsened from `Run.tla` to whose turn it is:
`agent` (`spec`, `model`, `tests`, `impl`), `human` (every phase where the
agent waits) or `ended`. The run's `seq` becomes the boolean `moved` ("the
run moved since its job was claimed"), and the attempts become a counter
that resets on any move. That matches D4, and keeps the state space finite
without a step bound, so liveness is checked exactly.

**State:**
- factoryd: `turn`, `moved`, `job` (the holder or none), `tok`, `failures`;
- the machine: `procs[r][x]`, the agent processes of runner x for run r.
  They survive a runner crash;
- each runner: `rstate` (`down`, `starting`, `reaped`, `ready`), and `lj`,
  its memory of each job (`claimed`, `running`, `exited`), lost at a crash.

**Actions:**
- runner: `Start`, `Reap`, `Hello`, `Claim` (`ClaimRun`, and `ClaimNone`,
  which only reconciles), `Launch`, `Stop` (poll said stop: kill), `Finish`
  and `Shutdown` (release best effort);
- environment: `Crash`, a lost claim response, and `AgentExit` (also for
  orphans);
- agent: `Progress`, which needs a live token and its process; the run may
  stay in the agent's turn (spec → model) or leave it;
- human: `Review`, `End` (cancel or archive) and `Retry`.

`MaxFaults` bounds crashes, shutdowns and lost responses together: a runner
stopped for ever can't make progress.

**Invariants:**
- `TypeOK`;
- `OneAgentPerRun`;
- `AgentsOnlyForOpenJobs`: an agent process exists only for its own runner's
  open job;
- `TokenOnlyForOpenJob`;
- `StuckRunsHaveNoJob`;
- `NoOrphansOnceReady`.

**Liveness:** `CancelledRunsLoseTheirAgent` and
`AgentTurnsGetAnAgentOrStuck`. They hold under weak fairness of the runner's
own steps, per run for claims, since claims pick the oldest waiting run.
That's justified in the module.

| Module and config | Scope | Distinct states |
|---|---|---|
| `Runner.cfg` | 1 run, 2 runners, 1 fault | 2,001 |
| `RunnerRuns.cfg` (`EXTENDS Runner`) | 2 runs, 2 runners, 1 fault | 49,411 |
| `Runner_big.cfg` | 1 run, 2 runners, 3 faults, 3 attempts | 10,699 |

**Reachability witnesses** (`Runner_reach.cfg`, `RunnerRuns_reach.cfg`), each
violated as required:
- `NeverStuck`, `NeverRetried`, `NeverKilledByCancel`;
- `NeverReclaimedAfterReap`, `NeverLostClaimReconciled`;
- `NeverSameJobAcrossPhases` (one job carries the agent from `spec` into
  `model`);
- `NeverTwoAgentsAtOnce` (two runs).

**What TLC found while writing it:**
1. **A lost claim response leaves the job open for ever.** The first model
   had no reconciliation. A planted model without it deadlocks: a cancelled
   run keeps an open job that nothing closes. This was a design gap, fixed by
   `holding` in the claim (D5) and the agent-jobs requirement "Claims
   reconcile the runner's jobs".
2. **Unbounded shutdowns violate the agent-turn property.** The runner
   restarts and stops for ever without claiming. This was a model bug, fixed
   by counting shutdowns as faults.
3. **Starvation.** With claim fairness only per runner, one run kept being
   picked and the other never was. This was a model bug, fixed by fairness
   per run, which is what oldest-first (D5) provides.

**Planted bugs checked:**

| Bug | Caught by |
|---|---|
| hello before reap | `AgentsOnlyForOpenJobs` |
| leases: a down runner's job expires | `AgentsOnlyForOpenJobs` |
| claim while stuck | `StuckRunsHaveNoJob` |
| finish keeps the token | `TokenOnlyForOpenJob` |
| no claim reconciliation | deadlock |

### D10. Bootstrap

`runner.1`, which writes this change, is driven by hand as before. The first
run launched by a runner is the next change, the web UI. When this merges,
the workflow memory and `AGENTS.md` (Bootstrap note) change: start with
`refine start`, then keep `refinery runner` running.

## Risks / Trade-offs

- **[Headless agents act unattended on your machine]** → Mitigations:
  - the default `auto` permission mode;
  - the agent's working directory is the run's worktree;
  - tokens are scoped to the run;
  - every review gate is still yours.

  `bypassPermissions` is opt-in.
- **[A runner that never returns leaves its runs "running"]** → `refine
  status` shows the runner's last-seen time, and `refine cancel` still works.
  This is deliberate (D4).
- **[A fresh agent per turn re-reads context]** → `get_task` carries the
  phase, the last decision and its note, the gate results and progress. The
  OpenSpec change and the repo carry the rest.
- **[`claude` CLI flags change between versions]** → They're built in one
  adapter function and covered by a test against a fake `claude` that
  records its argv. `REFINERY_AGENT_COMMAND` can point at a wrapper.
- **[The token sits in a file while the job runs]** → The file has mode
  0600, is in the runner's private directory, and is deleted at job end and
  at shutdown. The token is revoked at `finish`, `release` and `hello`.

## Open Questions

- Should the human be able to force-release a dead runner's job (for
  example `refine release <run>`, warning that an orphan may still run)? Left
  out: `cancel` and a new run cover it for one machine.
