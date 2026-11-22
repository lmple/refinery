# Proposal: run-lifecycle

## Why

Every later piece of Refinery (gates, MCP tools, runner, review queue) acts on
a run, and none of them can be built or trusted until a run's states, its
legal transitions and its durable record are pinned down. This is also the
first behaviour of Refinery itself to get a TLA+ model, as the base the
other changes build on (docs/vision.md, "First OpenSpec changes").

## What Changes

- A run state machine that follows the AGENTS.md workflow: spec → model →
  spec review → tests → tests review → impl → verify → final review →
  archive. It adds failure escalation to the human, cancellation, and the
  terminal states `archived` and `cancelled`.
- One transition function per TLA+ action, each attributed to a role
  (human, agent, factoryd). Out-of-order transitions and transitions requested
  by the wrong role are refused.
- Freezing: approving the spec review freezes the spec, and approving the
  tests review freezes the tests. Only a human resolving an escalation
  unfreezes them.
- **Run-record decision** (final; supersedes the direction in
  docs/vision.md):
  - `.refinery/runs/<run-id>.json` is committed on the run's branch, one file
    per run, so runs never conflict and main keeps every finished run.
  - The run's `events.jsonl` lives in factoryd's data directory, outside git,
    and is sealed there at archive.
  - At archive, the run record stores the trace-validation verdict, the log's
    sha256 and its event count.
- Durability protocol: append and fsync the event, then atomically rewrite
  the run record. Recovery drops events the record has not committed. A
  failed event append refuses the transition (fail closed).
- A SQLite (WAL) index of runs and events, rebuildable from run records and
  event logs.
- Trace validation:
  - **Here:** a generator that turns a run's event log into a TLA+ trace
    module for `spec/Run.tla`, the validator interface that archiving
    depends on, and a Python replay validator for tests.
  - **In `gate-runner`:** the TLC-backed validator, once the subprocess
    wrapper exists.
- `spec/Run.tla` with `Run.cfg`, a two-run `RunPair` model, a deep
  `Run_big.cfg`, and reachability witnesses.

## Capabilities

### New Capabilities

- `run-lifecycle`: phases of a run, who may trigger which transition, review
  gates, freezing, escalation, cancellation and terminal states.
- `run-record`: the committed run record, the event log, the durability and
  recovery protocol, archive sealing with trace validation, and rebuilding the
  index.

### Modified Capabilities

None (no existing specs).

## Impact

- New package `src/refinery/run/` (domain types, transitions, record and event
  log I/O, index, rebuild, trace export). No HTTP, MCP or CLI surface yet;
  those arrive in `mcp-workflow` and `review-queue`.
- New TLA+ specs: `spec/Run.tla`, `spec/RunPair.tla` and their configs.
- New runtime state: factoryd data directory (`REFINERY_DATA_DIR`) for event
  logs and the SQLite index.
- No new dependencies (stdlib `sqlite3`, `json`, `hashlib`, `os`;
  `hypothesis` is already a dev dependency).
- `docs/vision.md`: run-record paragraph updated to the final decision.
- Depends on a separate tooling fix: `make tla` must run from `spec/` so
  `RunPair.tla` can `EXTENDS Run`, and it should check `*_reach.cfg`
  witnesses (see design.md, "Tooling prerequisite").
