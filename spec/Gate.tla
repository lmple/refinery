-------------------------------- MODULE Gate --------------------------------
(***************************************************************************)
(* The verify gate of one run: factoryd runs the gate's steps one at a     *)
(* time, each in its own process group, and turns the results into         *)
(* PassGate or FailGate (the run's verify phase in Run.tla).               *)
(*                                                                         *)
(* Environment: steps exit (pass or fail), hang until their timeout, or   *)
(* fail to start; the human cancels the run mid-gate; factoryd crashes.   *)
(* Processes outlive a factoryd crash, so every process group is recorded *)
(* in a registry on disk before its command may run, and recovery kills   *)
(* every registered group. A spawned process waits on a start latch       *)
(* ("parked") until its registry entry exists; if factoryd dies first,    *)
(* the latch's stdin closes and it exits without running the command.     *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
    Steps,      \* gate steps (model values), run in any order, one at a time
    MaxGates    \* TLC-only bound on gate runs (see GateBound)

VARIABLES
    phase,      \* the run's phase: verify, impl, final_review or cancelled
    gate,       \* factoryd memory: "idle" or "running"
    step,       \* factoryd memory: each step's state in the current gate run
    pg,         \* OS: each step's process group
    reg,        \* disk: process registry entry per step's group
    up,         \* factoryd is running
    gates       \* gate runs started so far (for the TLC bound only)

vars == <<phase, gate, step, pg, reg, up, gates>>

Phases     == {"verify", "impl", "final_review", "cancelled"}
StepStates == {"pending", "running", "pass", "fail"}
\* none: never spawned; parked: spawned, waiting on its start latch;
\* running: the command runs; dead: every process of the group is gone.
PgStates   == {"none", "parked", "running", "dead"}

Live(s) == pg[s] \in {"parked", "running"}
Done(s) == step[s] \in {"pass", "fail"}

TypeOK ==
    /\ phase \in Phases
    /\ gate \in {"idle", "running"}
    /\ step \in [Steps -> StepStates]
    /\ pg \in [Steps -> PgStates]
    /\ reg \in [Steps -> BOOLEAN]
    /\ up \in BOOLEAN
    /\ gates \in Nat

Init ==
    /\ phase = "verify"
    /\ gate = "idle"
    /\ step = [s \in Steps |-> "pending"]
    /\ pg = [s \in Steps |-> "none"]
    /\ reg = [s \in Steps |-> FALSE]
    /\ up = TRUE
    /\ gates = 0

---------------------------------------------------------------------------
(* Gate runs (factoryd) *)

StartGate ==
    /\ up /\ phase = "verify" /\ gate = "idle"
    /\ gate' = "running"
    /\ step' = [s \in Steps |-> "pending"]
    /\ gates' = gates + 1
    /\ UNCHANGED <<phase, pg, reg, up>>

\* Steps run one at a time; none is spawned once the run left verify.
CanSpawn(s) ==
    /\ up /\ gate = "running" /\ phase = "verify"
    /\ step[s] = "pending"
    /\ \A t \in Steps : step[t] # "running"
    /\ pg[s] \in {"none", "dead"}

Spawn(s) ==
    /\ CanSpawn(s)
    /\ pg' = [pg EXCEPT ![s] = "parked"]
    /\ step' = [step EXCEPT ![s] = "running"]
    /\ UNCHANGED <<phase, gate, reg, up, gates>>

\* The executable is missing or cannot be started: the step fails (closed).
SpawnFails(s) ==
    /\ CanSpawn(s)
    /\ step' = [step EXCEPT ![s] = "fail"]
    /\ UNCHANGED <<phase, gate, pg, reg, up, gates>>

\* Write the registry entry, then release the latch.
Register(s) ==
    /\ up /\ step[s] = "running" /\ pg[s] = "parked" /\ ~reg[s]
    /\ reg' = [reg EXCEPT ![s] = TRUE]
    /\ UNCHANGED <<phase, gate, step, pg, up, gates>>

Release(s) ==
    /\ up /\ step[s] = "running" /\ pg[s] = "parked" /\ reg[s]
    /\ pg' = [pg EXCEPT ![s] = "running"]
    /\ UNCHANGED <<phase, gate, step, reg, up, gates>>

\* The command exits; the wrapper kills whatever it left behind in its group.
Exit(s) ==
    /\ up /\ step[s] = "running" /\ pg[s] = "running"
    /\ \E ok \in BOOLEAN : step' = [step EXCEPT ![s] = IF ok THEN "pass" ELSE "fail"]
    /\ pg' = [pg EXCEPT ![s] = "dead"]
    /\ UNCHANGED <<phase, gate, reg, up, gates>>

\* The step's time limit expires: kill the group; a timeout is a failure.
Timeout(s) ==
    /\ up /\ step[s] = "running" /\ Live(s)
    /\ pg' = [pg EXCEPT ![s] = "dead"]
    /\ step' = [step EXCEPT ![s] = "fail"]
    /\ UNCHANGED <<phase, gate, reg, up, gates>>

\* The call returns only after removing its registry entry.
Unregister(s) ==
    /\ up /\ reg[s] /\ pg[s] = "dead"
    /\ reg' = [reg EXCEPT ![s] = FALSE]
    /\ UNCHANGED <<phase, gate, step, pg, up, gates>>

\* The run left verify mid-gate (cancelled): kill the running step's group.
Abort ==
    /\ up /\ gate = "running" /\ phase # "verify"
    /\ \E s \in Steps : step[s] = "running"
    /\ pg' = [s \in Steps |-> IF step[s] = "running" /\ Live(s) THEN "dead" ELSE pg[s]]
    /\ step' = [s \in Steps |-> IF step[s] = "running" THEN "fail" ELSE step[s]]
    /\ UNCHANGED <<phase, gate, reg, up, gates>>

\* Every step finished and every entry is gone: record the result, unless
\* the run is no longer in verify, in which case it is discarded.
Finish ==
    /\ up /\ gate = "running"
    /\ \/ \A s \in Steps : Done(s)
       \/ phase # "verify" /\ \A s \in Steps : step[s] # "running"
    /\ \A s \in Steps : ~reg[s]
    /\ phase' = IF phase # "verify" THEN phase
                ELSE IF \A s \in Steps : step[s] = "pass" THEN "final_review"
                ELSE "impl"
    /\ gate' = "idle"
    /\ UNCHANGED <<step, pg, reg, up, gates>>

---------------------------------------------------------------------------
(* The rest of the run (Run.tla) and the environment *)

CompleteImpl ==  \* agent: back to verify after FailGate
    /\ up /\ phase = "impl" /\ gate = "idle"
    /\ phase' = "verify"
    /\ UNCHANGED <<gate, step, pg, reg, up, gates>>

Cancel ==  \* human
    /\ up /\ phase \in {"verify", "impl"}
    /\ phase' = "cancelled"
    /\ UNCHANGED <<gate, step, pg, reg, up, gates>>

\* factoryd dies and loses its memory; processes survive. Only crashes
\* during a gate run are modelled: any other crash loses nothing and equals
\* a stutter, and allowing it everywhere would make the deadlock check
\* vacuous.
Crash ==
    /\ up /\ gate = "running"
    /\ up' = FALSE
    /\ gate' = "idle"
    /\ step' = [s \in Steps |-> "pending"]
    /\ UNCHANGED <<phase, pg, reg, gates>>

\* A latch whose owner is gone sees EOF on stdin and exits without running
\* the command.
LatchExpires(s) ==
    /\ pg[s] = "parked" /\ step[s] # "running"
    /\ pg' = [pg EXCEPT ![s] = "dead"]
    /\ UNCHANGED <<phase, gate, step, reg, up, gates>>

\* An orphaned command can finish on its own while factoryd is down.
OrphanExits(s) ==
    /\ ~up /\ pg[s] = "running"
    /\ pg' = [pg EXCEPT ![s] = "dead"]
    /\ UNCHANGED <<phase, gate, step, reg, up, gates>>

\* Restart: kill every registered group that is still alive, clear the
\* registry.
Recover ==
    /\ ~up
    /\ up' = TRUE
    /\ pg' = [s \in Steps |-> IF reg[s] /\ Live(s) THEN "dead" ELSE pg[s]]
    /\ reg' = [s \in Steps |-> FALSE]
    /\ UNCHANGED <<phase, gate, step, gates>>

\* The run left verify for good: explicit stuttering.
Terminated ==
    /\ up /\ gate = "idle" /\ phase \in {"final_review", "cancelled"}
    /\ UNCHANGED vars

Next ==
    \/ StartGate \/ Abort \/ Finish \/ CompleteImpl \/ Cancel
    \/ Crash \/ Recover \/ Terminated
    \/ \E s \in Steps :
          \/ Spawn(s) \/ SpawnFails(s) \/ Register(s) \/ Release(s)
          \/ Exit(s) \/ Timeout(s) \/ Unregister(s)
          \/ LatchExpires(s) \/ OrphanExits(s)

\* Safety only: no fairness assumptions.
Spec == Init /\ [][Next]_vars

\* State constraint, TLC only: FailGate / CompleteImpl loops are unbounded.
GateBound == gates <= MaxGates

---------------------------------------------------------------------------
(* Invariants *)

\* Every running command can be found again after a crash.
RunningIsRegistered == \A s \in Steps : pg[s] = "running" => reg[s]

\* While factoryd is up, the only command running is the current step's:
\* nothing survives a gate run or a restart.
OnlyCurrentStepRuns ==
    up => \A s \in Steps : pg[s] = "running" => step[s] = "running" /\ gate = "running"

\* Steps run one at a time.
OneStepAtATime ==
    \A s, t \in Steps : (step[s] = "running" /\ step[t] = "running") => s = t

(* Action properties *)

\* PassGate only when every step of the gate run passed.
PassOnlyIfAllStepsPassed ==
    [][(phase = "verify" /\ phase' = "final_review") => \A s \in Steps : step[s] = "pass"]_vars

\* A gate result is only recorded by a finished gate run.
ResultOnlyFromFinishedGate ==
    [][(phase = "verify" /\ phase' \in {"final_review", "impl"})
           => gate = "running" /\ \A s \in Steps : Done(s)]_vars

CancelledIsFinal == [][phase = "cancelled" => phase' = "cancelled"]_vars

---------------------------------------------------------------------------
(* Reachability witnesses, checked by Gate_reach.cfg: each MUST be violated. *)

NeverPassed == phase # "final_review"

NeverFailedThenPassed == ~(phase = "final_review" /\ gates > 1)

NeverCancelledMidStep == ~(phase = "cancelled" /\ \E s \in Steps : pg[s] = "running")

NeverOrphanAfterCrash == up \/ \A s \in Steps : pg[s] # "running"

NeverLatchStranded == up \/ \A s \in Steps : ~(pg[s] = "parked" /\ ~reg[s])

=============================================================================
