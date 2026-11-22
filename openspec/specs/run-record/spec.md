# run-record Specification

## Purpose

Defines how a run's state and history are recorded: a run record committed
with the change, an event log kept outside git, the order in which they are
written so crashes never corrupt them, how the log is validated and sealed at
archive, and how factoryd's index is rebuilt from them.

## Requirements

### Requirement: Run record file
Each run SHALL have a run record at `.refinery/runs/<run-id>.json` in its
worktree, intended to be committed on the run's branch. It SHALL contain at
least: schema version, run id, change name, phase, frozen artifacts, the
number of committed transitions (`seq`), and the trace-validation result.

#### Scenario: Record after start
- **WHEN** a run `run-lifecycle.1` is started on change `run-lifecycle`
- **THEN** `.refinery/runs/run-lifecycle.1.json` exists with phase `spec`, seq 1, nothing frozen and trace verdict `none`

#### Scenario: Records of different runs do not collide
- **WHEN** runs `a.1` and `b.1` are both active
- **THEN** each has its own record file and neither run writes the other's

### Requirement: Run record writes are atomic
The run record SHALL be replaced atomically: a reader SHALL see either the
previous complete record or the new complete record, never a partial one.

#### Scenario: Crash during record write
- **WHEN** factoryd is killed while writing a run record
- **THEN** the file on disk is a complete, parseable record of either the previous or the new state

### Requirement: Event log
Each accepted transition SHALL append exactly one event to the run's
`events.jsonl` in factoryd's data directory, outside any git repository. An
event SHALL hold `seq`, the transition name (equal to the `spec/Run.tla`
action name), `from`, `to`, actor role, UTC timestamp, and the transition's
note, reason or gate reference when it has one.

#### Scenario: Event matches the transition
- **WHEN** `ApproveSpec` is accepted as the run's 4th transition
- **THEN** the log's 4th line is a JSON object with seq 4, name `ApproveSpec`, from `spec_review`, to `tests`, actor `human`

#### Scenario: Refused transitions are not logged
- **WHEN** a transition is refused
- **THEN** no event is appended

### Requirement: Event is durable before the record
For each transition the system SHALL append and fsync the event before
rewriting the run record. If the event cannot be written, the transition
SHALL be refused and the run record left unchanged (fail closed): this log is
part of the run record, not telemetry.

#### Scenario: Log write fails
- **GIVEN** the event log cannot be written (for example, the disk is full)
- **WHEN** a transition is requested
- **THEN** it is refused with an error, and the run record and phase are unchanged

### Requirement: Crash recovery
On startup, for every run, the system SHALL drop events whose `seq` is greater
than the run record's `seq`, so the event log holds exactly the committed
transitions.

#### Scenario: Crash between event and record
- **GIVEN** event 5 was appended but factoryd died before rewriting the record (record seq 4)
- **WHEN** factoryd starts
- **THEN** the log holds events 1–4 and the run is in the phase recorded by event 4

### Requirement: Trace validation at archive
`Archive` SHALL validate the complete event log, its own event included,
against `spec/Run.tla`. On a pass, the run record SHALL store verdict `pass`,
the event count and the sha256 of the log, and the run becomes `archived`. On
a failure, the `Archive` event SHALL be withdrawn, the record SHALL store
verdict `fail`, and the run SHALL stay in `archive`.

#### Scenario: Valid trace archives the run
- **GIVEN** a run in `archive` whose log is a valid behaviour of `spec/Run.tla`
- **WHEN** `Archive` runs
- **THEN** the run is `archived` and its record holds verdict `pass`, the event count and the log's sha256

#### Scenario: Invalid trace blocks archive
- **GIVEN** a run in `archive` whose log is not a valid behaviour of `spec/Run.tla`
- **WHEN** `Archive` runs
- **THEN** the run stays in `archive`, its record holds verdict `fail`, and the log does not contain an `Archive` event

#### Scenario: Retry after a fix
- **GIVEN** a run in `archive` with verdict `fail`
- **WHEN** the human requests `RetryArchive`
- **THEN** the verdict is reset to `none` and `Archive` may run again

#### Scenario: Validator unavailable
- **WHEN** `Archive` runs and the validator cannot produce a verdict (error or timeout)
- **THEN** this counts as verdict `fail`

### Requirement: Sealed event log
After a run is archived, its event log SHALL NOT be modified, and the system
SHALL be able to verify the log against the sha256 and event count in the run
record.

#### Scenario: Tampered log is detected
- **GIVEN** an archived run whose event log was edited afterwards
- **WHEN** the log is verified
- **THEN** verification reports a hash mismatch

### Requirement: Index is rebuildable
factoryd's SQLite index of runs and events SHALL be a cache: rebuilding it
from the run records and event logs SHALL produce the same runs, phases and
events as before.

#### Scenario: Rebuild after deleting the database
- **GIVEN** an index with several runs in different phases
- **WHEN** the database file is deleted and the index is rebuilt
- **THEN** it contains the same runs with the same phases, frozen sets and events

#### Scenario: Event log missing
- **GIVEN** a run record whose event log no longer exists
- **WHEN** the index is rebuilt
- **THEN** the run is restored from its record and reported as missing its event log
