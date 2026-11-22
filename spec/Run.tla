-------------------------------- MODULE Run --------------------------------
(***************************************************************************)
(* Lifecycle of a Refinery run: the phases of the AGENTS.md workflow, the  *)
(* three human review gates, failure escalation, cancellation, and the     *)
(* run record that makes every transition durable.                         *)
(*                                                                         *)
(* A transition is two writes: its event is appended (and fsynced) to the  *)
(* run's events.jsonl, then run.json is atomically rewritten. factoryd can *)
(* crash between the two; recovery drops events past run.json's seq, so    *)
(* the committed event log always replays to the committed phase. The      *)
(* SQLite index is a cache rebuilt from these files and is not modelled.   *)
(*                                                                         *)
(* Implementation event names are exactly the action names in the          *)
(* "Transitions" section, so an events.jsonl can be validated against      *)
(* this module.                                                            *)
(***************************************************************************)
EXTENDS Naturals, Sequences, TLC

CONSTANTS
    Runs,       \* run ids (model values)
    Changes,    \* OpenSpec change names (model values)
    NoChange,   \* model value: run.json not written yet
    MaxLog      \* TLC-only bound on events per run (see LogBound)

VARIABLES
    phase,      \* run.json: current phase
    change,     \* run.json: the change the run works on
    frozen,     \* run.json: artifacts frozen by an approved review gate
    seq,        \* run.json: number of committed transitions
    trace,      \* run.json: trace-validation verdict
    log,        \* events.jsonl: events written so far
    pending,    \* factoryd memory: <<next record>> whose event is written
                \* but whose run.json is not yet; <<>> when idle
    up          \* factoryd is running

vars == <<phase, change, frozen, seq, trace, log, pending, up>>

---------------------------------------------------------------------------
(* Phases and artifacts *)

AgentPhases  == {"spec", "model", "tests", "impl"}
ReviewPhases == {"spec_review", "tests_review", "final_review"}
ServerPhases == {"verify", "archive"}
Terminal     == {"archived", "cancelled"}
Phases       == AgentPhases \cup ReviewPhases \cup ServerPhases
                    \cup {"escalated"} \cup Terminal \cup {"none"}

\* "spec": spec/, openspec/specs/ and the change's spec deltas.
\* "tests": the tests approved at the tests review gate.
Artifacts == {"spec", "tests"}
Verdicts  == {"none", "pass", "fail"}

Acts == {"StartRun", "CompleteSpec", "CompleteModel", "SkipModel",
         "ApproveSpec", "RejectSpec", "CompleteTests", "ApproveTests",
         "RejectTests", "CompleteImpl", "PassGate", "FailGate", "Escalate",
         "ResolveToSpec", "ResolveToTests", "ResolveToImpl", "ApproveFinal",
         "RejectFinal", "Archive", "RetryArchive", "Cancel"}

Entry   == [act : Acts, from : Phases, to : Phases]
Next1   == [entry : Entry, change : Changes \cup {NoChange},
            frozen : SUBSET Artifacts, trace : Verdicts, verdict : Verdicts]

Active(r) == phase[r] \notin Terminal \cup {"none"}

TypeOK ==
    /\ phase   \in [Runs -> Phases]
    /\ change  \in [Runs -> Changes \cup {NoChange}]
    /\ frozen  \in [Runs -> SUBSET Artifacts]
    /\ seq     \in [Runs -> Nat]
    /\ trace   \in [Runs -> Verdicts]
    /\ log     \in [Runs -> Seq(Entry)]
    /\ \A r \in Runs : pending[r] = <<>>
                       \/ (Len(pending[r]) = 1 /\ pending[r][1] \in Next1)
    /\ up      \in BOOLEAN

Init ==
    /\ phase   = [r \in Runs |-> "none"]
    /\ change  = [r \in Runs |-> NoChange]
    /\ frozen  = [r \in Runs |-> {}]
    /\ seq     = [r \in Runs |-> 0]
    /\ trace   = [r \in Runs |-> "none"]
    /\ log     = [r \in Runs |-> <<>>]
    /\ pending = [r \in Runs |-> <<>>]
    /\ up      = TRUE

---------------------------------------------------------------------------
(* Two-step write. Begin appends the event and holds the next run.json in *)
(* memory; CommitRecord writes it. Every transition goes through Begin.    *)

Begin(r, act, to, ch, fr, tr) ==
    LET e == [act |-> act, from |-> phase[r], to |-> to]
    IN  /\ up
        /\ pending[r] = <<>>
        /\ log'     = [log EXCEPT ![r] = Append(@, e)]
        /\ pending' = [pending EXCEPT ![r] =
                         <<[entry |-> e, change |-> ch, frozen |-> fr,
                            trace |-> tr, verdict |-> "none"]>>]
        /\ UNCHANGED <<phase, change, frozen, seq, trace, up>>

\* A transition that only changes the phase.
Move(r, act, to) == Begin(r, act, to, change[r], frozen[r], trace[r])

\* r already owns change c, or is about to (its StartRun is in flight).
Claims(r, c) ==
    \/ change[r] = c /\ Active(r)
    \/ /\ pending[r] # <<>>
       /\ pending[r][1].change = c
       /\ pending[r][1].entry.to \notin Terminal

---------------------------------------------------------------------------
(* Transitions. Actor in brackets: H = human, A = agent, S = factoryd.     *)

\* [H] Start a run on a change that has no active run.
StartRun(r, c) ==
    /\ phase[r] = "none"
    /\ \A r2 \in Runs \ {r} : ~Claims(r2, c)
    /\ Begin(r, "StartRun", "spec", c, {}, "none")

CompleteSpec(r)  == phase[r] = "spec"  /\ Move(r, "CompleteSpec", "model")  \* [A]
CompleteModel(r) == phase[r] = "model" /\ Move(r, "CompleteModel", "spec_review") \* [A]
\* [A] The change does not affect behaviour, so no TLA+ update (AGENTS.md).
SkipModel(r)     == phase[r] = "model" /\ Move(r, "SkipModel", "spec_review")

\* [H] Spec review gate. Approval freezes the spec for the rest of the run.
ApproveSpec(r) ==
    /\ phase[r] = "spec_review"
    /\ Begin(r, "ApproveSpec", "tests", change[r], {"spec"}, trace[r])
RejectSpec(r) == phase[r] = "spec_review" /\ Move(r, "RejectSpec", "spec")

CompleteTests(r) == phase[r] = "tests" /\ Move(r, "CompleteTests", "tests_review") \* [A]

\* [H] Tests review gate. Approval freezes the tests too.
ApproveTests(r) ==
    /\ phase[r] = "tests_review"
    /\ Begin(r, "ApproveTests", "impl", change[r], Artifacts, trace[r])
RejectTests(r) == phase[r] = "tests_review" /\ Move(r, "RejectTests", "tests")

CompleteImpl(r) == phase[r] = "impl"   /\ Move(r, "CompleteImpl", "verify")   \* [A]
PassGate(r)     == phase[r] = "verify" /\ Move(r, "PassGate", "final_review") \* [S]
FailGate(r)     == phase[r] = "verify" /\ Move(r, "FailGate", "impl")         \* [S]

\* [A] Failure triage found a suspected spec or test bug: back to the human.
Escalate(r) == phase[r] \in {"tests", "impl"} /\ Move(r, "Escalate", "escalated")

\* [H] Resolve an escalation. Only these two actions ever unfreeze anything.
ResolveToSpec(r) ==
    /\ phase[r] = "escalated"
    /\ Begin(r, "ResolveToSpec", "spec", change[r], {}, trace[r])
ResolveToTests(r) ==
    /\ phase[r] = "escalated"
    /\ Begin(r, "ResolveToTests", "tests", change[r], {"spec"}, trace[r])
\* [H] It was a code bug after all; only possible once tests are frozen.
ResolveToImpl(r) ==
    /\ phase[r] = "escalated"
    /\ "tests" \in frozen[r]
    /\ Move(r, "ResolveToImpl", "impl")

\* [H] Final review gate.
ApproveFinal(r) == phase[r] = "final_review" /\ Move(r, "ApproveFinal", "archive")
RejectFinal(r)  == phase[r] = "final_review" /\ Move(r, "RejectFinal", "impl")

\* [S] Archive. Its run.json (phase archived, with the verdict) is only
\* written if trace validation of the complete log, Archive event included,
\* passes; CommitRecord takes the trace from that verdict.
Archive(r) ==
    /\ phase[r] = "archive"
    /\ trace[r] = "none"
    /\ Begin(r, "Archive", "archived", change[r], frozen[r], trace[r])

\* [H] Retry archiving after a failed trace validation (a Refinery bug,
\* fixed outside this run).
RetryArchive(r) ==
    /\ phase[r] = "archive"
    /\ trace[r] = "fail"
    /\ Begin(r, "RetryArchive", "archive", change[r], frozen[r], "none")

\* [H] Cancel any run that has not finished.
Cancel(r) == Active(r) /\ Move(r, "Cancel", "cancelled")

---------------------------------------------------------------------------
(* factoryd internals: not events. *)

\* Write run.json for the in-flight transition.
CommitRecord(r) ==
    /\ up
    /\ pending[r] # <<>>
    /\ LET p == pending[r][1]
       IN  /\ p.entry.act = "Archive" => p.verdict = "pass"
           /\ phase'  = [phase  EXCEPT ![r] = p.entry.to]
           /\ change' = [change EXCEPT ![r] = p.change]
           /\ frozen' = [frozen EXCEPT ![r] = p.frozen]
           \* Archive records the verdict actually computed, not an assumed one.
           /\ trace'  = [trace  EXCEPT ![r] =
                           IF p.entry.act = "Archive" THEN p.verdict ELSE p.trace]
           /\ seq'    = [seq    EXCEPT ![r] = @ + 1]
           /\ pending' = [pending EXCEPT ![r] = <<>>]
           /\ UNCHANGED <<log, up>>

