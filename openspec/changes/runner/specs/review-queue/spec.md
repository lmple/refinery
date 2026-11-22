# Spec Delta

## MODIFIED Requirements

### Requirement: The review queue
The review queue SHALL list every unfinished run whose next move is the
human's, with run id, change, phase and how long it has waited. These are
runs in:
- `spec_review`, `tests_review` or `final_review`;
- `escalated`;
- `archive` after a failed archive;
- any phase, when the run is stuck (see agent-jobs). Such runs are shown as
  stuck, waiting since their last finished job.

#### Scenario: Queue contents
- **GIVEN** run `a.1` in `spec_review`, run `b.1` in `impl` and run `c.1` in `escalated`
- **WHEN** the human lists the queue
- **THEN** it holds `a.1` and `c.1` and not `b.1`

#### Scenario: A stuck run is in the queue
- **GIVEN** run `b.1` in `impl` is stuck
- **WHEN** the human lists the queue
- **THEN** it holds `b.1`, shown as stuck
