# Spec Delta

## ADDED Requirements

### Requirement: The agent sees the human's last decision
`get_task` SHALL include `last_decision`: the action, note and time of the
run's most recent human decision (an approval, rejection, resolution or
cancellation), or null if there is none. It SHALL also include `pr`, the run's
PR URL or null.

#### Scenario: After a rejection
- **GIVEN** the human rejected run `demo.1`'s spec with note "scenario 2 has no THEN"
- **WHEN** the agent calls `get_task`
- **THEN** `last_decision` is `RejectSpec` with that note, and the phase is `spec`

#### Scenario: Before any decision
- **WHEN** the agent of a new run calls `get_task`
- **THEN** `last_decision` is null