\* Validate the log ending in the in-flight Archive event. The verdict is
\* nondeterministic here: it checks the implementation, which this module
\* cannot see.
CheckTrace(r) ==
    /\ up
    /\ pending[r] # <<>>
    /\ pending[r][1].entry.act = "Archive"
    /\ pending[r][1].verdict = "none"
    /\ \E v \in {"pass", "fail"} :
           pending' = [pending EXCEPT ![r][1].verdict = v]
    /\ UNCHANGED <<phase, change, frozen, seq, trace, log, up>>

\* Trace validation failed: withdraw the Archive event, record the verdict.
AbortArchive(r) ==
    /\ up
    /\ pending[r] # <<>>
    /\ pending[r][1].entry.act = "Archive"
    /\ pending[r][1].verdict = "fail"
    /\ log'     = [log EXCEPT ![r] = SubSeq(@, 1, seq[r])]
    /\ trace'   = [trace EXCEPT ![r] = "fail"]
    /\ pending' = [pending EXCEPT ![r] = <<>>]
    /\ UNCHANGED <<phase, change, frozen, seq, up>>

---------------------------------------------------------------------------
(* Environment *)

\* factoryd dies and loses its memory. Only crashes with a transition in
\* flight are modelled: any other crash loses nothing and equals a stutter,
\* and allowing it everywhere would make the deadlock check vacuous.
Crash ==
    /\ up
    /\ \E r \in Runs : pending[r] # <<>>
    /\ up'      = FALSE
    /\ pending' = [r \in Runs |-> <<>>]
    /\ UNCHANGED <<phase, change, frozen, seq, trace, log>>

