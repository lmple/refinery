# Design: review-queue

## Context

factoryd serves the agent tools over MCP; human decisions have no surface
yet. See proposal.md for motivation, and the five spec deltas for the
requirements. The model is `spec/Review.tla`.

What exists:
- `RunStore` (transitions, archive with a validator);
- `TlcTraceValidator`;
- `Supervisor` (the only way to run `git`, `gh` and `openspec`);
- `Workflow`, the tool service.

## Goals / Non-Goals

**Goals:**
- **One service, two surfaces.** One `Review` service holds every human
  operation, used by the HTTP API, and through it by `refine`. The agent side
  stays in `Workflow`.
- **Reviewed commits are facts.** A review is always of a pushed commit that
  factoryd recorded itself.
- **A resumable archive.** The archive sequence is idempotent, and can be
  resumed after a crash or a failure.

**Non-Goals:**
- **The web UI** (the next change after `runner`).
- **A merge queue.** You merge on GitHub.
- **Rendering diffs or specs in factoryd.** GitHub shows the PR.
- **Rewriting history or force-pushing.** factoryd only appends commits and
  pushes fast-forward.

## Decisions

### D1. Layering

- **`refinery.review.service.Review(store, supervisor, config, workflow)`:**
  `queue()`, `show(run)`, `start(...)`, `approve(run, note)`,
  `reject(run, note)`, `cancel(run)`, `resolve(run, to)` and
  `retry_archive(run)`. Each returns plain data or raises `ReviewError(status,
  message)`, where the status (404, 409 or 422) maps to the HTTP response.
- **`refinery.review.git`:** small functions over `Supervisor`:
  - `head`, `branch`, `default_branch`, `dirty_paths(exclude=".refinery/")`;
  - `commit_paths`, `push`;
  - `gh_pr_create`, `gh_pr_ready`, `gh_pr_url`.
- **`refinery.review.publish`:** `publish(run)` (D3) and the archive
  sequence (D4).
- **`refinery.factoryd.api`:** FastAPI routes under `/api` with a human-token
  dependency (`Authorization: Bearer`, verified by `Workflow.authenticate`).
  The router is mounted in `create_app` next to `/mcp`.
- **`refinery.cli.refine`:** a Typer app using `httpx2`, wired as the
  console script `refine = "refinery.cli.refine:app"`.

Rejected alternative: human tools over MCP. A CLI and, later, the web UI need
HTTP anyway, and humans don't drive MCP clients.

### D2. Reviewed commit and clean worktree

- **Approval.** `Review.approve` in a review gate refuses with 409 in two
  cases:
  - `dirty_paths()` is not empty;
  - `HEAD` differs from `published`, the commit factoryd published for this
    gate (D3, stored in `publish.json`), because the agent committed after
    publishing and the human hasn't seen the new commit.

  Otherwise it calls the transition with `commit=HEAD`. Approval doesn't
  commit anything: the run record it changes is committed at the next
  publishing.
- **Submission.** `Workflow.submit_for_review` checks `dirty_paths()` before
  `CompleteModel`, `SkipModel`, `CompleteTests` and `CompleteImpl`. A
  submission from `spec` (`CompleteSpec`) is a hand-over to the model phase,
  not a review, so it doesn't need a clean worktree.
- **`dirty_paths`** is `git status --porcelain --untracked-files=all`, minus
  `.refinery/`, minus ignored files. Any git failure counts as dirty (fail
  closed).

### D3. Publishing

`publish(run)` runs after every transition into `spec_review`,
`tests_review`, `final_review` or `escalated`. Each step is idempotent:
1. Commit the run record: `git add .refinery/runs/<id>.json && git commit -m
   "run: <id> <phase>"`, only when the file changed.
2. Push: `git push -u origin <branch>`, fast-forward only.
3. Find the branch's PR: `gh pr list --head <branch> --json url`. If there
   isn't one, create it: `gh pr create --draft --base <default> --head
   <branch> --title "<change>" --body <summary>`.
4. Write `publish.json`: `{branch, published, pr, last_error}`, where
   `published` is `HEAD` after step 1. It is recorded even when the push
   fails, since the human then reviews locally (fail open).

Errors are caught, stored in `last_error`, logged, and shown by
`show(run)`/`get_task` (fail open: publishing is not a gate). Publishing runs
in the same task as the transition that triggered it. It awaits the
supervisor, so it doesn't block the server.

`Workflow` calls `publish` after its own agent submissions that land in a
review. These are agent-driven moves into `spec_review` and `tests_review`,
and factoryd's `PassGate` into `final_review`.

### D4. Archive sequence

`Review.approve` in `final_review` applies `ApproveFinal`, then runs
`archive_sequence(run)`. Each step checks whether it is already done:

| Step | Already done when |
|---|---|
| a. `openspec archive <change> --yes` in the worktree, then commit `<change>: archive the OpenSpec change` | `openspec/changes/<change>/` no longer exists |
| b. `RunStore.archive(run, TlcTraceValidator(...))` | the run is `archived` |
| c. commit `run: <id> archived`, push, `gh pr ready` | the record is committed, the push is up to date, and the PR isn't a draft |

- **Failure.** A failure in a or c leaves the run in `archive` and is stored
  in `publish.json`'s `last_error`. A trace failure at b gives verdict `fail`,
  as `run-record` specifies.
- **Retry.** `Review.retry_archive(run)` applies `RetryArchive` if the verdict
  is `fail`, then runs the sequence again from the first step not yet done.
- **Restart.** On startup, `Workflow.open` resumes the sequence for every run
  in `archive` whose verdict is `none`, alongside its gate rescheduling.

Rejected alternative: the agent archives the OpenSpec change before the final
review. AGENTS.md puts archiving after the human's approval.

### D5. Last decision and PR in `get_task`

`last_decision` is the newest event whose name is an approval, a rejection, a
`ResolveTo*` or `Cancel`, given as `{action, note, at}`. `pr` comes from
`publish.json`.

### D6. Start checks

`start_workflow` and `Review.start` both call
`git.branch(worktree)`. They refuse if the branch is the default branch
(`git symbolic-ref refs/remotes/origin/HEAD`, falling back to `main`), or if
there is no branch (detached HEAD).

### D7. API shape and authentication

- **Bodies.** Pydantic request models (`ApproveBody{note?}`,
  `RejectBody{note}`, `ResolveBody{to: Literal[...]}`, `StartBody{change,
  worktree}`) and JSON responses.
- **Authentication.** A dependency resolves the bearer token with
  `Workflow.authenticate`. There's no token, or the token is unknown, gives
  401; an agent token gives 403.
- **Errors.** `ReviewError` maps to `{"error": …}` with its status.
  FastAPI's own validation errors stay 422.

### D8. `refine`

- **Configuration.** `REFINE_URL` (default `http://127.0.0.1:8740`) and
  `REFINE_TOKEN` (required, from the environment, as AGENTS.md says). There
  are no config files.
