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
On shutdown, factoryd SHALL cancel its running gate runs, killing their
processes and recording no results. It SHALL then close the run index.

#### Scenario: Shutdown mid-gate
- **GIVEN** a gate run whose `test` step is running
- **WHEN** factoryd shuts down
- **THEN** the step's processes are gone, no `PassGate` or `FailGate` is recorded, and the run is still in `verify`