\* Restart: run.json is the truth; drop events it has not committed.
Recover ==
    /\ ~up
    /\ up'  = TRUE
    /\ log' = [r \in Runs |-> SubSeq(log[r], 1, seq[r])]
    /\ UNCHANGED <<phase, change, frozen, seq, trace, pending>>

\* Every run finished: explicit stuttering, so TLC's deadlock check stays on.
Done ==
    /\ up
    /\ \A r \in Runs : phase[r] \in Terminal
    /\ UNCHANGED vars

RunStep(r) ==
    \/ CompleteSpec(r) \/ CompleteModel(r) \/ SkipModel(r)
    \/ ApproveSpec(r) \/ RejectSpec(r)
    \/ CompleteTests(r) \/ ApproveTests(r) \/ RejectTests(r)
    \/ CompleteImpl(r) \/ PassGate(r) \/ FailGate(r)
    \/ Escalate(r) \/ ResolveToSpec(r) \/ ResolveToTests(r) \/ ResolveToImpl(r)
    \/ ApproveFinal(r) \/ RejectFinal(r)
    \/ Archive(r) \/ RetryArchive(r) \/ Cancel(r)
    \/ CommitRecord(r) \/ CheckTrace(r) \/ AbortArchive(r)

Next ==
    \/ \E r \in Runs, c \in Changes : StartRun(r, c)
    \/ \E r \in Runs : RunStep(r)
    \/ Crash \/ Recover \/ Done

