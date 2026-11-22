# Design: run-lifecycle

## Context

Greenfield: `src/refinery/` holds only a stub, and `spec/` is empty. See
proposal.md for why this comes first. The requirements are in
`specs/run-lifecycle/spec.md` and `specs/run-record/spec.md`, and the model
is in `spec/Run.tla`. This document covers how the code realises them.

Constraints from AGENTS.md:
- One function per transition, named after the TLA+ action.
- Event names equal action names.
- Files and git are the truth; the database is a cache.
- Gates fail closed.
- Every subprocess call goes through one wrapper, which doesn't exist yet
  (`gate-runner`).
- Single machine for the MVP.

## Goals / Non-Goals

**Goals:**
- A pure, fully tested transition core that mirrors `spec/Run.tla`
  one-to-one.
- A record/log protocol whose crash behaviour is the one TLC checked.
- Archive gated on trace validation behind an interface, so the TLC-backed
  validator plugs in later without changing this code.

**Non-Goals:**
- **Committing and pushing the run record** (`review-queue`: factoryd commits
  at review gates and archive). This change only writes the file in the
  worktree.
- **Checking that frozen files are unchanged** (`gate-runner`'s verify gate).
  This change only records what is frozen.
- **Mapping tokens to roles** (`mcp-workflow`). Here the caller states the
  role.
- **Agent processes, worktrees and branches** (`runner`, `Runner.tla`).
- **HTTP, MCP and CLI surfaces.**
- **Detecting the GitHub merge.** `archived` is terminal; merging happens
  outside the run.

## Decisions

### D1. Phases: three human gates and an explicit `escalated` phase

The phase list is the AGENTS.md workflow with its three human approvals:
spec+model review, tests review, and "after human approval" before archive.
Failure triage ("spec bug / test bug → back to the human") is a phase of its
own, `escalated`, rather than a flag. That way the human's decision is a
logged transition (`ResolveTo*`), and the TLA+ property "only a Resolve
unfreezes" can be stated.

- **Rejected: two gates, archive without approval.** It contradicts AGENTS.md
  phase 7.
- **Rejected: escalation as a rejection note.** It loses the classification
  and can't express unfreezing.

### D2. Functional core, I/O shell

Each TLA+ action is one pure function in `refinery.run.transitions` (for
example `approve_spec`). It takes the current `RunRecord` (a frozen
dataclass) and a request carrying role and note, and returns either
`Accepted(record, event)` or `Refused(reason)`. A single transition table
(name → from-phases, to-phase, role, freeze effect) drives the shared phase
and role checks. The per-action functions add their own guards: a non-empty
note on rejections, a reason on `SkipModel`, a classification on `Escalate`,
and `tests ∈ frozen` on `ResolveToImpl`.

The I/O shell (`refinery.run.store`) applies an accepted result with the
durability protocol (D5).

- **Rejected: methods on a mutable `Run` class.** Harder to property-test,
  and mixes I/O with rules.

### D3. Run ids and record path

The run id is `<change>.<n>`, where `n` is 1 plus the number of runs ever
started on that change (from the index), for example `run-lifecycle.2`. The
record lives at `.refinery/runs/<run-id>.json` (decision recorded in the
proposal). It is written as UTF-8 JSON with sorted keys and a trailing
newline, so diffs stay stable and reviewable.

Fields: `schema` (1), `run_id`, `change`, `phase`, `frozen` (sorted list),
`seq`, `trace` (`{verdict, events, sha256}`, where `events` and `sha256` are
null until archived), `started_at`, `updated_at`.

- **Rejected: ULIDs.** Unreadable in branch names and CLI output, and nothing
  here needs global uniqueness.

### D4. Event log location and sealing

The log lives at `$REFINERY_DATA_DIR/runs/<run-id>/events.jsonl`, with the
data directory defaulting to `$XDG_DATA_HOME/refinery`, that is
`~/.local/share/refinery`. It sits outside every repository from the start,
so it can't be committed by accident and survives worktree deletion. Each
line is one event, as compact JSON with sorted keys.

Sealing at archive means three things: the sha256 of the exact file bytes and
the line count go into the run record, the file is chmod 0444, and the
transition layer refuses further appends to an archived run. Verifying a log
recomputes both values.

- **Rejected: a gitignored log in the worktree** (the vision's direction).
  The worktree is deleted after merge, taking the log with it.
- **Rejected: committing the log.** Your decision: noise in every PR.

### D5. Durability protocol (what `spec/Run.tla` models)

Each transition takes three steps, under a per-run `asyncio.Lock`:

1. Append the event line, then `flush` and `os.fsync`. If this fails, refuse
   the transition (fail closed; see the run-record spec).
2. Write the new record to a temporary file in the same directory, `fsync`
   it, `os.replace` it over the record, and `fsync` the directory.
3. Update the SQLite index (WAL) in one transaction. The index is a cache, so
   a crash here is repaired at startup.

Startup recovery, per run: truncate the log to the record's `seq` lines
(`Recover` in the model), then rebuild that run's index rows from the files.

`StartRun` holds a single start lock across all three steps. This is the
implementation of the model's `Claims` guard, which covers a start that is
still in flight; the `RunPair` mutation check shows the guard is needed.

- **Rejected: record first, then event.** A crash would leave a committed
  transition with no event, and the log can't be repaired from the record.
- **Rejected: the index as primary store.** That contradicts principle 1.

### D6. Index schema and rebuild

- `runs(run_id PK, change, phase, frozen JSON, seq, trace JSON,
  record_path, updated_at)`, with a partial unique index on `change` where
  `phase NOT IN ('archived','cancelled')`. This gives defence in depth for the
  one-active-run rule.
- `events(run_id, seq, name, from_phase, to_phase, actor, at, payload JSON,
  PRIMARY KEY (run_id, seq))`.

Rebuild takes the repository roots to scan (`.refinery/runs/*.json` in each
working tree, which covers main and active worktrees) and the data directory.
When two copies of a record exist (on main and in a worktree), the one with
the higher `seq` wins. A record whose log is missing is restored and flagged
`events_missing`.

### D7. Trace validation behind an interface

`TraceValidator` is a protocol: `validate(events) -> Verdict`, where a verdict
is pass, or fail with a reason. `Archive` calls it with the complete log,
including its own pending event. Any exception or timeout from the validator
becomes `fail`.

This change ships three pieces:
- **`ReplayValidator`:** walks the events through the transition table and
  checks that from/to chain from `none`. It is used in tests, and is cheap,
  but it isn't independent of the code it checks.
- **`export_trace(run_id, events) -> TraceModule(name, tla, cfg)`:** a
  generator for a `Trace_<run>.tla` module. It extends `Run` with
  `Runs = {r1}` and the trace as a sequence constant. Its `TraceNext` performs,
  for event i, the named action followed by `CommitRecord`, plus `CheckTrace`
  with verdict pass for `Archive`.
  - **Withdrawn attempts.** A failed `Archive` attempt is withdrawn from the
    log, so the module replays an unlogged attempt only right before a logged
    `RetryArchive`. That attempt's `CheckTrace` is forced to fail.
  - **Deterministic replay.** Every choice in `TraceNext` is forced, so TLC's
    verdict doesn't depend on search order.
  - **Acceptance:** every event is consumed, nothing is in flight, the
    model's log equals the trace, and the final phase is the last event's
    `to`. TLC 1.7.4 has no `POSTCONDITION`, so this is written as an inverted
    invariant (`TraceIncomplete`) that must be violated, the same pattern as
    the reachability witnesses. A rejected trace deadlocks instead.
- **Golden-file tests for the generator** (`tests/run/golden/`). In the tests
  the text is compared, not executed. It was checked with TLC when frozen:
  - accepted: happy path, a detour with rejections, an escalation and a
    failed gate, and an archive retry;
  - accepted, and not frozen: a cancelled run;
  - rejected (deadlock): a log that skips the tests review gate.

The TLC-backed validator, which runs the export through the subprocess
wrapper, lands in `gate-runner`. Nothing in the MVP archives a run before
`review-queue`, so nothing waits on it.

### D8. Model layout and abstractions

`spec/Run.tla` models:
- one run's phases and the two-step write;
- crashes between the two writes;
- the trace verdict as nondeterministic, since the model can't see the
  implementation it checks.

It does not model:
- the SQLite index (a cache);
- git;
- agent processes;
- note contents;
- crashes with nothing in flight. These are equivalent to stuttering, and
  allowing them everywhere would make the deadlock check vacuous.

Terminal states stutter through `Done`. Configurations:

| Config | Scope | Size |
|---|---|---|
| `Run.cfg` | 1 run, up to 16 events | ~74k states, ~2s |
| `RunPair.cfg` (`RunPair.tla` extends `Run`) | 2 runs on 1 change, up to 8 events | ~38k states, ~2s |
| `Run_big.cfg` (`make tla-big`) | 2 runs, up to 14 events | ~89M states, ~7 min |

`Run_reach.cfg` and `RunPair_reach.cfg` hold the reachability witnesses
(`Never*`), each of which must be violated.

## Tooling prerequisite

The current `make tla` must change in two ways, in a separate tooling commit
that lands before implementation:

1. **Run SANY and TLC from inside `spec/`.** Both resolve `EXTENDS` against
   the working directory, so today `make tla` fails on `RunPair.tla`.
2. **Check `<M>_reach.cfg` witnesses.** Run TLC once per listed invariant,
   and require each one to be violated. A single `-continue` run reports only
   the first violated invariant per state, so witnesses mask each other.

## Risks / Trade-offs

- **[ReplayValidator checks the code with the code's own table]** → It is
  only used in tests until `gate-runner` adds the TLC validator. The archive
  path never defaults to it.
- **[Losing the data directory loses event history]** → The run record keeps
  the verdict, count and sha256, so the evidence that validation passed
  survives. Backups are out of scope for a single operator.
- **[factoryd writes into worktrees directly]** → This is fine on one
  machine. The `runner` change revisits it for remote runners.
- **[State space grows fast with two runs]** → The two-run deep check is only
  in `Run_big.cfg`; the gate uses the split configs.
- **[fsync on every transition]** → Transitions happen at human and agent
  pace, a handful per minute, so the cost is negligible.

## Open Questions

- Approval events will probably carry the reviewed commit SHA, so the verify
  gate can diff frozen paths against it. That is an additive event field,
  decided in `review-queue`.
