# process-supervision Specification

## Purpose

Defines how Refinery runs every subprocess: in its own process group, within a
time limit, cancellable, and with nothing left running after the call
returns. This holds even when factoryd itself crashes mid-call. The gate
behaviour that depends on it is modelled by `spec/Gate.tla`.

## Requirements

### Requirement: One entry point for subprocesses
Every subprocess Refinery starts (gate steps, TLC, git, and later agents)
SHALL be started through the process supervisor. No other code SHALL start
processes.

#### Scenario: Source tree uses only the supervisor
- **WHEN** the Python sources under `src/refinery/` are scanned
- **THEN** only the supervisor module uses `subprocess`, `asyncio.create_subprocess_*`, `os.system`, `os.spawn*`, `os.exec*` or `os.fork`

### Requirement: Own process group
Each supervised command SHALL run in a new process group, separate from
factoryd's.

#### Scenario: Group differs from the supervisor's
- **WHEN** a command prints its process group id
- **THEN** that id is the command's own pid and differs from factoryd's process group id

### Requirement: Nothing outlives a call
When a supervised call returns or raises, for any outcome, no process of its
process group SHALL still be alive. Processes the command left behind SHALL be
killed.

#### Scenario: Background child is cleaned up
- **WHEN** a command starts a background `sleep 60` and exits 0
- **THEN** the call returns outcome `exited` with code 0, and the background process is no longer alive

### Requirement: Time limit
Each call SHALL have a time limit. When it expires, the supervisor SHALL send
SIGTERM to the group, wait a grace period, then send SIGKILL, and report
outcome `timed_out`.

#### Scenario: Command exceeds its limit
- **WHEN** `sleep 60` runs with a 0.2 s limit
- **THEN** the call returns `timed_out` within the limit plus the grace period, and no process of the group is alive

#### Scenario: Command ignores SIGTERM
- **WHEN** a command that traps and ignores SIGTERM exceeds its limit
- **THEN** it is killed with SIGKILL after the grace period and the call returns `timed_out`

### Requirement: Cancellation
When the task awaiting a call is cancelled, the supervisor SHALL kill the
group (SIGTERM, grace, SIGKILL) before the cancellation propagates.

#### Scenario: Caller cancelled
- **WHEN** the task awaiting `sleep 60` is cancelled
- **THEN** the task ends cancelled, and no process of the group is alive

### Requirement: Call outcomes
A call SHALL report exactly one outcome: `exited` (with exit code),
`signalled` (with signal number), `timed_out`, `cancelled` or
`failed_to_start`. A missing or non-executable program SHALL yield
`failed_to_start` rather than an exception.

#### Scenario: Missing program
- **WHEN** the program `does-not-exist-refinery` is run
- **THEN** the call returns `failed_to_start` and the command never ran

#### Scenario: Non-zero exit
- **WHEN** `sh -c 'exit 3'` is run
- **THEN** the call returns `exited` with code 3

#### Scenario: Killed by a signal
- **WHEN** a command kills itself with SIGKILL
- **THEN** the call returns `signalled` with signal 9

### Requirement: Output captured or streamed
A call SHALL either capture stdout and stderr and return them, or append both
to a given log file. The log SHALL be flushed to disk when the call returns.

#### Scenario: Captured output
- **WHEN** `sh -c 'echo out; echo err >&2'` is run with capture
- **THEN** the result's stdout is "out\n" and stderr is "err\n"

#### Scenario: Streamed output
- **WHEN** the same command is run with log file `step.log`
- **THEN** `step.log` contains both lines and the result carries no captured output

### Requirement: Registry before the command runs
Before a command may start, the supervisor SHALL record its process group in
a registry in factoryd's data directory, identified by group id and leader
start time. The command SHALL NOT start until that entry is durable. The entry
SHALL be removed when the call returns.

#### Scenario: Entry exists while running
- **WHEN** a command is running under the supervisor
- **THEN** the registry holds an entry for its process group, and after the call returns the entry is gone

#### Scenario: Supervisor dies before registering
- **WHEN** the supervisor's process ends after starting a command's process but before writing its registry entry
- **THEN** the command never runs

### Requirement: Reaping after a restart
On startup, the supervisor SHALL kill every registered process group that is
still alive, then clear the registry. A group SHALL only be killed if it is
verifiably the registered one: same leader start time, or leader gone but
group still present.

#### Scenario: Orphan from a crash is killed
- **GIVEN** a registry entry for a still-running `sleep 60` group whose supervisor died
- **WHEN** a new supervisor starts
- **THEN** that group is killed and the registry is empty

#### Scenario: Reused process id is spared
- **GIVEN** a registry entry whose process id now belongs to an unrelated process with a different start time
- **WHEN** a new supervisor starts
- **THEN** the unrelated process is not signalled and the entry is removed