\* Safety only: no fairness assumptions.
Spec == Init /\ [][Next]_vars

\* State constraint, TLC only: rejection and escalation loops are unbounded.
LogBound == \A r \in Runs : Len(log[r]) <= MaxLog

---------------------------------------------------------------------------
(* Invariants *)

\* At most one unfinished run per change.
OneActiveRunPerChange ==
    \A r1, r2 \in Runs :
        (r1 # r2 /\ Active(r1) /\ Active(r2)) => change[r1] # change[r2]

\* events.jsonl holds exactly the committed events, plus the in-flight one.
LogMatchesRecord ==
    \A r \in Runs :
        IF up THEN Len(log[r]) = seq[r] + Len(pending[r])
              ELSE Len(log[r]) \in {seq[r], seq[r] + 1}

\* The committed events chain from "none" to the committed phase: what
\* trace validation checks against this module.
CommittedTraceValid ==
    \A r \in Runs :
        LET s == SubSeq(log[r], 1, seq[r])
        IN  /\ Len(s) = 0 => phase[r] = "none"
            /\ Len(s) > 0 => /\ s[1].from = "none"
                             /\ s[Len(s)].to = phase[r]
                             /\ \A i \in 1..(Len(s) - 1) : s[i].to = s[i + 1].from

\* Spec frozen from spec approval on; tests frozen from tests approval on.
FrozenMatchesPhase ==
    \A r \in Runs :
        /\ phase[r] \in {"none", "spec", "model", "spec_review"} => frozen[r] = {}
        /\ phase[r] \in {"tests", "tests_review"} => frozen[r] = {"spec"}
        /\ phase[r] \in {"impl", "verify", "final_review", "archive", "archived"}
               => frozen[r] = Artifacts
        /\ phase[r] = "escalated" => "spec" \in frozen[r]

\* No archived run without a passing trace validation.
ArchivedOnlyWithPassingTrace ==
    \A r \in Runs : phase[r] = "archived" => trace[r] = "pass"

TraceFailOnlyWhileArchiving ==
    \A r \in Runs : trace[r] = "fail" => phase[r] \in {"archive", "cancelled"}

\* An archived run passed all three human gates, in order.
ApprovalsBeforeArchive ==
    \A r \in Runs :
        phase[r] = "archived" =>
            \E i, j, k \in 1..seq[r] :
                /\ i < j /\ j < k
                /\ log[r][i].act = "ApproveSpec"
                /\ log[r][j].act = "ApproveTests"
                /\ log[r][k].act = "ApproveFinal"

(* Action properties *)

TerminalIsFinal ==
    [][\A r \in Runs : phase[r] \in Terminal => phase'[r] = phase[r]]_vars

\* Committed events are never rewritten or dropped.
CommittedEventsImmutable ==
    [][\A r \in Runs : SubSeq(log'[r], 1, seq[r]) = SubSeq(log[r], 1, seq[r])]_vars

\* Frozen artifacts are released only by a human resolving an escalation.
FrozenReleasedOnlyByResolve ==
    [][\A r \in Runs :
          ~(frozen[r] \subseteq frozen'[r]) =>
              /\ pending[r] # <<>>
              /\ pending[r][1].entry.act \in {"ResolveToSpec", "ResolveToTests"}]_vars

---------------------------------------------------------------------------
(* Reachability witnesses, checked by Run_reach.cfg: each one MUST be      *)
(* violated, proving the behaviour it negates is reachable.               *)

NeverArchived == \A r \in Runs : phase[r] # "archived"

NeverArchivedAfterReject ==
    \A r \in Runs :
        phase[r] = "archived" =>
            \A i \in 1..seq[r] :
                log[r][i].act \notin {"RejectSpec", "RejectTests", "RejectFinal"}

NeverTestsReopened ==
    \A r \in Runs : \A i \in 1..seq[r] : log[r][i].act # "ResolveToTests"

NeverTraceFailed == \A r \in Runs : trace[r] # "fail"

NeverCrashMidTransition == up \/ \A r \in Runs : Len(log[r]) = seq[r]

NeverSecondRunAfterCancel ==
    ~\E r1, r2 \in Runs :
        /\ r1 # r2
        /\ phase[r1] = "cancelled"
        /\ Active(r2)
        /\ change[r1] = change[r2]

=============================================================================
