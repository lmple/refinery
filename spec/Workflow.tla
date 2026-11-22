------------------------------ MODULE Workflow ------------------------------
(***************************************************************************)
(* The MCP workflow: how human and agent tool calls drive runs, how run-   *)
(* bound agent tokens are issued and stop working, how factoryd starts the *)
(* real gate when a run enters verify (and again after a restart), and how *)
(* an agent's dry-run gate and the real gate are kept apart.               *)
(*                                                                         *)
(* Phases are coarsened from Run.tla: "spec" stands for every agent phase  *)
(* before the tests are approved, "spec_review" for the review gates       *)
(* there, "impl" for the implementation phase. Gate internals are         *)
(* Gate.tla's; here a gate run is pending, running, or idle.              *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
    Runs,        \* run ids (model values)
    MaxCrashes   \* TLC-only bound on factoryd crashes

VARIABLES
    phase,       \* run record: coarsened phase
    token,       \* token store: the run's agent token, "none" or "issued"
    gate,        \* factoryd memory: real gate "idle", "pending" (scheduled) or "running"
    dry,         \* factoryd memory: agent dry-run gate "idle" or "running"
    up,          \* factoryd is running
    crashes      \* crashes so far (for the TLC bound only)

vars == <<phase, token, gate, dry, up, crashes>>

Phases   == {"none", "spec", "spec_review", "impl", "verify", "final_review",
             "archived", "cancelled"}
Terminal == {"archived", "cancelled"}

\* An agent token works only while its run exists and has not ended.
AgentOk(r) == token[r] = "issued" /\ phase[r] \notin Terminal \cup {"none"}

TypeOK ==
    /\ phase \in [Runs -> Phases]
    /\ token \in [Runs -> {"none", "issued"}]
    /\ gate \in [Runs -> {"idle", "pending", "running"}]
    /\ dry \in [Runs -> {"idle", "running"}]
    /\ up \in BOOLEAN
    /\ crashes \in 0..MaxCrashes

Init ==
    /\ phase = [r \in Runs |-> "none"]
    /\ token = [r \in Runs |-> "none"]
    /\ gate = [r \in Runs |-> "idle"]
    /\ dry = [r \in Runs |-> "idle"]
    /\ up = TRUE
    /\ crashes = 0

---------------------------------------------------------------------------
(* Human tool calls (human token) *)

\* start_workflow: create the run and hand out its agent token, once.
StartWorkflow(r) ==
    /\ up /\ phase[r] = "none"
    /\ phase' = [phase EXCEPT ![r] = "spec"]
    /\ token' = [token EXCEPT ![r] = "issued"]
    /\ UNCHANGED <<gate, dry, up, crashes>>

\* Review decisions (review-queue); approval of the final review archives.
Approve(r) ==
    /\ up /\ phase[r] \in {"spec_review", "final_review"}
    /\ phase' = [phase EXCEPT ![r] = IF @ = "spec_review" THEN "impl" ELSE "archived"]
    /\ UNCHANGED <<token, gate, dry, up, crashes>>

Reject(r) ==
    /\ up /\ phase[r] \in {"spec_review", "final_review"}
    /\ phase' = [phase EXCEPT ![r] = IF @ = "spec_review" THEN "spec" ELSE "impl"]
    /\ UNCHANGED <<token, gate, dry, up, crashes>>

Cancel(r) ==
    /\ up /\ phase[r] \notin Terminal \cup {"none"}
    /\ phase' = [phase EXCEPT ![r] = "cancelled"]
    /\ UNCHANGED <<token, gate, dry, up, crashes>>

---------------------------------------------------------------------------
(* Agent tool calls (the run's agent token) *)

\* submit_for_review: completes the current agent phase. From impl it enters
\* verify and schedules the real gate; refused while a dry run is going on.
Submit(r) ==
    /\ up /\ AgentOk(r)
    /\ phase[r] \in {"spec", "impl"}
    /\ dry[r] = "idle"
    /\ phase' = [phase EXCEPT ![r] = IF @ = "spec" THEN "spec_review" ELSE "verify"]
    /\ gate' = [gate EXCEPT ![r] = IF phase[r] = "impl" THEN "pending" ELSE @]
    /\ UNCHANGED <<token, dry, up, crashes>>

\* run_gate: a dry run, only in impl and only when no gate runs for the run.
DryRun(r) ==
    /\ up /\ AgentOk(r)
    /\ phase[r] = "impl"
    /\ dry[r] = "idle" /\ gate[r] = "idle"
    /\ dry' = [dry EXCEPT ![r] = "running"]
    /\ UNCHANGED <<phase, token, gate, up, crashes>>

\* A dry run finishes (pass or fail: no transition either way).
DryFinish(r) ==
    /\ up /\ dry[r] = "running"
    /\ dry' = [dry EXCEPT ![r] = "idle"]
    /\ UNCHANGED <<phase, token, gate, up, crashes>>

---------------------------------------------------------------------------
(* factoryd *)

GateStart(r) ==
    /\ up /\ gate[r] = "pending" /\ phase[r] = "verify"
    /\ gate' = [gate EXCEPT ![r] = "running"]
    /\ UNCHANGED <<phase, token, dry, up, crashes>>

\* PassGate or FailGate.
GateFinish(r) ==
    /\ up /\ gate[r] = "running" /\ phase[r] = "verify"
    /\ \E pass \in BOOLEAN :
           phase' = [phase EXCEPT ![r] = IF pass THEN "final_review" ELSE "impl"]
    /\ gate' = [gate EXCEPT ![r] = "idle"]
    /\ UNCHANGED <<token, dry, up, crashes>>

\* The run left verify (cancelled): drop the scheduled or running gate.
GateAbort(r) ==
    /\ up /\ gate[r] # "idle" /\ phase[r] # "verify"
    /\ gate' = [gate EXCEPT ![r] = "idle"]
    /\ UNCHANGED <<phase, token, dry, up, crashes>>

\* The run left impl (cancelled): kill the dry run.
DryAbort(r) ==
    /\ up /\ dry[r] = "running" /\ phase[r] # "impl"
    /\ dry' = [dry EXCEPT ![r] = "idle"]
    /\ UNCHANGED <<phase, token, gate, up, crashes>>

\* factoryd dies and forgets every scheduled or running gate. Only crashes
\* with something in flight are modelled (others equal a stutter).
Crash ==
    /\ up /\ crashes < MaxCrashes
    /\ \E r \in Runs : gate[r] # "idle" \/ dry[r] # "idle"
    /\ up' = FALSE
    /\ gate' = [r \in Runs |-> "idle"]
    /\ dry' = [r \in Runs |-> "idle"]
    /\ crashes' = crashes + 1
    /\ UNCHANGED <<phase, token>>

\* Restart: every run found in verify gets its gate scheduled again.
Recover ==
    /\ ~up
    /\ up' = TRUE
    /\ gate' = [r \in Runs |-> IF phase[r] = "verify" THEN "pending" ELSE "idle"]
    /\ UNCHANGED <<phase, token, dry, crashes>>

\* Every run finished: explicit stuttering, so the deadlock check stays on.
Done ==
    /\ up /\ \A r \in Runs : phase[r] \in Terminal
    /\ UNCHANGED vars

Next ==
    \/ Crash \/ Recover \/ Done
    \/ \E r \in Runs :
          \/ StartWorkflow(r) \/ Approve(r) \/ Reject(r) \/ Cancel(r)
          \/ Submit(r) \/ DryRun(r) \/ DryFinish(r)
          \/ GateStart(r) \/ GateFinish(r) \/ GateAbort(r) \/ DryAbort(r)

\* Fairness, for the liveness property only: factoryd keeps working through
\* the gates it scheduled and restarts after a crash. These are factoryd's
\* own actions (a scheduler loop and a process supervisor), not the human's
\* or the agent's, which stay unconstrained.
Fairness ==
    /\ WF_vars(Recover)
    /\ \A r \in Runs : WF_vars(GateStart(r)) /\ WF_vars(GateFinish(r)) /\ WF_vars(GateAbort(r))

Spec == Init /\ [][Next]_vars /\ Fairness

---------------------------------------------------------------------------
(* Invariants *)

\* A token exists exactly for runs that were started.
TokenOnlyForStartedRuns == \A r \in Runs : (token[r] = "issued") = (phase[r] # "none")

\* A dry run and the real gate never overlap for a run.
GateAndDryRunExclusive == \A r \in Runs : ~(gate[r] # "idle" /\ dry[r] = "running")

\* Dry runs happen in impl only (or are being killed after a cancel).
DryRunOnlyInImpl == \A r \in Runs : dry[r] = "running" => phase[r] \in {"impl", "cancelled"}

\* The real gate exists only for a run in verify (or being dropped after a cancel).
GateOnlyInVerify == \A r \in Runs : gate[r] # "idle" => phase[r] \in {"verify", "cancelled"}

(* Action properties *)

\* Everything an agent can do needs a working agent token: the moves out of
\* spec or impl into review or verify, and starting a dry run (which moves
\* no phase, so it needs its own clause), only from impl.
AgentNeedsLiveToken ==
    [][\A r \in Runs :
          /\ (phase[r] \in {"spec", "impl"} /\ phase'[r] \in {"spec_review", "verify"})
                 => AgentOk(r)
          /\ (dry[r] = "idle" /\ dry'[r] = "running")
                 => AgentOk(r) /\ phase[r] = "impl"]_vars

(* Liveness *)

\* Every run that enters verify gets a gate result (or is cancelled), even
\* across factoryd crashes: nothing stays in verify for ever.
VerifyResolves == \A r \in Runs : phase[r] = "verify" ~> phase[r] # "verify"

---------------------------------------------------------------------------
(* Reachability witnesses (Workflow_reach.cfg): each MUST be violated. *)

NeverArchived == \A r \in Runs : phase[r] # "archived"

NeverGateRescheduledAfterCrash ==
    ~(crashes > 0 /\ up /\ \E r \in Runs : phase[r] = "verify" /\ gate[r] = "pending")

NeverDryRunKilledByCancel == ~\E r \in Runs : dry[r] = "running" /\ phase[r] = "cancelled"

NeverTwoRunsInVerify == ~\A r \in Runs : phase[r] = "verify"

=============================================================================
