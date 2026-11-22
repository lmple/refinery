# Proposal: mcp-workflow

## Why

Runs, gates and records exist only as a library. Nothing lets an agent see
its task or move its run, and nothing stops it from doing a human's part.
This change puts factoryd on the network: an MCP endpoint whose tools are the
only way agents act, with enforcement in the server, not in prompts.

After it lands, every Refinery change goes through these tools (AGENTS.md,
Bootstrap note). Until the `runner` change exists, the agent is launched by
hand.

## What Changes

- **factoryd server:**
  - `refinery serve` runs FastAPI and uvicorn on loopback, with the MCP SDK's
    streamable HTTP endpoint at `/mcp`.
  - On startup it reaps registered process groups, recovers runs, then starts
    the gate of every run left in `verify`.
- **Tools** (the five bootstrap tools plus one):
  - `start_workflow` (human);
  - `get_task`, `submit_for_review`, `escalate`, `run_gate` and
    `report_progress` (agent).
- **Phase-aware submission.** `submit_for_review` completes whatever the
  current agent phase is, including `SkipModel` given a reason. From `impl`
  it enters `verify`, and factoryd starts the real gate.
- **Dry runs.** `run_gate` is a dry-run gate in `tests` or `impl`: the same
  steps, recorded separately, never a transition. It never overlaps the real
  gate.
- **Progress.** `report_progress` appends to a per-run `progress.jsonl` and
  fails open; it is never a transition.
- **Role-scoped tokens.**
  - A human token comes from `refinery token create --role human`.
  - An agent token is issued by `start_workflow`, bound to that run, and dead
    once the run ends.
  - Only hashes are stored. A call with no valid token is rejected with
    HTTP 401, and a token of the wrong role cannot call a tool.
- **TLA+:** `spec/Workflow.tla` models tool calls, agent tokens, automatic and
  restarted gates, and dry runs, with the first liveness property in
  Refinery: every run in `verify` is resolved, even across crashes. It comes
  with `Workflow.cfg`, `Workflow_big.cfg` and `Workflow_reach.cfg`.

## Capabilities

### New Capabilities

- `agent-tools`: the MCP tools, what each does per phase and role, and their
  errors.
- `access-tokens`: human and agent tokens, how they are issued, stored,
  verified, bound to a run and revoked.
- `factoryd-server`: serving, configuration, and the startup and shutdown
  order.

### Modified Capabilities

- `verify-gate`: adds automatic gate start (on entering `verify` and after a
  restart), dry-run gates, and gate errors failing closed.

## Impact

- **New packages:**
  - `src/refinery/auth/` (tokens);
  - `src/refinery/factoryd/` (workflow service, MCP adapter, app, CLI);
  - `refinery.gate.runner`, which gains dry runs.
- **The `refinery` console script** (already in `pyproject.toml`) becomes
  the Typer admin CLI: `serve` and `token create|list|revoke`. The `refine`
  client CLI comes with `review-queue`.
- **No new dependencies:** FastAPI, uvicorn, Typer and the MCP SDK (2.3) are
  already in `pyproject.toml`; this change starts using them.
- **New runtime state:** `<data>/tokens/`, `<data>/runs/<id>/progress.jsonl`
  and `<data>/runs/<id>/dry-runs/`.
