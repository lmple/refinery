# factoryd-server Specification

## Purpose

Defines how factoryd runs as a server: how it is started and configured,
where it listens, and the order of its startup and shutdown, so that it
recovers cleanly from a crash.

## Requirements

### Requirement: Serving
`refinery serve` SHALL run factoryd. Its options, each with an environment
variable, are:

| Option | Environment variable | Default |
|---|---|---|
| data directory | `REFINERY_DATA_DIR` | `$XDG_DATA_HOME/refinery` |
| roots (repeatable) | `REFINERY_ROOTS` | — |
| host | `REFINERY_HOST` | `127.0.0.1` |
| port | `REFINERY_PORT` | 8740 |
| `tla2tools.jar` | `REFINERY_TLA_JAR` | — |
| `Run.tla` | `REFINERY_RUN_SPEC` | — |

The MCP endpoint SHALL be served at `/mcp`.

#### Scenario: Default address
- **WHEN** `refinery serve` starts with no host or port given
- **THEN** it listens on 127.0.0.1:8740 only

### Requirement: Startup order
On startup, before accepting any request, factoryd SHALL do three things in
this order:
1. Reap registered process groups.
2. Recover and index runs from the configured roots and the data directory.
3. Schedule the gate of every run in `verify`.

#### Scenario: Restart with an orphan and a run in verify
- **GIVEN** a registered process group left by a crash, and run `demo.1` in `verify` with no gate result
- **WHEN** factoryd starts
- **THEN** the orphan group is killed before `demo.1`'s new gate run starts, and that gate run records a result

### Requirement: Shutdown
On every shutdown path, factoryd SHALL cancel its running gate runs before
the process exits, killing their processes and recording no results. It
SHALL then close the run index. That includes a second SIGINT sent to force
the exit.

#### Scenario: Shutdown mid-gate
- **GIVEN** a gate run whose `test` step is running
- **WHEN** factoryd shuts down
- **THEN** the step's processes are gone, no `PassGate` or `FailGate` is recorded, and the run is still in `verify`

#### Scenario: Forced exit still runs factoryd's shutdown
- **GIVEN** factoryd with a shutdown timeout of 1 second, a gate run whose step is running, and a client holding a connection open
- **WHEN** factoryd receives SIGINT twice
- **THEN** the process exits within 6 seconds, the step's processes are gone, no `PassGate` or `FailGate` is recorded, and the run is still in `verify`

### Requirement: Shutdown timeout
factoryd's shutdown timeout SHALL be read from `REFINERY_SHUTDOWN_TIMEOUT`,
in seconds, and SHALL default to 5 seconds.

#### Scenario: Setting the timeout
- **WHEN** `REFINERY_SHUTDOWN_TIMEOUT` is `1`
- **THEN** the shutdown timeout is 1 second, and with the variable unset it is 5 seconds

### Requirement: Bounded shutdown
On SIGINT or SIGTERM, factoryd SHALL stop accepting connections and give
open requests up to the shutdown timeout to complete. It SHALL then close
every connection still open and exit, whatever its clients do, however
many signals arrive.

#### Scenario: A client holds a connection open
- **GIVEN** factoryd with a shutdown timeout of 1 second, and a client that has sent part of a request and keeps the connection open
- **WHEN** factoryd receives one SIGINT, or one SIGTERM
- **THEN** the process exits within 6 seconds

### Requirement: Cut-off requests are logged once
Requests cut off by the shutdown timeout SHALL be reported in one log line
giving their number, with no traceback.

#### Scenario: One request cut off
- **GIVEN** a client holding a connection open past the shutdown timeout
- **WHEN** factoryd shuts down
- **THEN** its log has one line saying one request was cut off, and no traceback
