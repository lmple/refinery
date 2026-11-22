# Design: gate-runner

## Context

`run-lifecycle` is in place: `RunStore`, transitions and the run record. It
left two interfaces open: `TraceValidator` (implemented only by a replay
validator used in tests) and the verify phase's `PassGate`/`FailGate`, which
nothing records yet. No code starts subprocesses yet. See proposal.md for
motivation; the requirements are in the four spec deltas, and the model is
`spec/Gate.tla`.

## Goals / Non-Goals

**Goals:**
- One small supervisor that every later subprocess (TLC, git, gate steps,
  agents) uses, whose cleanup guarantee holds across a factoryd crash.
- A gate that cannot pass by accident: fail closed, every step, frozen paths
  enforced.
- Archive validated by TLC against `spec/Run.tla`, not by the code's own
  table.

**Non-Goals:**
- **Exposing the gate** through MCP (`run_gate`) or the CLI
  (`mcp-workflow`, `review-queue`). This change is a library.
- **Launching agents and tmux** (`runner`).
- **Sandboxing gate commands.** Gate steps run agent-written code with
  factoryd's privileges (see Risks).
- **Running steps in parallel.** Steps run sequentially; the model's
  `OneStepAtATime` invariant records that choice.
- **Containing processes that leave their process group** (`setsid`,
  daemonising). Cgroups would be needed for that (see Risks).

## Decisions

### D1. Supervisor: one async call, process group, kill escalation

`refinery.proc.Supervisor(data_dir)` exposes `async run(argv, *, cwd, env,
timeout_s, output) -> CallResult`. Here `output` is `Capture()` or
`StreamTo(path)`. `CallResult` holds `outcome` (`exited`, `signalled`,
`timed_out`, `cancelled` or `failed_to_start`), plus `exit_code`, `signal`,
`duration_s`, and `stdout`/`stderr` when captured.

- **Process group.** Commands are started with `start_new_session=True`,
  which gives each one its own session and process group.
- **Streaming.** A streamed call passes the log file's descriptor as the
  child's stdout and stderr, with no pipe or reader task. Output survives a
  factoryd crash, and nothing can deadlock on a full pipe. Capture uses
  pipes read by `communicate()`.
- **Killing.** Kill means `killpg(SIGTERM)`, then a grace period (default
  2 s), then `killpg(SIGKILL)`. After the leader exits, for any outcome, the
  supervisor sends `killpg(SIGKILL)` and polls `killpg(pgid, 0)` until
  `ESRCH`, so nothing the command started survives the call.
- **Cancellation.** It is handled with `try/except CancelledError`: kill,
  wait, then re-raise.

Rejected alternatives:
- **`subprocess.run(..., timeout=)`:** it kills only the leader, not the
  group, and blocks the event loop.
- **`prctl(PR_SET_PDEATHSIG)` through ctypes:** it covers the direct child
  only, not grandchildren, and adds a ctypes surface.

### D2. Registry and start latch

The registry lives at `<data>/procs/<pgid>.json` and holds `{pgid,
start_ticks, argv, started_at}`. It is written atomically (temp file, fsync,
rename, directory fsync), like the run record.

`start_ticks` is field 22 of `/proc/<pid>/stat`: the leader's start time, in
clock ticks since boot.

The command runs behind a latch:

```sh
/bin/sh -c 'read -r _ || exit 125; exec "$@"' refinery-latch <argv...>
```

1. The supervisor spawns the latch with a pipe on stdin.
2. It writes the registry entry.
3. It writes one line to the pipe, so `read` returns and the latch `exec`s
   the real command in the same process, keeping the same pid and process
   group.

If factoryd dies before step 3, the pipe closes, `read` hits EOF and the latch
exits with 125 without ever running the command. Nobody observes that exit:
its supervisor is gone.

