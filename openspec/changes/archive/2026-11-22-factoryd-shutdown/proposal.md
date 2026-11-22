# Proposal: factoryd-shutdown

## Why

Ctrl-C can't stop `refinery serve` while a client keeps a connection open.
Claude Code's MCP client does exactly that: it holds a streaming `GET /mcp`
for the whole session. It happened twice during the `review-queue` run.
Any number of Ctrl-Cs and SIGTERMs were ignored, and only SIGKILL stopped
the server. A SIGKILL also skips factoryd's own shutdown, so gate processes
are left for the next start to reap.

This happens on every Ctrl-C while an agent is connected, so it blocks
everyday use. It comes before `runner`, which starts and stops factoryd
much more often.

## What Changes

- **Bounded shutdown.** On SIGINT or SIGTERM, factoryd stops accepting
  connections and gives open requests up to a shutdown timeout to finish.
  It then closes whatever is still open and exits. That holds even if a
  client never lets go, and however many signals arrive.
- **factoryd's own shutdown always runs.** Cancelling gate runs, killing
  their processes and closing the run index happen before the process
  exits. That includes the forced path, a second Ctrl-C.
- **A quiet exit.** Requests cut off by the timeout get one log line, not
  a traceback per request.
- **New setting.** `REFINERY_SHUTDOWN_TIMEOUT` (seconds, default 5).

## Capabilities

### Modified Capabilities

- `factoryd-server`: Shutdown now runs factoryd's own cleanup on every path,
  forced exits included. New requirements: Shutdown timeout, Bounded
  shutdown, and Cut-off requests are logged once.

## Impact

- **Code:**
  - `src/refinery/factoryd/cli.py`: `serve` runs uvicorn with a bounded
    graceful shutdown, then runs factoryd's own shutdown if uvicorn
    skipped it.
  - `src/refinery/factoryd/app.py`: the shutdown is idempotent and can be
    called from outside the lifespan.
  - `src/refinery/factoryd/config.py`: the timeout setting.
- **Tests:** `tests/factoryd/test_shutdown.py` gains stalled-connection
  cases. The existing SIGINT test missed the bug because its client closes
  its own connection.
- **TLA+:** no change. `Workflow.tla` already models a server crash at any
  point, the worst case of this change (gate runs killed, rescheduled on
  restart). A bounded shutdown adds no state and no transition.
- **Dependencies:** none. `timeout_graceful_shutdown` is an existing
  uvicorn option.
