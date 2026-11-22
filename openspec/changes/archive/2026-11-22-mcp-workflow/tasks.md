# Tasks

Phases follow AGENTS.md:
- **Group 1 (tests phase):** an API skeleton plus tests written failing, then
  frozen at the tests review gate.
- **Group 2 (implementation):** must not touch `spec/`, `openspec/specs/` or
  the frozen tests.

## 1. Tests (written failing; frozen after the tests review gate)

- [x] 1.0 Write the API skeleton, with new behaviour raising
      `NotImplementedError`:
      - `refinery.auth.tokens` (`TokenStore`, `Token`, `Role`,
        `RefineryTokenVerifier`);
      - `refinery.factoryd` (`ServerConfig`, `Principal`, `Workflow`,
        `WorkflowError`, `create_app`, the MCP builder, the Typer CLI,
        progress);
      - `GateRunner.dry_run`.

      Wire the console script to the CLI. Verify: `uv run pyright` is clean.
- [x] 1.1 In `tests/auth/test_tokens.py`, write the access-tokens scenarios
      at store level:
      - format;
      - hashes only and mode 0600;
      - verify, revoke and list;
      - an agent token bound to its run, rejected once the run is cancelled.

      Verify: they fail with `NotImplementedError`.
- [x] 1.2 In `tests/factoryd/test_workflow.py`, write every agent-tools
      scenario against `Workflow` with `Principal`s:
      - roles and run binding;
      - `start_workflow`, including the roots check and an existing run;
      - `get_task` per phase, after a failed gate, and with progress;
      - `submit_for_review` per phase, the skip reason, the wrong turn, and
        impl starting the gate;
      - `escalate`, `run_gate` dry runs, `report_progress` failing open;
      - refusals as errors.

      Verify: they fail with `NotImplementedError`.
- [x] 1.3 In `tests/gate/test_dry_run.py` and
      `tests/factoryd/test_scheduling.py`, write the verify-gate additions:
      - a dry run leaves the run alone, and writes its records under
        `dry-runs/`;
      - a dry run is killed when the run leaves `impl`;
      - no overlap with gate runs, and submit refused during a dry run;
      - the gate starts on `CompleteImpl` and after a restart;
      - a gate error fails closed.

      Verify: they fail with `NotImplementedError`.
- [x] 1.4 In `tests/factoryd/test_server.py`, write the factoryd-server
      scenarios:
      - default address from the config;
      - startup order: an orphan is reaped before the restarted gate of a
        run in `verify`;
      - shutdown mid-gate kills the step and records nothing.

      Verify: they fail with `NotImplementedError`.
- [x] 1.5 In `tests/factoryd/test_mcp_http.py`, write the end-to-end checks
      over real HTTP, with uvicorn on an ephemeral port and the SDK client:
      - 401 with no token, an unknown token and a revoked token;
      - the tool list;
      - a role refusal;
      - the `start_workflow` → `get_task` → `submit_for_review` round trip.

      Verify: they fail with `NotImplementedError`.
- [x] 1.6 In `tests/factoryd/test_cli.py`, write the admin CLI tests with
      Typer's `CliRunner`:
      - `token create --role human` prints the token once;
      - `--role agent` is refused;
      - `list` shows no secrets;
      - `revoke`.

      Verify: they fail with `NotImplementedError`.
- [x] 1.7 Run `uv run pytest -q`: every new test fails for the stated reason,
      all existing tests still pass, and nothing errors in collection. Then
      stop for the tests review gate.

## 2. Implementation

- [x] 2.1 Implement tokens and the SDK verifier (D2). Verify: 1.1 passes.
- [x] 2.2 Implement `GateRunner.dry_run` and `fail_closed` (D4, D5). Verify:
      the dry-run tests in 1.3 pass, and the `tests/gate` suite still
      passes.
- [x] 2.3 Implement progress and the `Workflow` tool operations (D3, D6,
      D7). Verify: 1.2 passes.
- [x] 2.4 Implement scheduling, startup and shutdown (D4). Verify: the
      scheduling tests in 1.3, and 1.4, pass.
- [x] 2.5 Implement the MCP adapter, `create_app` and the CLI (D1). Verify:
      1.5 and 1.6 pass.

## 3. Verify

- [x] 3.1 Verify: `make gate` is green, and `make tla-big` reports no
      violation.
- [x] 3.2 End to end, by hand: run `refinery serve` on a scratch data
      directory with this repository as root. Create a human token and call
      `start_workflow` from a real MCP client (the SDK client in a script).
      Then drive `get_task` and `submit_for_review` with the agent token
      through to `verify`, and see the gate start and record a result.
      Verify: the run's events show the expected transitions, and nothing is
      left in the process registry after shutdown.

## Workflow follow-up

- Review gate 1 (spec + model): now. Wait for approval before group 1.
- Review gate 2 (tests): after 1.7.
- Final review, then archive the change, commit and open a PR.