- **Output.** One line per run in `runs`, and a short block in `status`.
  Errors go to stderr with exit 1. A missing token gives exit 2.

### D9. Model (`spec/Review.tla`)

One run, from the agent's submission into a review to finished: archived,
pushed, PR ready.

**Abstraction.** Commits are booleans rather than ids: `atPublished` (`HEAD`
is the commit published for the gate) and `atPushed` (`HEAD` is what origin
has). The state space is therefore finite without a step bound, and liveness
is checked exactly.

**Phases** are coarsened from `Run.tla`:
- `work` stands for the agent phases;
- `review` stands for `spec_review` and `tests_review`;
- reaching `final_review` stands for `CompleteImpl` followed by `PassGate`.

**Actions:**
- the agent and its environment: `Edit` and `Commit` at any time, and
  `Submit` guarded by a clean worktree;
- factoryd publishing: `PublishRecord`, then `PublishPush` or `PushFails`
  (fail open: the gate counts as published at the local `HEAD`);
- the human: `Approve` and `ApproveFinal` (guarded by a clean worktree at the
  published commit), `Reject`, and `RetryArchive` (after a failed step or a
  failed trace);
- factoryd archiving: `ArchiveSpecs` or `ArchiveSpecsFails`, then
  `ValidateTrace`, `FinishRecord`, `FinishPush` and `FinishReady`;
- the environment: `Crash` (only with work in flight) and `Recover`.
  `Recover` republishes any gate whose record is uncommitted or whose `HEAD`
  isn't the published commit; the archive sequence resumes from persisted
  state.

**Properties:**
- invariants: `ArchivedOnlyAfterEverything`, `ReadyOnlyWhenArchived`,
  `PushedGateHasAPR`;
- action properties: `ApprovalsAreOfPublishedCommits`,
  `ReviewsAreOfCommits`;
- liveness: `GatesGetPublished`, `GateRecordsGetCommitted` (publishing owed
  at a crash still happens) and `ArchiveCompletes`, under weak fairness of
  factoryd's own steps, justified in the module.

| Config | Scope | Size |
|---|---|---|
| `Review.cfg` | 1 crash | 473 states |
| `Review_big.cfg` | 3 crashes | 1,049 states |

Reachability witnesses: finished, approved after a failed push, resumed
after a crash, trace failure before a retry, and approval blocked by a commit
made after publishing.

The first TLC run found a flaw in `ApprovalsAreOfPublishedCommits` itself. It
identified an approval as "the decision is approved and the phase changes",
so factoryd's later archive steps matched too. The fix identifies approvals
by their phase transition.

Planted bugs checked:

| Bug | Caught by |
|---|---|
| approval without the published-commit check | `ApprovalsAreOfPublishedCommits` |
| recovery that doesn't republish | `GateRecordsGetCommitted` |
| archive before the OpenSpec archive | `ArchivedOnlyAfterEverything` |
| PR marked ready in `archive` | `ReadyOnlyWhenArchived` |
| submitting with uncommitted work | `ReviewsAreOfCommits` |

## Risks / Trade-offs

- **[factoryd now pushes with your git credentials and calls `gh`]** → Only
  to the run's own branch, never the default branch (D6), and only
  fast-forward. A push failure is visible and doesn't block.
- **[`openspec` is an external CLI on `PATH`]** → It's already in use. If
  it's missing, step a fails closed; the run waits in `archive` and can be
  retried.
- **[The record commits add noise to the PR history]** → They're one small
  commit per gate, labelled `run:`. Squash-merging on GitHub removes them.
- **[A long `gh` or `git push` delays the transition's response]** → They're
  bounded by supervisor time limits (30 s). A timeout is a publishing error
  (fail open).
- **[The bootstrap run itself (`review-queue.1`) cannot use these tools until
  merged]** → Its decisions are the last ones made through the bootstrap
  script; this is documented in the PR.

## Open Questions

- The PR body template: a link to the change's proposal is enough for now.