**Failure to start.** It is decided before spawning: `argv[0]` is resolved
with `shutil.which`, or checked with `os.access(X_OK)` for a path. If that
fails, the call returns `failed_to_start` and nothing is spawned. Every exit
code the latch reports therefore belongs to the real command, including
126/127 if the program disappears between the check and the `exec`. That
outcome still fails a gate step.

This is `Spawn → Register → Release` in `Gate.tla`. The mutation "release
before register" violates `RunningIsRegistered`.

Rejected alternatives:
- **Register after starting the command:** this leaves a crash window in
  which a running command has no entry, and the model shows the orphan.
- **Pid files written by the child:** the child can't know when its entry is
  durable.

### D3. Reaping

`Supervisor.reap()` runs at startup, before any gate. For each entry:

| Leader process | Group | Action |
|---|---|---|
| alive, start time matches | — | `killpg(SIGKILL)`, then wait |
| alive, start time differs (pid reused) | — | spare it |
| gone | still present (`killpg(pgid, 0)` succeeds) | kill the group |

A pgid can't be reused while its group exists, so when the leader is gone
but the group is present, the group is still the registered one. Then remove
the entry. This is `Recover` in the model.

### D4. Gate runner

`refinery.gate.GateRunner(store, supervisor, config)` exposes:
- `async run_gate(run_id) -> GateReport | Refused`
- `cancel(run_id)`

`GateConfig` holds per-step time limits, the make program, `TLA_JAR` and the
environment. The default limits are:

| Step | Limit |
|---|---|
| `frozen` | 60 s |
| `test` | 600 s |
| `types` | 300 s |
| `lint` | 120 s |
| `tla` | 1800 s |

How a gate run works:
- **Start.** It's refused unless the run is in `verify` and no gate task is
  registered for it; a per-run lock and a task map enforce this. Gate ids come
  from the gate directories that already exist (`g<max+1>`), so a crashed gate
  run's directory is never reused.
- **Steps.** Each step runs `make <step> TLA_JAR=<configured jar>` in the
  worktree, streamed to `<step>.log`. The jar path is passed so worktrees
  never download it.
