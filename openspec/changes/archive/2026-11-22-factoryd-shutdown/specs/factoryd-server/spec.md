## MODIFIED Requirements

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

## ADDED Requirements

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
