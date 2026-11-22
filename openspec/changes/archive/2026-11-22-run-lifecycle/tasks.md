# Tasks

Phases follow AGENTS.md. Group 1 is a separate tooling commit that lands first.
Group 2 is the tests phase: an API skeleton plus tests written failing, then
frozen at the tests review gate. Group 3 must not touch `spec/`, `openspec/specs/` or the frozen tests.

## 1. Tooling prerequisite (separate commit)

- [x] 1.1 Make `make tla` and `make tla-big` run SANY and TLC from inside
      `spec/`. Verify: `make tla` passes with `RunPair.tla` (`EXTENDS Run`)
      present.
- [x] 1.2 Teach `make tla` to check `spec/<M>_reach.cfg`: one TLC run per
      listed invariant, and fail unless each one is violated. Verify: all
      six witnesses are reported reachable, and a scratch witness that can't
      be violated (`TRUE`) makes `make tla` fail.
- [x] 1.3 Document the `_reach.cfg` convention in AGENTS.md's formal-methods
      rules. Verify: the text matches what 1.2 does.

## 2. Tests (written failing; frozen after the tests review gate)

- [x] 2.0 Write the API skeleton under `src/refinery/run/`: complete domain
      types in `model.py`, and every other public function and class with its
      signature and a docstring, raising `NotImplementedError`. This replaces
      the old 3.1: the tests need an importable, type-checked API so they fail
      one by one instead of erroring at collection. Verify: `uv run pyright`
      is clean.
- [x] 2.1 In `tests/run/test_transitions.py`, write one test per scenario in
      `specs/run-lifecycle/spec.md`. Verify: each test fails with
      `NotImplementedError` from the skeleton.
- [x] 2.2 In `tests/run/test_transition_properties.py`, write hypothesis
      tests over random sequences of (transition, role, payload) requests,
      mirroring the TLA+ invariants:
      - `FrozenMatchesPhase`
      - `ApprovalsBeforeArchive`
      - `CommittedTraceValid` (via `ReplayValidator`)
      - `TerminalIsFinal`
      - `FrozenReleasedOnlyByResolve`

      Verify: they fail with `NotImplementedError`.
- [x] 2.3 In `tests/run/test_store.py`, write one test per scenario in
      `specs/run-record/spec.md` that needs no index:
      - atomic record write
      - append failure refuses the transition
      - recovery after a crash between event and record
      - sealing and tamper detection
      - archive with pass, fail, and validator error or timeout counting as
        fail
      - `RetryArchive`
      - two concurrent `StartRun`s on one change, with exactly one succeeding

      Verify: they fail with `NotImplementedError`.
- [x] 2.4 In `tests/run/test_store_crash.py`, write a hypothesis
      `RuleBasedStateMachine` that interleaves transitions with crashes
      injected between the event append and the record write, then recovers.
      After every step it checks `LogMatchesRecord`, `CommittedTraceValid`,
      `CommittedEventsImmutable` and `OneActiveRunPerChange` (multi-run).
      Verify: it fails with `NotImplementedError`.
- [x] 2.5 In `tests/run/test_index.py`, write the rebuild scenarios:
      - deleting the DB and rebuilding gives an identical index
      - a missing log is flagged `events_missing`
      - when two copies of a record exist, the higher `seq` wins

      Verify: they fail with `NotImplementedError`.
- [x] 2.6 In `tests/run/test_trace_export.py`, write two kinds of test:
      - golden-file tests of `export_trace` for a happy-path log and a log
        with reject and escalate events
      - a parity test that the set of transition names equals `Acts` parsed
        from `spec/Run.tla`

      Verify: they fail with `NotImplementedError`.
- [x] 2.7 Run `uv run pytest -q tests/run`. Every new test should fail for
      the stated reason and nothing should error in collection. Then stop for
      the tests review gate.

## 3. Implementation

- [x] 3.2 In `src/refinery/run/transitions.py`, write the transition table
      and one pure function per TLA+ action, named after it (D2). Verify:
      2.1 and 2.2 pass.
- [x] 3.3 In `src/refinery/run/record.py` and
      `src/refinery/run/eventlog.py`, implement the atomic record write,
      append+fsync with fail-closed refusal, recovery truncation, and sealing
      and verification (D4, D5). Verify: the non-archive tests in 2.3, and
      2.4, pass.
- [x] 3.4 In `src/refinery/run/archive.py`, implement the `TraceValidator`
      protocol, `ReplayValidator`, and the Archive / AbortArchive /
      RetryArchive flow, where errors and timeouts become `fail` (D7).
      Verify: the archive tests in 2.3 pass.
- [x] 3.5 In `src/refinery/run/index.py`, implement the SQLite (WAL) schema
      with a partial unique index, applying transitions, and rebuild (D6).
      Verify: 2.5 passes.
- [x] 3.6 In `src/refinery/run/trace_export.py`, generate the
      `Trace_<run>.tla` and `.cfg` text (D7). Verify: 2.6 passes.
- [x] 3.7 In `src/refinery/run/store.py`, write `RunStore`: per-run locks, the
      start lock, run-id allocation, and startup recovery (D3, D5). Verify:
      the concurrent-start test in 2.3 passes.
- [x] 3.8 Update the run-record paragraph in `docs/vision.md` to the final
      decision: a per-run record and the log in the data directory. Verify:
      it matches proposal.md.

## 4. Verify

- [x] 4.1 Verify: `make gate` is green, and `make tla-big` reports no
      violation.
- [x] 4.2 Check by hand: export the trace of one test-fixture run and run TLC
      on it. Verify: TLC reports `TraceIncomplete` violated, meaning the trace
      was accepted. The automated version lands in `gate-runner`.

## Workflow follow-up

- Review gate 1 (spec + model): now. Wait for approval before group 1.
- Review gate 2 (tests): after 2.7.
- Final review, then archive the change, commit and open a PR.