- **Watching the run.** Before each step, and every `poll_s` (0.2 s) while
  a step runs, the runner re-reads the run. If the run has left `verify`, the
  runner cancels the running step, so the supervisor kills it. The step is
  reported as `cancelled`, and the gate run stops without a result (the
  model's `Abort`/`Finish` discard). This works without any `cancel()` call,
  as the verify-gate spec requires.
- **Result.** `result.json` is written atomically, then `store.transition`
  records `PassGate` or `FailGate`, with `gate=<id>` and, on failure,
  `reason="failed: <steps>"`. If that transition is refused because the run
  left `verify` in the meantime, the result stays on disk and the report says
  so.
- **Cancellation.** `cancel(run_id)` cancels the gate task: the supervisor's
  cancellation kills the running step, and `CancelledError` propagates to the
  caller. `mcp-workflow` will call it on `Cancel`, which saves waiting for the
  next poll.
- **Environment.** `check_frozen` takes the same explicit `env` as the make
  steps (`GateConfig.env`).
- **Worktree.** `RunStore.worktree(run_id)` is new. It derives the worktree
  from the indexed record path: `<worktree>/.refinery/runs/<id>.json`.

### D5. Frozen check

The `frozen` step is not a make target. The runner calls git through the
supervisor, with captured output, once per frozen artifact `a` with approved
commit `c`:
- `git diff --quiet <c> -- <paths(a)>`: exit 0 means no difference and 1 means
  a difference. This covers committed and uncommitted changes to tracked
  files.
- `git ls-files --others --exclude-standard -- <paths(a)>`: any output means
  new untracked files.

Any other git outcome fails the step (fail closed). The step's log lists each
check's result.

`<paths(a)>` is defined in the verify-gate spec. The tests artifact is
`tests/` as a whole, the simplest rule that can't miss a file.

### D6. Reviewed commits

The model changes:
- `TransitionRequest.commit`, `Event.commit`;
- `RunRecord.spec_commit` and `RunRecord.tests_commit`, each `str | None`.
  These are explicit fields; a mapping in a frozen dataclass would be
  mutable.

Where commits are checked and kept:
- `approve_spec` and `approve_tests` refuse unless `commit` matches
  `^[0-9a-f]{40}$`.
- Freezing sets the commit, and the `Resolve*` transitions clear the commits
  of the artifacts they unfreeze, so the commit fields always match the frozen
  set.
- The record writes `"approved": {"spec": …, "tests": …}`. The reader treats a
  missing `approved` as both null, which keeps the format additive under
  schema 1.

`Run.tla` is unchanged: it abstracts payloads, and the gate decides on frozen
paths, which `Gate.tla` models as a step that passes or fails.

### D7. TLC trace validator

`refinery.run.tlc.TlcTraceValidator(supervisor, *, java, jar, run_spec,
work_dir, timeout_s)`:
1. Creates a fresh directory under `work_dir`.
2. Copies `run_spec` (the `Run.tla` factoryd implements, configured
   explicitly) and the exported `Trace_<run>.tla`/`.cfg` into it.
3. Runs TLC with capture and `cwd` set to that directory, so `EXTENDS Run`
   resolves (the same rule as `scripts/tla.sh`).
4. Maps the result:

   | TLC result | Verdict |
   |---|---|
   | `exited` 12 with `Invariant TraceIncomplete is violated` | pass |
   | `exited` 11, deadlock | fail: "not a behaviour of Run" |
   | anything else | fail, with the outcome and a tail of the output |

5. Removes the directory.

`RunStore.archive` already wraps the validator in `asyncio.timeout`. The
cancellation that causes reaches the supervisor, which kills TLC.

### D8. Model scope

`spec/Gate.tla` models:
- one run's slice of `Run.tla` (verify, impl, final_review, cancelled);
- sequential gate steps, each with one process group (`none`, `parked`,
  `running`, `dead`);
- the registry, start latch, timeouts, failure to start, cancellation and
  factoryd crashes.

These are abstracted away:
- TERM/KILL escalation (one atomic kill);
- children left behind (folded into the group being alive);
- PID-reuse identity checks;
- the contents of gate logs;
- other runs, which have separate worktrees and groups.

| Config | Scope | Size |
|---|---|---|
| `Gate.cfg` | 2 steps, up to 3 gate runs | 946 states, under 1 s |
| `Gate_big.cfg` | 3 steps, up to 4 gate runs | 9,741 states |
| `Gate_reach.cfg` | 5 witnesses | — |

The witnesses are: passed, failed then passed, cancelled mid-step, orphan
after a crash, and a latch stranded by a crash.

Planted bugs checked when the model was written:

| Bug | Caught by |
|---|---|
| release before register | `RunningIsRegistered` |
| recovery that doesn't kill | `RunningIsRegistered` |
| pass if any step passed | `PassOnlyIfAllStepsPassed` |
| abort without killing | `OnlyCurrentStepRuns` |

## Risks / Trade-offs

- **[Gate steps run agent-written code with factoryd's privileges]** →
  Single-operator, own machines (vision non-goals). Sandboxing is a later
  change, and the risk is noted in this change's spec review.
- **[A command that calls `setsid` escapes its group, and so the
  supervisor]** → Our gate commands don't. Agents (`runner`) may need cgroups
  or systemd scopes, a decision for that change.
- **[Linux-only `/proc` start time]** → The MVP targets Linux, and CI is
  Ubuntu. Another platform would fail the reaping identity check closed: it
  would spare the group and log it.
- **[Approvals now need a commit, which changes `run-lifecycle` tests]** →
  The edits are listed in the proposal and limited to request defaults and
  the property oracle. No test logic is weakened.
- **[Sequential steps make a gate run slow (about the sum of the steps)]** →
  Acceptable at MVP scale; parallel steps would need a model change.

## Open Questions

- The default step time limits are guesses; tune them after real runs.
  Changing them doesn't change the specs.
