# Proposal: review-queue

## Why

Runs reach review gates, but no human can act on them through factoryd. Human
decisions still bypass the server: the `review-queue.1` run that writes this
change uses a bootstrap script. Archiving isn't wired either. This change
completes the MVP loop (docs/vision.md):
- you review on the GitHub PR;
- you decide with `refine`;
- factoryd records, publishes and archives.

From then on, every change, starting with `runner`, goes through factoryd end
to end.

## What Changes

- **Human HTTP API**, under `/api` and authenticated with human tokens:
  - list and show runs, and the review queue (runs waiting for you);
  - start a run;
  - approve, reject with a note, cancel, resolve an escalation, retry an
    archive.

  Refusals are JSON errors with proper status codes, and nothing changes.
- **`refine` CLI**, a Typer client of that API: `refine start`, `runs`,
  `status`, `approve`, `reject`, `cancel`, `resolve` and `retry-archive`. It
  reads `REFINE_URL` and `REFINE_TOKEN` from the environment.
- **Reviewed commits recorded by factoryd.** On approval, factoryd records
  the worktree's `HEAD` itself, so you never pass a commit. The agent's
  submissions into a review (`CompleteModel`, `SkipModel`, `CompleteTests`,
  `CompleteImpl`) are refused while the worktree has uncommitted work.
  factoryd's own run record (`.refinery/`) is excepted.
- **Publishing.** On entering a human review gate, factoryd commits the run
  record and pushes the run's branch. At the first gate it opens a draft PR
  with `gh`. Publishing fails open: a push or PR error is shown, and the
  review can still proceed.
- **Archive on final approval.** `ApproveFinal` moves the run to `archive`,
  where factoryd:
  1. runs `openspec archive <change> --yes` and commits the result;
  2. validates the trace with TLC and records `Archive`;
  3. commits the final run record, pushes, and marks the PR ready.

  Any failure keeps the run in `archive` (fail closed), and retrying resumes
  where it stopped. You merge on GitHub.
- **Decisions reach the agent.** `get_task` gains `last_decision` (action,
  note, time) and `pr` (the URL). `submit_for_review` still returns
  immediately.
- **Starting a run** (`start_workflow` and `refine start`) is refused on a
  worktree that is on `main` or a detached HEAD.
- **TLA+:** `spec/Review.tla` models the review gate with a worktree (clean or
  dirty, `HEAD`), publishing, the archive sequence with crashes and retry, and
  the decision feedback. It comes with `Review.cfg`, `Review_big.cfg` and
  `Review_reach.cfg`.
- **`docs/vision.md`:** the change order puts `review-queue` before `runner`,
  and the MVP text matches these decisions.

## Capabilities

### New Capabilities

- `review-queue`: human decisions at review gates; reviewed commits, the
  clean-worktree rule and the queue of runs waiting for the human.
- `review-publishing`: commit, push and PR at review gates; the archive
  sequence at the end.
- `human-api`: the HTTP JSON API for humans, and the `refine` CLI on top of
  it.

### Modified Capabilities

- `agent-tools`: `get_task` reports the last human decision and the PR;
  submissions into a review need a clean worktree; starting needs a branch
  worktree.
- `run-lifecycle`: approvals no longer carry a caller-supplied commit;
  factoryd records the reviewed commit itself.

## Impact

- **New:**
  - `src/refinery/review/` (decisions, queue, publishing, archive
    sequence);
  - `src/refinery/factoryd/api.py` (HTTP routes);
  - `src/refinery/cli/` (`refine`);
  - a `refine` console script in `pyproject.toml`.
- **Changed:**
  - `Workflow` (`get_task` and `submit_for_review` rules, and start
    checks);
  - `create_app` (mounts `/api`);
  - `run.transitions`: `ApproveSpec`/`ApproveTests` still need a commit, but
    factoryd now supplies it.
- **External tools, all already used on this machine:** `git` (commit,
  push), `gh` (PR create and ready) and `openspec` (archive). All run through
  the supervisor. No new Python dependencies: the CLI's HTTP client is
  `httpx2`, already installed through `mcp`.
- **Data:** `<data>/runs/<id>/publish.json` holds the branch, the PR URL and
  the last publishing error.
