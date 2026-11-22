# Tasks

Phases follow AGENTS.md, driven through factoryd (`factoryd-shutdown.1`).
No TLA+ change (see proposal, Impact): the model phase is skipped, with the
reason given.

## 1. Tests (written failing; frozen after the tests review gate)

- [x] 1.1 In `tests/factoryd/test_shutdown.py`, run `refinery serve` as a
      subprocess with `REFINERY_SHUTDOWN_TIMEOUT=1`, and hold a stalled
      connection: a POST whose body never finishes. Cover:
      - one SIGINT;
      - one SIGTERM;
      - SIGINT twice, 0.5 s apart.

      Each must exit within 6 s; past that, the test kills the server.
      Verify: they fail today (the server is still running at 6 s).
- [x] 1.2 In the same file, with a gate step running (a `make` target that
      sleeps), a stalled connection, and SIGINT twice: factoryd exits
      within 6 s, the step's process group is gone, no gate result is
      recorded, and the run is still in `verify`. Verify: it fails today.
- [x] 1.3 Check that factoryd's stderr after a stalled-connection shutdown
      has no `Traceback` and has one line saying how many requests were
      cut off. Verify: it fails today.
- [x] 1.4 In the config tests: `REFINERY_SHUTDOWN_TIMEOUT` sets
      `ServerConfig.shutdown_timeout_s`, which defaults to 5. A skeleton
      field (default 5, the variable not read yet) keeps pyright clean.
      Verify: it fails on the variable.

## 2. Implementation

- [x] 2.1 Add `ServerConfig.shutdown_timeout_s`, read from
      `REFINERY_SHUTDOWN_TIMEOUT`. Verify: 1.4 passes.
- [x] 2.2 Make the app's shutdown idempotent and expose it (D2). `serve`
      runs uvicorn with `timeout_graceful_shutdown`, then calls the
      shutdown (D1, D2). Verify: 1.1 and 1.2 pass.
- [x] 2.3 Add the log filter for cut-off requests (D3). Verify: 1.3
      passes.

## 3. Verify

- [x] 3.1 Verify: `make gate` is green.
- [ ] 3.2 By hand: the real factoryd with this Claude Code session
      connected. Ctrl-C once exits within about 5 s, and Ctrl-C twice
      also exits, with no traceback.
