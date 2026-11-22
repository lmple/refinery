------------------------------- MODULE Review -------------------------------
(***************************************************************************)
(* Review gates through factoryd for one run, from the agent's submission  *)
(* to archived. It covers:                                                 *)
(*   - the worktree: uncommitted agent work, factoryd's run record;        *)
(*   - what was published for the gate, and what origin has;               *)
(*   - the PR;                                                             *)
(*   - the human's decisions;                                              *)
(*   - the archive sequence (OpenSpec archive, trace validation, final     *)
(*     push and PR ready);                                                 *)
(*   - factoryd crashes.                                                   *)
(*                                                                         *)
(* Commits are abstract: atPublished and atPushed say whether HEAD still   *)
(* is the commit published for the gate, and the one origin has. That     *)
(* keeps the state space finite, so liveness is checked exactly.           *)
(* Phases are coarsened from Run.tla: "work" stands for the agent phases,  *)
(* "review" for spec_review and tests_review, and reaching final_review    *)
(* stands for CompleteImpl followed by PassGate.                           *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxCrashes   \* TLC-only bound on factoryd crashes

VARIABLES
    phase,          \* run record: work, review, final_review, archive, archived
    dirty,          \* worktree: uncommitted agent work (outside .refinery/)
    recordDirty,    \* worktree: run record changed since its last commit
    atPublished,    \* HEAD is the commit published for the current gate
    atPushed,       \* HEAD is what origin has for the branch
    pr,             \* "none", "draft" or "ready"
    pubPending,     \* factoryd memory: this gate still has to be published
    specsArchived,  \* worktree: the OpenSpec change was archived and committed
    archiveError,   \* publish.json: an archive step failed; waits for the human
    verdict,        \* run record: trace verdict "none", "pass" or "fail"
    lastDecision,   \* history: the human's last decision, and whether it was sound
    up,             \* factoryd is running
    crashes

vars == <<phase, dirty, recordDirty, atPublished, atPushed, pr, pubPending,
          specsArchived, archiveError, verdict, lastDecision, up, crashes>>

Phases     == {"work", "review", "final_review", "archive", "archived"}
Gates      == {"review", "final_review"}
\* Archived, and nothing left to commit, push or mark ready.
Finished   == phase = "archived" /\ ~recordDirty /\ atPushed /\ pr = "ready"

TypeOK ==
    /\ phase \in Phases
    /\ dirty \in BOOLEAN /\ recordDirty \in BOOLEAN
    /\ atPublished \in BOOLEAN /\ atPushed \in BOOLEAN
    /\ pr \in {"none", "draft", "ready"}
    /\ pubPending \in BOOLEAN /\ specsArchived \in BOOLEAN /\ archiveError \in BOOLEAN
    /\ verdict \in {"none", "pass", "fail"}
    /\ lastDecision \in {"none", "approved", "rejected"}
    /\ up \in BOOLEAN /\ crashes \in 0..MaxCrashes

Init ==
    /\ phase = "work" /\ dirty = FALSE /\ recordDirty = FALSE
    /\ atPublished = FALSE /\ atPushed = FALSE /\ pr = "none"
    /\ pubPending = FALSE /\ specsArchived = FALSE /\ archiveError = FALSE
    /\ verdict = "none" /\ lastDecision = "none"
    /\ up = TRUE /\ crashes = 0

---------------------------------------------------------------------------
(* The agent (and its environment: it may edit or commit at any time) *)

Edit ==
    /\ phase # "archived" /\ ~dirty
    /\ dirty' = TRUE
    /\ UNCHANGED <<phase, recordDirty, atPublished, atPushed, pr, pubPending,
                   specsArchived, archiveError, verdict, lastDecision, up, crashes>>

Commit ==
    /\ dirty
    /\ dirty' = FALSE /\ atPublished' = FALSE /\ atPushed' = FALSE
    /\ UNCHANGED <<phase, recordDirty, pr, pubPending, specsArchived,
                   archiveError, verdict, lastDecision, up, crashes>>

\* submit_for_review into a review gate: only with everything committed.
Submit(target) ==
    /\ up /\ phase = "work" /\ ~dirty
    /\ phase' = target
    /\ recordDirty' = TRUE /\ pubPending' = TRUE
    /\ UNCHANGED <<dirty, atPublished, atPushed, pr, specsArchived,
                   archiveError, verdict, lastDecision, up, crashes>>

---------------------------------------------------------------------------
(* factoryd publishing a gate (fails open) *)

\* Commit the changed run record: a new HEAD.
PublishRecord ==
    /\ up /\ pubPending /\ recordDirty
    /\ recordDirty' = FALSE /\ atPublished' = FALSE /\ atPushed' = FALSE
    /\ UNCHANGED <<phase, dirty, pr, pubPending, specsArchived, archiveError,
                   verdict, lastDecision, up, crashes>>

\* Push, open the draft PR if there is none, and record HEAD as published.
PublishPush ==
    /\ up /\ pubPending /\ ~recordDirty
    /\ atPushed' = TRUE /\ atPublished' = TRUE /\ pubPending' = FALSE
    /\ pr' = IF pr = "none" THEN "draft" ELSE pr
    /\ UNCHANGED <<phase, dirty, recordDirty, specsArchived, archiveError,
                   verdict, lastDecision, up, crashes>>

\* The push (or the PR) fails: the error is shown, and the gate counts as
\* published at the local HEAD (the human reviews locally).
PushFails ==
    /\ up /\ pubPending /\ ~recordDirty
    /\ atPublished' = TRUE /\ pubPending' = FALSE
    /\ UNCHANGED <<phase, dirty, recordDirty, atPushed, pr, specsArchived,
                   archiveError, verdict, lastDecision, up, crashes>>

---------------------------------------------------------------------------
(* The human *)

\* An approval is of the published commit, with nothing uncommitted.
CanApprove == up /\ ~pubPending /\ ~dirty /\ atPublished

Approve ==
    /\ phase = "review" /\ CanApprove
    /\ phase' = "work" /\ recordDirty' = TRUE /\ lastDecision' = "approved"
    /\ UNCHANGED <<dirty, atPublished, atPushed, pr, pubPending, specsArchived,
                   archiveError, verdict, up, crashes>>

Reject ==
    /\ up /\ phase \in Gates /\ ~pubPending
    /\ phase' = "work" /\ recordDirty' = TRUE /\ lastDecision' = "rejected"
    /\ UNCHANGED <<dirty, atPublished, atPushed, pr, pubPending, specsArchived,
                   archiveError, verdict, up, crashes>>

ApproveFinal ==
    /\ phase = "final_review" /\ CanApprove
    /\ phase' = "archive" /\ recordDirty' = TRUE /\ lastDecision' = "approved"
    /\ UNCHANGED <<dirty, atPublished, atPushed, pr, pubPending, specsArchived,
                   archiveError, verdict, up, crashes>>

\* retry-archive: after a failed step (archiveError) or a failed trace
\* (verdict fail, which is RetryArchive in Run.tla).
RetryArchive ==
    /\ up /\ phase = "archive" /\ (archiveError \/ verdict = "fail")
    /\ archiveError' = FALSE
    /\ verdict' = IF verdict = "fail" THEN "none" ELSE verdict
    /\ UNCHANGED <<phase, dirty, recordDirty, atPublished, atPushed, pr,
                   pubPending, specsArchived, lastDecision, up, crashes>>

---------------------------------------------------------------------------
(* factoryd: the archive sequence (each step skipped once done) *)

Proceeding == up /\ phase = "archive" /\ ~archiveError /\ verdict # "fail"

\* openspec archive <change> --yes, then commit: a new HEAD.
ArchiveSpecs ==
    /\ Proceeding /\ ~specsArchived
    /\ specsArchived' = TRUE /\ atPushed' = FALSE /\ atPublished' = FALSE
    /\ UNCHANGED <<phase, dirty, recordDirty, pr, pubPending, archiveError,
                   verdict, lastDecision, up, crashes>>

ArchiveSpecsFails ==
    /\ Proceeding /\ ~specsArchived
    /\ archiveError' = TRUE
    /\ UNCHANGED <<phase, dirty, recordDirty, atPublished, atPushed, pr,
                   pubPending, specsArchived, verdict, lastDecision, up, crashes>>

\* Archive with TLC: pass archives the run, fail stays in archive.
ValidateTrace ==
    /\ Proceeding /\ specsArchived /\ verdict = "none"
    /\ \E pass \in BOOLEAN :
          /\ verdict' = IF pass THEN "pass" ELSE "fail"
          /\ phase' = IF pass THEN "archived" ELSE phase
          /\ recordDirty' = TRUE
    /\ UNCHANGED <<dirty, atPublished, atPushed, pr, pubPending, specsArchived,
                   archiveError, lastDecision, up, crashes>>

\* Then: commit the final record, push, mark the PR ready.
FinishRecord ==
    /\ up /\ phase = "archived" /\ recordDirty
    /\ recordDirty' = FALSE /\ atPushed' = FALSE /\ atPublished' = FALSE
    /\ UNCHANGED <<phase, dirty, pr, pubPending, specsArchived, archiveError,
                   verdict, lastDecision, up, crashes>>

FinishPush ==
    /\ up /\ phase = "archived" /\ ~recordDirty /\ ~atPushed
    /\ atPushed' = TRUE
    /\ UNCHANGED <<phase, dirty, recordDirty, atPublished, pr, pubPending,
                   specsArchived, archiveError, verdict, lastDecision, up, crashes>>

FinishReady ==
    /\ up /\ phase = "archived" /\ ~recordDirty /\ atPushed /\ pr # "ready"
    /\ pr' = "ready"
    /\ UNCHANGED <<phase, dirty, recordDirty, atPublished, atPushed, pubPending,
                   specsArchived, archiveError, verdict, lastDecision, up, crashes>>

---------------------------------------------------------------------------
(* Environment: factoryd crashes and restarts *)

\* Only crashes with work in flight are modelled (others equal a stutter).
Crash ==
    /\ up /\ crashes < MaxCrashes
    /\ pubPending \/ phase = "archive" \/ (phase = "archived" /\ ~Finished)
    /\ up' = FALSE /\ pubPending' = FALSE /\ crashes' = crashes + 1
    /\ UNCHANGED <<phase, dirty, recordDirty, atPublished, atPushed, pr,
                   specsArchived, archiveError, verdict, lastDecision>>

\* Restart: a gate not yet published (its record uncommitted, or HEAD not
\* the published commit) is published again; the archive sequence resumes
\* from its persisted state by itself.
Recover ==
    /\ ~up
    /\ up' = TRUE
    /\ pubPending' = (phase \in Gates /\ (recordDirty \/ ~atPublished))
    /\ UNCHANGED <<phase, dirty, recordDirty, atPublished, atPushed, pr,
                   specsArchived, archiveError, verdict, lastDecision, crashes>>

Done == up /\ Finished /\ UNCHANGED vars

Next ==
    \/ Edit \/ Commit \/ Submit("review") \/ Submit("final_review")
    \/ PublishRecord \/ PublishPush \/ PushFails
    \/ Approve \/ Reject \/ ApproveFinal \/ RetryArchive
    \/ ArchiveSpecs \/ ArchiveSpecsFails \/ ValidateTrace
    \/ FinishRecord \/ FinishPush \/ FinishReady
    \/ Crash \/ Recover \/ Done

\* Fairness, for liveness only: factoryd keeps working on what it owes
\* (publishing, the archive sequence, restarting). A push or an archive step
\* may fail, but factoryd attempts it. Human and agent actions stay
\* unconstrained.
Fairness ==
    /\ WF_vars(PublishRecord) /\ WF_vars(PublishPush \/ PushFails)
    /\ WF_vars(ArchiveSpecs \/ ArchiveSpecsFails) /\ WF_vars(ValidateTrace)
    /\ WF_vars(FinishRecord) /\ WF_vars(FinishPush) /\ WF_vars(FinishReady)
    /\ WF_vars(Recover)

Spec == Init /\ [][Next]_vars /\ Fairness

---------------------------------------------------------------------------
(* Invariants *)

\* Nothing is ever archived without the OpenSpec change archived and a
\* passing trace.
ArchivedOnlyAfterEverything == phase = "archived" => specsArchived /\ verdict = "pass"

\* The PR is marked ready only once the run is archived.
ReadyOnlyWhenArchived == pr = "ready" => phase = "archived"

\* A gate that was published and pushed has a PR to review it on.
PushedGateHasAPR == (phase \in Gates /\ ~pubPending /\ atPushed) => pr # "none"

(* Action properties *)

\* Approvals (and final approval) only of the published commit, with
\* nothing uncommitted: the human approves what they saw. An approval step
\* is identified by its phase change: review -> work with the decision
\* becoming "approved", or final_review -> archive (only ApproveFinal).
ApprovalsAreOfPublishedCommits ==
    [][(\/ phase = "review" /\ phase' = "work" /\ lastDecision' = "approved"
        \/ phase = "final_review" /\ phase' = "archive")
           => (~dirty /\ atPublished)]_vars

\* Submissions into a review only with everything committed.
ReviewsAreOfCommits ==
    [][(phase = "work" /\ phase' \in Gates) => ~dirty]_vars

(* Liveness *)

\* Every gate gets published (pushed, or failed visibly), across crashes.
GatesGetPublished == pubPending ~> ~pubPending

\* A gate whose run record is still uncommitted (its publishing was owed
\* when factoryd crashed, and pubPending is memory, lost) gets it committed,
\* unless the run leaves the gate first.
GateRecordsGetCommitted ==
    (phase \in Gates /\ recordDirty) ~> (~recordDirty \/ phase \notin Gates)

\* An approved run finishes: archived, pushed, PR ready. The alternative is
\* that it waits for the human after a failure.
ArchiveCompletes == phase = "archive" ~> (Finished \/ archiveError \/ verdict = "fail")

---------------------------------------------------------------------------
(* Reachability witnesses (Review_reach.cfg): each MUST be violated. *)

NeverFinished == ~Finished
NeverApprovedAfterFailedPush == ~(lastDecision = "approved" /\ ~atPushed /\ phase = "work" /\ pr = "none")
NeverResumedAfterCrash == ~(crashes > 0 /\ Finished)
NeverRetriedAfterTraceFailure == ~(verdict = "fail" /\ archiveError = FALSE /\ phase = "archive" /\ specsArchived)
NeverApprovalBlockedByNewCommit == ~(phase = "review" /\ ~pubPending /\ ~dirty /\ ~atPublished)

=============================================================================
