# Design: mcp-workflow

## Context

`RunStore`, `GateRunner`, `Supervisor` and `TlcTraceValidator` exist as a
library, and nothing serves them yet. `pyproject.toml` already depends on
FastAPI, uvicorn, Typer and `mcp` 2.3.

In SDK 2.x, `FastMCP` became `mcp.server.mcpserver.MCPServer`. It accepts a
`TokenVerifier` plus `AuthSettings`, and adds bearer-auth middleware to its
streamable HTTP app. Tool handlers read the verified token with
`mcp.server.auth.middleware.auth_context.get_access_token()`.

See proposal.md for motivation, the four spec deltas for the requirements,
and `spec/Workflow.tla` for the model.

## Goals / Non-Goals

**Goals:**
- **A plain service layer.** A `Workflow` service implements each tool as a
  plain async method taking a `Principal`, testable without HTTP. The MCP
  module only adapts it.
- **Enforcement in factoryd:**
  - authentication by middleware;
  - role and run binding in `Workflow`;
  - phase rules in `RunStore`/`transitions`, unchanged.
- **Crash-safe gates:** every run in `verify` gets its gate, including after
  a crash (Workflow.tla's liveness property).

**Non-Goals:**
- **Review decisions** through MCP or a CLI (approve, reject, cancel,
  resolve), `submit_for_review` waiting for a decision, and notes in
  `get_task` (`review-queue`).
- **The `refine` client CLI and the HTTP JSON API for UIs** (`review-queue`,
  then the web UI).
- **Launching agents** (`runner`). For now, the human starts the agent by
  hand in the run's worktree, with the agent token from `start_workflow`.
- **Archiving through a tool.** `ApproveFinal`, and the archive that follows,
  come with `review-queue`, which wires `TlcTraceValidator` into
  `RunStore.archive`.
- **TLS and non-loopback exposure** (a vision non-goal). Binding beyond
  loopback is possible, but documented as unsupported.

## Decisions

### D1. Layering

`refinery.factoryd`:
- `config.py`: `ServerConfig` (data directory, roots, host, port, jar, run
  spec), built from CLI options and environment variables.
- `workflow.py`: `Workflow(store, gates, tokens, config)`, the six tool
  operations, gate scheduling, startup and shutdown.
- `mcp.py`: builds the `MCPServer`. Each tool is a thin wrapper that resolves
  the caller's `Principal`, calls `Workflow`, and turns `WorkflowError` into
  a tool error.
- `app.py`: `create_app(config)`, the Starlette/FastAPI app with a lifespan
  that runs `Workflow.startup()` and `.shutdown()`, mounting the MCP
  streamable HTTP app at `/mcp`.
- `cli.py`: the Typer `refinery` command (`serve`, `token create|list|revoke`).
  The console script stays `refinery = "refinery:main"`, which now hands off
  to this CLI, so `pyproject.toml` is unchanged.
- `RunStore.runs()`: a small public addition listing every known run. It's
  used by startup to find runs in `verify`, and by tests.

Rejected alternative: tool logic inside the MCP handlers. It's untestable
without HTTP, and the `refine` CLI (`review-queue`) needs the same
operations.

### D2. Tokens

`refinery.auth.tokens`:
- **Format.** A token is `rft_<h|a>_<id>_<secret>`: `id` is 8 hex characters
  (the lookup key), and `secret` is 64 hex characters
  (`secrets.token_hex(32)`). Hex rather than URL-safe base64 keeps `_` a pure
  separator, so a token parses only one way, and the secret is always the
  whole last field.
- **Storage.** `<data>/tokens/<id>.json` holds `{id, role, run_id,
  sha256(secret), created_at, revoked_at}`, written atomically with mode
  0600.
- **Checking.** `TokenStore.verify(token)` parses the token, loads it by id,
  and compares hashes with `hmac.compare_digest`. A token that is unknown,
  malformed or revoked is rejected (`None`).
- **SDK bridge.** `RefineryTokenVerifier(TokenVerifier)` maps a valid token
  to `AccessToken(client_id=id, scopes=[role], claims={"run_id": …})`.
- **Run binding.** An agent token's run check (the run exists and isn't
  terminal) happens in the verifier, against `RunStore`. A token of an ended
  run is therefore rejected with 401, as the spec requires. No write is
  needed when a run ends: the check is against the run's phase.

`AuthSettings` needs an issuer and a resource URL. Both are factoryd's own
`http://<host>:<port>`, since factoryd issues its own tokens and there is no
OAuth server. `required_scopes` is unset; roles are checked per tool.

Rejected alternatives:
- **JWTs:** they need key management and revocation lists, for no benefit on
  one machine.
- **Static tokens from the environment:** you rejected those, because they
  aren't bound to a run.

### D3. Tool → transition mapping

`submit_for_review` uses a table keyed by phase:

| Phase | Transition |
|---|---|
| `spec` | `CompleteSpec` |
| `model` | `SkipModel` (reason given) or `CompleteModel` |
| `tests` | `CompleteTests` |
| `impl` | `CompleteImpl` |

Every other phase gets `WorkflowError("it is the <human|factoryd>'s turn
(<phase>)")`. Whose turn it is comes from the role of the transitions that
leave the phase (`RULES`), the same source `get_task` reports.

In `impl`:
1. `Workflow` first refuses if a dry run is in progress for the run
   (`GateAndDryRunExclusive`).
2. It applies `CompleteImpl`.
3. It schedules the gate run as a background task (`Submit` then `GateStart`
   in the model).

### D4. Gate scheduling and restart

