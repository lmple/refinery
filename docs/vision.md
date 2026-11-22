# Refinery — Vision

Status: draft, 2026-11-22. How the work is done (workflow, rules, commands)
lives in [`AGENTS.md`](../AGENTS.md); this document says *why* Refinery exists
and *what* comes first.

## Problem

AI agents now write most of the code, and four things break as a result:

1. **Agent code can't be trusted on its face.** Output looks plausible and is
   subtly wrong, and tests written by the same agent in the same session share
   its blind spots.
2. **Review is the bottleneck.** Agents produce changes faster than one person
   can read raw diffs, and a diff doesn't say what the change was *meant* to do.
3. **Agent sessions are unmanaged.** Runs are scattered across terminals,
   machines and harnesses, with no shared record of what ran, what state it is
   in, or why it failed.
4. **Process lives in prompts and drifts.** Workflow rules written in prompts
   or hooks get skipped, reinterpreted or "fixed" by the agent they constrain.

## Goals

1. **Intent first.** Every change starts as an OpenSpec change, and, where it
   touches behaviour, a TLA+ model checked by TLC, before any test or code
   exists.
2. **Independent, fail-closed gates.** Model checking, tests frozen before
   implementation, pyright strict and ruff decide whether a change advances,
   not the agent that wrote it. A gate that crashes, times out or can't run
   counts as failed.
3. **Review the intent, not just the diff.** The human reads the spec delta and
   the TLA+ delta first, then the code diff, at fixed review gates.
4. **One place to see every run.** Phase, events and gate results for every
   run, whatever harness or machine it ran on.
5. **Enforcement in the server and git**, through role-scoped tokens and branch
   protection, not prompts.
6. **Files and git are the source of truth.** factoryd's database can be
   deleted and rebuilt from the repositories.
7. **Refinery builds Refinery.** Its own development goes through it as soon as
   the loop works.

## Non-goals

- **Teams or multiple users.** One human operator, with no accounts, orgs or
  permission model beyond role-scoped tokens for agents and runners.
- **Hosted or SaaS.** It runs only on my own machines and is not exposed
  publicly.
- **Autonomous merge.** No change reaches `main` without my approval, even when
  every gate passes.
- **Replacing CI.** Refinery orchestrates and gates agent work; GitHub Actions
  (`make gate`) stays the CI of record.
- **Being an agent.** Refinery doesn't generate code itself; it launches and
  constrains existing harnesses through adapters.
- **A frontend build.** Server-rendered HTML and htmx with vendored JS only,
  never an npm build.

## Users

Just me: a single operator who writes or approves the specs, approves at each
review gate, and merges. I work from a terminal, Doom Emacs and a browser.

Agents and runners are *actors*, not users. They authenticate with role-scoped
tokens and can do only what their role and their run's current phase allow.

## Components

| Component        | Role                                                                    | MVP |
|------------------|-------------------------------------------------------------------------|-----|
| **factoryd**     | FastAPI + SQLite server: runs, stages, events, gates, review queue, scheduler; HTTP/JSON + SSE for UIs, MCP (streamable HTTP) for agents | yes |
| **runner**       | Pulls jobs from factoryd, creates git worktrees, launches agents headless or in tmux | yes, one machine |
| **CLI** (`refine`) | Typer client of the HTTP API                                          | yes |
| **Web UI**       | Server-rendered HTML + htmx, vendored JS (Mermaid, Cytoscape.js, Observable Plot, CodeMirror) | later |
| **Emacs client** | Separate package on the same API                                        | later |
| **Spec plugins** | TLA+ (TLC, Apalache, TLAPS), Lean, Quint; frameworks OpenSpec, Spec Kit | TLA+ (TLC) + OpenSpec |
| **Agent adapters** | Claude Code, OpenCode, pi, Mistral Vibe                               | Claude Code |
| **Merge queue**  | factoryd serialises approved changes onto `main`                        | later |

## MVP

**Done when** a Refinery change goes from start to merged PR entirely through
Refinery, with every phase transition recorded and enforced by factoryd, and
the next change after that is developed the same way.

In scope:

- **Start.** I start a workflow on an OpenSpec change with `refine`. factoryd
  creates the run, and the local runner creates a worktree and branch and
  launches Claude Code.
- **Phases.** spec → model → review gate → tests → review gate → impl →
  verify → archive, as in AGENTS.md. Each transition goes through factoryd,
  which refuses out-of-order transitions. The agent works only through the
  bootstrap MCP tools: `start_workflow`, `get_task`, `run_gate`,
  `report_progress`, `submit_for_review`.
- **Gates.** `run_gate` runs the same checks as `make gate`, via the single
  subprocess wrapper, and records every result as an event.
- **Review.** The run's GitHub PR is where I read the spec delta, the TLA+
  delta and the diff. factoryd pushes the branch and opens the PR at the first
  review gate, and pushes again before each later one. I record each decision with `refine approve` or
  `refine reject` with a note. `submit_for_review` returns the decision and,
  on rejection, the note; `get_task` also includes the latest note, so an
  agent that restarted still sees it.
- **Approve to merge.** Final approval archives the OpenSpec change and
  commits and pushes it on the run's branch. GitHub Actions runs `make gate`,
  and I merge the PR on GitHub.
- **Run record.** Direction: a committed `.refinery/run.json` on the run's
  branch, plus a gitignored `.refinery/events.jsonl` as the local, fail-open
  event log. The final decision is made in `run-lifecycle`.

Out of scope for the MVP: the web UI, rendered TLA+ state graphs, merge queue,
runners on more than one machine, other adapters, other spec plugins
(Apalache, TLAPS, Lean, Quint, Spec Kit), the Emacs client.

## First OpenSpec changes

In order; each builds on the one before it.

1. **`run-lifecycle`.** `spec/Run.tla` defines the run state machine: phases,
   review gates and terminal states, with `TypeOK`, phase-ordering invariants
   and reachability checks. Implementation: runs, stages and events in SQLite
   (WAL), rebuildable from the run record (`.refinery/run.json` and
   `.refinery/events.jsonl`, finalised here); one function per TLA+ action;
   event names equal to action names; a trace-validation test that replays an
   event log against the spec.
2. **`gate-runner`.** The single subprocess wrapper (timeout, cancellation,
   process-group kill) that every TLC, git and agent call goes through, plus
   gate execution that records results as events and fails closed.
3. **`mcp-workflow`.** factoryd's MCP endpoint with the five bootstrap tools,
   role-scoped tokens and server-side phase enforcement. From here on, every
   Refinery change goes through these tools (AGENTS.md, Bootstrap note); until
   the runner exists, the agent is launched by hand.
4. **`runner`.** `spec/Runner.tla` models job claiming, runner crash and
   restart, and cancellation, with invariants such as "a job is held by at
   most one runner" and "a cancelled run leaves no agent running".
   Implementation: the local runner pulls jobs from factoryd, creates the
   worktree and branch, and launches Claude Code headless or in tmux through
   the subprocess wrapper.
5. **`review-queue`.** `submit_for_review` returning the decision and note,
   `get_task` including the latest note, the review queue, approve/reject, and
   the `refine` CLI for start, status, review, approve and reject. Completes
   the MVP.

After the MVP, the **web UI** comes first: run list, live events (SSE), the
review queue, and starting and cancelling runs from the browser.
