# Design: factoryd-shutdown

## Context

`refinery serve` calls `uvicorn.run(app, lifespan="on")`. uvicorn 0.54 on
Python 3.12 shuts down like this:

1. **First SIGINT or SIGTERM:** set `should_exit`, close the listening
   socket, and ask each connection to close once its current response is
   complete.
2. **Wait:** loop while connections remain, unless `force_exit` is set.
   The log says "Waiting for connections to close. (CTRL+C to force
   quit)".
3. **Then:** `await server.wait_closed()`, with no time limit.
4. **Then, unless `force_exit` is set:** run the lifespan shutdown. That's
   where factoryd cancels gate runs and closes the run index.

A second SIGINT sets `force_exit`, which ends step 2 but not step 3. Since
Python 3.12.1, `Server.wait_closed()` waits until every connection has
dropped. So one connection that never closes hangs shutdown forever. The
process then sits idle in `epoll_wait`, sending SSE pings on the open
stream. That is what the hung server showed: one connection from Claude
Code, last written by the server 4 s earlier and last read 40 minutes
earlier.

This was reproduced with a POST whose body never finishes: after three
SIGINTs the server was still running 17 s later. It also corrects
review-queue's D10, which guessed at a blocked event loop.

Still unknown: why the MCP stream itself stayed open. sse-starlette ends
its streams within half a second of uvicorn's `should_exit`, and did in
every trial, with curl and with the MCP SDK client. This change doesn't
depend on the answer. The SIGUSR1 stack dump (D10) is there to catch it
next time.

## Goals / Non-Goals

**Goals:**
- factoryd exits within a known bound after SIGINT or SIGTERM, whatever
  its clients do.
- factoryd's own shutdown (Shutdown requirement) runs on every path that
  ends in an exit.

**Non-Goals:**
- Finding why sse-starlette left that one stream open.
- Changing how agents reconnect after a restart.

## Decisions

### D1. Bound the graceful phase with `timeout_graceful_shutdown`

`serve` passes `timeout_graceful_shutdown=config.shutdown_timeout_s` to
uvicorn. uvicorn wraps steps 2 and 3 in `asyncio.wait_for`. On timeout it
cancels the remaining request tasks, which drops their connections, and
moves on to the lifespan shutdown.

Alternatives considered:
- **Closing transports ourselves on a second signal:** that means reaching
  into uvicorn's private `server_state`.
- **Patching `wait_closed`:** fragile across uvicorn and Python versions.

The option exists for exactly this purpose.

Default: 5 s. That's long enough for a gate API call or a `refine` request
in flight to finish, and short enough that Ctrl-C feels like it works.

### D2. Run factoryd's own shutdown after uvicorn returns

With `force_exit` set (a second Ctrl-C), uvicorn skips the lifespan
shutdown entirely, so gate processes would outlive the server until the
next start reaps them.

- `create_app` keeps its lifespan.
- The shutdown body moves into an idempotent `shutdown()`, stored on
  `app.state`. It pops the opened `Workflow`, if any, and closes it.
- The lifespan's `finally` calls `shutdown()`.
- `serve` runs `uvicorn.Server(...).serve()` under its own `asyncio.run`
  and then calls `shutdown()` again. That second call does nothing if the
  lifespan already ran it.

The MCP session manager's lifespan is not re-run on the forced path. Its
tasks are cancelled when `asyncio.run` ends, and it holds no state that
needs saving.

### D3. One log line for cut-off requests

When the timeout cancels request tasks, each one surfaces as a
`CancelledError` traceback. A logging filter on uvicorn's error logger
replaces them with a single warning naming how many requests were cut
off. Every other error still logs normally.

### D4. The bound, stated

factoryd exits within `shutdown_timeout_s` plus the time to cancel gate
runs. Cancelling a gate run takes at most the supervisor's grace period
(2 s) for its process group, plus bookkeeping. The scenarios use
`REFINERY_SHUTDOWN_TIMEOUT=1` and allow 6 s in total, so the tests stay
fast without being flaky.

## Risks / Trade-offs

- **[A long request is cut off at shutdown]** → The cut happens only after
  the timeout. An agent's tool call that was cut is retried after
  reconnecting. Transitions are atomic in the store, so a request is
  either recorded or not.
- **[It relies on a uvicorn option and on uvicorn's skip-lifespan
  behaviour]** → Both are covered by the subprocess tests, which run the
  real `refinery serve`. An upgrade that changes either one fails those
  tests.
