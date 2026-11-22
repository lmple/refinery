# Spec Delta

## ADDED Requirements

### Requirement: TLC trace validation
The production trace validator SHALL export the event log as a TLA+ trace
module and run TLC on it through the process supervisor, against the
`spec/Run.tla` that factoryd implements, within a time limit. The log passes
only if TLC reports `TraceIncomplete` violated. A deadlock, any other TLC
result, an error or a timeout SHALL fail it.

#### Scenario: Valid log passes
- **WHEN** the log of a run that went through the whole workflow is validated
- **THEN** the verdict is pass

#### Scenario: Log that skips a gate fails
- **WHEN** a log whose `ApproveTests` event goes from `tests` to `impl`, skipping `CompleteTests` and the tests review, is validated
- **THEN** the verdict is fail

#### Scenario: TLC unavailable
- **WHEN** Java or `tla2tools.jar` cannot be started
- **THEN** the verdict is fail, with a reason naming the failure to start

#### Scenario: TLC too slow
- **WHEN** TLC does not finish within the time limit
- **THEN** its processes are killed and the verdict is fail
