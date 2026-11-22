# AGENTS.md — Refinery

Shared instructions for every coding agent working on this repo (Claude Code,
OpenCode, pi, Mistral Vibe, …). Agent-specific notes live in their own files
(e.g. `CLAUDE.md`), which import this one.

## What Refinery is

A spec-driven, formally verified software factory for AI-written code. A human
designs specifications; agents implement; formal methods and tests act as gates;
the human reviews and approves.

Components:

- **factoryd** — server (FastAPI + SQLite). Owns runs, stages, events, gates,
  review queue, scheduler, merge queue. Exposes:
  - HTTP/JSON API + SSE for UIs
  - MCP endpoint (streamable HTTP) for agents
- **runner** — process on each machine that pulls jobs from factoryd, creates
  git worktrees, launches agents (headless or in tmux).
- **CLI** (`refine`) — Typer client of the HTTP API.
- **Web UI** — server-rendered HTML + htmx, vendored JS only (Mermaid,
  Cytoscape.js, Observable Plot, CodeMirror). No npm build.
- **Emacs client** — separate repo/package later; consumes the same API.
- **Spec plugins** — TLA+ (TLC/Apalache/TLAPS), Lean, Quint; spec-framework
  plugins: OpenSpec (first), Spec Kit (later).
- **Agent adapters** — Claude Code, OpenCode, pi, Mistral Vibe.

Design principles:

1. Files and git are the source of truth. factoryd indexes and orchestrates; its
   database must be rebuildable from the repos.
2. Logging fails open (buffer to local JSONL); gates fail closed.
3. Enforcement lives in the server and git (role-scoped tokens, branch
   protection), not in agent prompts or hooks.
4. Minimal tools, minimal dependencies, boring technology.
5. Server renders visuals (SVG via Graphviz / mermaid-cli) once; all clients
   display them.

## Stack

- Python 3.12+, managed with `uv` (never pip directly).
- FastAPI, Uvicorn, Typer, official MCP Python SDK, stdlib `sqlite3` (WAL mode),
  htmx.
- Tests: pytest + hypothesis.
- Quality: `pyright` (strict), `ruff` (lint + format).
- Concurrency: `asyncio` with structured concurrency (`TaskGroup`). No threads
  unless justified in the change proposal.
- Formal specs of Refinery itself: **TLA+** (PlusCal allowed for algorithmic
  parts). Do NOT use Quint in this repository.

Adding a dependency requires a line of justification in the OpenSpec change
proposal.

## Repository layout

```
spec/              TLA+ specs (*.tla, *.cfg) — behaviour source of truth
openspec/specs/    current requirements (source of truth for intent)
openspec/changes/  in-flight changes (proposal, tasks, spec deltas)
docs/              vision.md, architecture notes, ADRs
src/refinery/      server, runner, cli, web, plugins, adapters
tests/             unit, property-based, trace-validation tests
vendor/            vendored JS for the web UI
```

## Workflow (every change)

Every feature or behaviour change follows these phases, in order. Never skip a
phase, never merge phases.

1. **spec** — Create an OpenSpec change in `openspec/changes/<name>/`
   (proposal.md, tasks.md, spec deltas). Every requirement uses SHALL/MUST and
   has at least one `#### Scenario:`.
2. **model** — If the change affects behaviour (runs, scheduler, locks, merge
   queue, gates, permissions), update or create the TLA+ spec in `spec/`.
   Requirements:
   - a `TypeOK` invariant
   - every safety property from the change written as an invariant
   - SANY parses it and TLC checks it with no violation
   - at least one reachability check showing the new behaviour is actually
     reachable (no vacuous invariants)
3. **review gate** — Stop. Summarize the spec delta and the TLA+ delta for the
   human. Wait for explicit approval.
4. **tests** — Write failing tests first (unit + hypothesis properties mirroring
   the TLA+ invariants; trace-validation tests where relevant). Show that they
   fail. Stop for approval; after approval, tests are frozen.
5. **impl** — Implement until all checks pass. Do not modify `spec/`,
   `openspec/specs/`, or frozen tests in this phase. If the spec or a test looks
   wrong, stop and report it; do not "fix" it.
6. **verify** — Run the full gate (see Commands). Report results.
7. **archive** — After human approval: archive the OpenSpec change, commit,
   push.

Failure triage: when a check fails, classify it as **spec bug**, **test bug**,
or **code bug** and say which. Only code bugs are fixed in the impl phase; the
others go back to the human.

## Formal methods rules (TLA+)

- Keep specs small and focused: one module per concern (`Run.tla`,
  `Scheduler.tla`, `Locks.tla`, `MergeQueue.tla`, …).
- Use small constants in `.cfg` files so TLC finishes in seconds; add a larger
  `*_big.cfg` for occasional deeper checks.
- Model the environment explicitly (agent crashes, timeouts, server restarts,
  concurrent runs).
- Prefer invariants and simple temporal properties; justify any fairness
  assumption in a comment.
- Event names in the implementation must match action names in the spec, so
  event logs can be validated against the spec (trace validation).
- Never weaken an invariant to make TLC pass. Report the counterexample instead.

## Code rules

- Type-annotate everything; pyright strict must pass.
- Small modules, small functions, explicit data types (dataclasses or Pydantic
  models at API boundaries).
- Every state transition in code goes through one function per transition, named
  after the TLA+ action.
- All subprocess calls (agents, TLC, git) go through a single wrapper with
  timeout, cancellation, and process-group kill.
- No secrets in code, config, logs, or tests. Tokens come from the environment.
- This is a personal project: never add employer-specific code, data, or
  references.

## Commands

```sh
uv run pytest -q                     # tests
uv run pyright                       # types (strict)
uv run ruff check && uv run ruff format --check
make tla                             # SANY + TLC on all spec/*.tla
make gate                            # everything above, in order
```

`make tla` runs, per spec:

```sh
java -cp tools/tla2tools.jar tla2sany.SANY spec/<M>.tla
java -jar tools/tla2tools.jar -workers auto -config spec/<M>.cfg spec/<M>.tla
```

(PlusCal: translate first with
`java -cp tools/tla2tools.jar pcal.trans spec/<M>.tla`.)

## Commits

- One task = one commit. Message format: `<area>: <imperative summary>` (e.g.
  `scheduler: add per-module lock`).
- Never commit with a failing gate.
- Never rewrite history on `main`.

## Bootstrap note

Refinery does not exist yet as a running factory. Until the factoryd MCP tools
(`start_workflow`, `get_task`, `run_gate`, `report_progress`,
`submit_for_review`) are implemented, follow the workflow above manually and use
the Commands section for gates. Once they exist, every change MUST go through
them.
