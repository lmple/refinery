# Spec Delta

## ADDED Requirements

### Requirement: Approvals record the reviewed commit
`ApproveSpec` and `ApproveTests` SHALL carry the full 40-hex SHA of the
reviewed commit, or be refused. The commit SHALL be recorded in the event and
in the run record as that artifact's approved commit. A `Resolve*` transition
that unfreezes an artifact SHALL clear its approved commit.

#### Scenario: Approval without a commit is refused
- **GIVEN** a run in `spec_review`
- **WHEN** `ApproveSpec` is requested without a commit, or with `abc123`
- **THEN** it is refused and the run stays in `spec_review`

#### Scenario: Approved commits are recorded
- **WHEN** `ApproveSpec` is accepted with commit S, and later `ApproveTests` with commit T
- **THEN** the events carry S and T, and the run record's approved commits are spec = S and tests = T

#### Scenario: Resolving clears unfrozen commits
- **GIVEN** a run with approved commits spec = S and tests = T, in `escalated`
- **WHEN** `ResolveToTests` is accepted
- **THEN** the approved commits are spec = S and no tests commit
