# agent-tools Specification

## Purpose

Defines the MCP tools through which humans start runs and agents do their
work. Covers what each tool does in each phase, which role may call it, and
how it fails. Enforcement lives here, in factoryd, not in agent prompts. The
behaviour is modelled by `spec/Workflow.tla`.

## Requirements

### Requirement: Tool set
factoryd SHALL expose exactly these MCP tools over streamable HTTP at `/mcp`:
`start_workflow`, `get_task`, `submit_for_review`, `escalate`, `run_gate` and
`report_progress`.

#### Scenario: Listing tools
- **WHEN** an authenticated client lists the tools
- **THEN** it gets exactly those six names

### Requirement: Tools are restricted by role
`start_workflow` SHALL require a human token, and every other tool an agent
token. A call with a token of the wrong role SHALL fail with a tool error and
change nothing.

#### Scenario: Agent cannot start a run
- **WHEN** an agent token calls `start_workflow`
- **THEN** the call fails and no run is created

#### Scenario: Human token cannot submit
- **WHEN** a human token calls `submit_for_review`
- **THEN** the call fails and no run changes

### Requirement: Agent calls act on the token's run
Agent tools SHALL take no run parameter. They SHALL act on the run their
token is bound to, and on no other.

#### Scenario: Two runs, two agents
- **GIVEN** runs `a.1` and `b.1`, each with its own agent token
- **WHEN** the agent of `a.1` calls `submit_for_review`
- **THEN** only `a.1` changes

### Requirement: Starting a workflow
`start_workflow(change, worktree)` SHALL perform `StartRun` on `change`, with
the run's record in `worktree`. It SHALL return the run id, the phase and a
new agent token for the run, which is shown only in this response. The
worktree SHALL be one of factoryd's configured roots.

#### Scenario: Start
- **WHEN** a human starts a workflow on change `demo` in a configured root
- **THEN** the response has run id `demo.1`, phase `spec` and an agent token that works for `demo.1`

#### Scenario: Worktree outside the roots
- **WHEN** `worktree` is not a configured root
- **THEN** the call fails and no run is created

#### Scenario: Change already has a run
- **GIVEN** change `demo` has an unfinished run
- **WHEN** a human starts another workflow on `demo`
- **THEN** the call fails naming the existing run

### Requirement: Getting the task
`get_task` SHALL return:
- the run id, change, phase and worktree;
- whose turn it is (agent, human or factoryd);
- what the agent should do now;
- each frozen artifact with its approved commit and paths;
- the latest real gate run's id, verdict and failed steps, if any;
- the last progress messages.

#### Scenario: Task in the spec phase
- **WHEN** the agent of a run in `spec` calls `get_task`
- **THEN** it learns the phase is `spec`, it is the agent's turn, and it must write the OpenSpec change then call `submit_for_review`

#### Scenario: Task while waiting for review
- **WHEN** the agent of a run in `spec_review` calls `get_task`
- **THEN** it learns it is the human's turn and that it should wait

#### Scenario: Task after a failed gate
- **GIVEN** the run's latest gate run `g2` failed in `types`
- **WHEN** the agent calls `get_task`
- **THEN** the response names `g2`, verdict fail, and `types` as a failed step

### Requirement: Submitting completes the agent phase
`submit_for_review(skip_model_reason?)` SHALL perform the transition that
completes the current agent phase:

| Phase | Transition |
|---|---|
| spec | `CompleteSpec` |
| model | `CompleteModel`, or `SkipModel` when a reason is given |
| tests | `CompleteTests` |
| impl | `CompleteImpl` |

In any other phase it SHALL fail without changing the run. A
`skip_model_reason` outside `model` SHALL fail.

#### Scenario: Submitting the model
- **GIVEN** a run in `model`
- **WHEN** the agent calls `submit_for_review()`
- **THEN** the run is in `spec_review` and the event is `CompleteModel`

#### Scenario: Skipping the model
- **GIVEN** a run in `model`
- **WHEN** the agent calls `submit_for_review(skip_model_reason="docs only")`
- **THEN** the event is `SkipModel` with that reason

#### Scenario: Not the agent's turn
- **GIVEN** a run in `tests_review`
- **WHEN** the agent calls `submit_for_review`
- **THEN** the call fails, saying it is the human's turn, and the run is unchanged

#### Scenario: Submitting implementation starts the gate
- **GIVEN** a run in `impl`
- **WHEN** the agent calls `submit_for_review`
- **THEN** the run is in `verify` and factoryd has started its gate run

### Requirement: Escalating
`escalate(bug, reason)` SHALL perform `Escalate` with classification `bug`
(`spec_bug` or `test_bug`) and the reason.

#### Scenario: Escalate a test bug
- **GIVEN** a run in `impl`
- **WHEN** the agent calls `escalate(bug="test_bug", reason="test 3 contradicts scenario 2")`
- **THEN** the run is in `escalated` with that classification and reason

### Requirement: Dry-run gate for agents
`run_gate` SHALL run a dry-run gate (see verify-gate) and return each step's
result and log path. It SHALL never move the run.

#### Scenario: Dry run in impl
- **GIVEN** a run in `impl`
- **WHEN** the agent calls `run_gate`
- **THEN** it gets the five step results, and the run is still in `impl`

#### Scenario: Dry run outside tests and impl
- **GIVEN** a run in `spec`
- **WHEN** the agent calls `run_gate`
- **THEN** the call fails

### Requirement: Progress reports fail open
`report_progress(message)` SHALL append the message, with time and run, to
`<data>/runs/<run-id>/progress.jsonl`, and SHALL never be a transition. If the
message cannot be written, the call SHALL still succeed and say it was not
recorded.

#### Scenario: Progress recorded
- **WHEN** the agent reports "tests for scenario 3 written"
- **THEN** the message is the last line of the run's `progress.jsonl` and appears in `get_task`

#### Scenario: Progress cannot be written
- **GIVEN** the run's progress file cannot be written
- **WHEN** the agent reports progress
- **THEN** the call succeeds, reporting that the message was not recorded, and the run is unchanged

### Requirement: Refused transitions are tool errors
When a tool's transition is refused (wrong phase, missing payload, run
ended), the tool SHALL fail with the refusal's reason, and the run record and
event log SHALL be unchanged.

#### Scenario: Escalate without a reason
- **WHEN** the agent calls `escalate(bug="spec_bug", reason="")`
- **THEN** the call fails with the refusal's reason, and the run is unchanged

### Requirement: The agent sees the human's last decision
`get_task` SHALL include `last_decision`: the action, note and time of the
run's most recent human decision (an approval, rejection, resolution or
cancellation), or null if there is none. It SHALL also include `pr`, the run's
PR URL or null.

#### Scenario: After a rejection
- **GIVEN** the human rejected run `demo.1`'s spec with note "scenario 2 has no THEN"
- **WHEN** the agent calls `get_task`
- **THEN** `last_decision` is `RejectSpec` with that note, and the phase is `spec`

#### Scenario: Before any decision
- **WHEN** the agent of a new run calls `get_task`
- **THEN** `last_decision` is null