`Workflow.schedule_gate(run_id)` creates a background task running
`GateRunner.run_gate`, kept in a map.
- **Internal errors.** If the task raises an exception that isn't a
  cancellation, the task records `FailGate` with `gate error: …`, as the
  spec's fail-closed rule requires. `GateRunner` gains a small
  `fail_closed(run_id, gate_id, detail)` helper for this, which writes
  `result.json` with the remaining steps marked failed.
- **Waiting for gates.** `wait_for_gates` drops finished tasks itself. When
  every task is already done, awaiting them doesn't yield to the event loop,
  so their done callbacks would never run and the wait would spin; a gate run
  that hits an internal error before its first await finishes exactly like
  that.
- **Startup.** `Workflow.startup()`:
  1. `Supervisor.reap()`;
  2. `RunStore.open(...)` (recovery and rebuild);
  3. `schedule_gate` for each run in `verify` (`Recover` in the model).
- **Shutdown.** `Workflow.shutdown()` cancels every gate task, which kills
  its step, then closes the store.

### D5. Dry runs

`GateRunner.dry_run(run_id)`:
- is allowed in `tests` and `impl`;
- writes to `dry-runs/d<n>/`;
- reuses the same step loop with a different allowed-phase set (watched
  every `poll_s`);
- never calls `store.transition`.

The runner keeps one task map for both kinds, so a dry run and a gate run for
the same run are mutually exclusive at the source. `Workflow.submit_for_review`
in `impl` checks `gates.running(run_id)` first.

`run_gate` waits for the dry run to finish (bounded by the step limits) and
returns the steps. MCP clients may time out on very long gates; that's
acceptable for the MVP, since `get_task` shows dry-run results too.

### D6. Progress

`refinery.factoryd.progress`:
- **Format.** Lines of `{at, run_id, message}` in
  `<data>/runs/<id>/progress.jsonl`, appended without fsync. This is
  telemetry, not the run record.
- **Fail open.** Any `OSError` is caught, and the tool returns
  `{"recorded": false, "warning": …}`, per AGENTS.md principle 2.
- **In `get_task`.** It reads the last 5 lines and skips unreadable ones.

### D7. `get_task` content

`get_task` returns a JSON object:

```
{run_id, change, phase, turn, worktree,
 instructions,
 frozen: [{artifact, commit, paths}],
 gate: {id, verdict, failed_steps} | null,
 dry_run: {id, verdict, failed_steps} | null,
 progress: [...]}
```

- **`instructions`** is a short fixed text per phase, condensed from
  AGENTS.md's workflow (for example, `spec`: write the OpenSpec change in
  `openspec/changes/<change>/`, then call `submit_for_review`).
- **`gate`** comes from the newest `gates/g<n>/result.json`, and **`dry_run`**
  from `dry-runs/d<n>/result.json`.

### D8. Testing approach

- **`Workflow`** is tested directly with `Principal`s, using the git
  worktree fixture and scripted make targets from `gate-runner`'s tests.
- **MCP end to end.** A few tests run real HTTP: uvicorn serves
  `create_app(config)` on an ephemeral port inside the test's event loop,
  and the SDK's streamable HTTP client connects with each token. They cover
  401 without a token, the tool list, a role refusal, and one full
  `start_workflow` → `get_task` → `submit_for_review` round trip.
- **CLI** tests use Typer's `CliRunner`.

### D9. Model (`spec/Workflow.tla`)

**Phases.** These are coarsened:
- `spec` stands for the agent phases before the tests are approved, and
  `spec_review` for their review gates;
- `impl`, `verify` and `final_review` are kept;
- `archived` and `cancelled` are the terminal phases.

**Variables:** the agent token per run, the real gate (idle, pending or
running), the dry run (idle or running), `up`, and a crash counter (at most
`MaxCrashes`). The state space is finite without a step bound, so liveness is
checked exactly.

**Properties:**
- invariants: `TokenOnlyForStartedRuns`, `GateAndDryRunExclusive`,
  `DryRunOnlyInImpl`, `GateOnlyInVerify`;
- action property: `AgentNeedsLiveToken`;
- liveness: `VerifyResolves`, under weak fairness of factoryd's own actions
  (`GateStart`, `GateFinish`, `GateAbort`, `Recover`), justified in the
  module. Human and agent actions stay unconstrained.

| Config | Scope | Size |
|---|---|---|
| `Workflow.cfg` | 2 runs, 1 crash | 368 states |
| `Workflow_big.cfg` | 3 runs, 2 crashes | 7,311 states |

Reachability witnesses: archived, gate rescheduled after a crash, dry run
killed by a cancel, and two runs in `verify`.

Planted bugs checked when the model was written:

| Bug | Caught by |
|---|---|
| recovery that doesn't reschedule | `VerifyResolves` |
| submit during a dry run | `GateAndDryRunExclusive` |
| dry run without a live token | `AgentNeedsLiveToken` (after strengthening it) |

## Risks / Trade-offs

- **[`run_gate` may outlast an MCP client's tool timeout]** → The agent can
  read the result later through `get_task`. Streaming progress is a later
  improvement.
- **[Long-lived human token in a file the user copies around]** → It can be
  revoked, `token list` shows it, it's bound to loopback, and there's a
  single operator.
- **[SDK 2.x API churn]** → All SDK use is confined to `factoryd/mcp.py` and
  `app.py`.
- **[The worktree must equal a configured root]** → This is restrictive, but
  enough for one machine with the agent launched by hand. `runner` revisits
  worktree layout.
- **[Workflow.tla coarsens phases]** → It checks tokens, scheduling and the
  dry-run/gate interplay. The phase rules themselves stay with `Run.tla`.

## Open Questions

- The default port, 8740, is arbitrary; it's an option either way.
