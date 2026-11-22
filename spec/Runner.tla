------------------------------- MODULE Runner -------------------------------
(***************************************************************************)
(* Agent jobs: how runners claim the runs that want an agent, launch and   *)
(* stop agent processes, and report how they ended; failed attempts and    *)
(* stuck runs; runner crashes (agents survive as orphans until the runner  *)
(* restarts and reaps them), graceful shutdown, and claims whose response  *)
(* is lost.                                                                *)
(*                                                                         *)
(* A run is coarsened from Run.tla to whose turn it is: "agent" (spec,     *)
(* model, tests, impl), "human" (every phase where the agent waits: the    *)
(* review gates, escalated, verify) or "ended". Any move of the run sets   *)
(* `moved`, which stands for "the run's seq differs from the job's claim   *)
(* seq" in the implementation (design D4).                                 *)
(*                                                                         *)
(* Action names match the job log's events: Claim, Finish, Release (here   *)
(* inside Hello, Shutdown and Claim's reconciliation) and Retry.           *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Runs,         \* run ids (model values)
    Runners,      \* runner ids, i.e. runner token ids (model values)
    MaxAttempts,  \* failed attempts that make a run stuck (REFINERY_AGENT_ATTEMPTS)
    MaxFaults     \* TLC-only bound on runner crashes, shutdowns and lost claim
                  \* responses: a runner stopped for ever cannot make progress

None == "none"

VARIABLES
    \* factoryd: the run and its job log
    turn,      \* whose turn the run is: "agent", "human" or "ended"
    moved,     \* the run moved since its current job was claimed
    job,       \* the run's open job: the holding runner, or None
    tok,       \* the open job's agent token is live
    failures,  \* failed attempts in the current turn since the last Retry
    \* the machine: agent processes, which outlive a crashed runner
    procs,     \* procs[r][x]: agent processes of runner x for run r (0..2)
    \* each runner process (its memory is lost when it crashes)
    rstate,    \* "down", "starting" (not reaped yet), "reaped" or "ready" (said hello)
    lj,        \* lj[x][r]: the runner's job for r: "none", "claimed", "running", "exited"
    \* bookkeeping for TLC only
    faults,    \* crashes, shutdowns and lost claim responses so far
    seen       \* behaviours seen so far, for the reachability witnesses

vars == <<turn, moved, job, tok, failures, procs, rstate, lj, faults, seen>>

Tags == {"retry", "reaped_orphan", "cancel_kill", "reconciled"}

Stuck(r) == failures[r] >= MaxAttempts

\* The definition the claim uses (agent-jobs, "Runs in an agent phase want an agent").
Wants(r) == turn[r] = "agent" /\ job[r] = None /\ ~Stuck(r)

\* Runners with a live agent process for r (running or orphaned).
Agents(r) == {x \in Runners : procs[r][x] > 0}

TypeOK ==
    /\ turn \in [Runs -> {"agent", "human", "ended"}]
    /\ moved \in [Runs -> BOOLEAN]
    /\ job \in [Runs -> Runners \cup {None}]
    /\ tok \in [Runs -> BOOLEAN]
    /\ failures \in [Runs -> 0..MaxAttempts]
    /\ procs \in [Runs -> [Runners -> 0..2]]
    /\ rstate \in [Runners -> {"down", "starting", "reaped", "ready"}]
    /\ lj \in [Runners -> [Runs -> {"none", "claimed", "running", "exited"}]]
    /\ faults \in 0..MaxFaults
    /\ seen \subseteq Tags

Init ==
    /\ turn = [r \in Runs |-> "agent"]   \* StartRun: every run starts in spec
    /\ moved = [r \in Runs |-> FALSE]
    /\ job = [r \in Runs |-> None]
    /\ tok = [r \in Runs |-> FALSE]
    /\ failures = [r \in Runs |-> 0]
    /\ procs = [r \in Runs |-> [x \in Runners |-> 0]]
    /\ rstate = [x \in Runners |-> "down"]
    /\ lj = [x \in Runners |-> [r \in Runs |-> "none"]]
    /\ faults = 0
    /\ seen = {}

\* Release every open job of runner x in `rs`: the job closes and its token is
\* revoked. Releases are never attempts, so `failures` is untouched.
ReleaseAll(x, rs) ==
    /\ job' = [r \in Runs |-> IF r \in rs /\ job[r] = x THEN None ELSE job[r]]
    /\ tok' = [r \in Runs |-> IF r \in rs /\ job[r] = x THEN FALSE ELSE tok[r]]

---------------------------------------------------------------------------
(* The runner process and its environment *)

\* The runner process starts (after a crash or a shutdown).
Start(x) ==
    /\ rstate[x] = "down"
    /\ rstate' = [rstate EXCEPT ![x] = "starting"]
    /\ UNCHANGED <<turn, moved, job, tok, failures, procs, lj, faults, seen>>

\* Reap: kill every process group in the runner's own registry.
Reap(x) ==
    /\ rstate[x] = "starting"
    /\ procs' = [r \in Runs |-> [procs[r] EXCEPT ![x] = 0]]
    /\ rstate' = [rstate EXCEPT ![x] = "reaped"]
    /\ seen' = IF \E r \in Runs : procs[r][x] > 0 THEN seen \cup {"reaped_orphan"} ELSE seen
    /\ UNCHANGED <<turn, moved, job, tok, failures, lj, faults>>

\* Hello: factoryd releases (runner_restart) every open job of the runner.
Hello(x) ==
    /\ rstate[x] = "reaped"
    /\ ReleaseAll(x, Runs)
    /\ rstate' = [rstate EXCEPT ![x] = "ready"]
    /\ UNCHANGED <<turn, moved, failures, procs, lj, faults, seen>>

\* Claim: the runner sends the jobs it holds; factoryd first releases (lost)
\* the caller's other open jobs, whose claim response never arrived, then
\* hands out a run that wants an agent, if any. The response may be lost.
Lost(x) == {r \in Runs : job[r] = x /\ lj[x][r] = "none"}
Reconciled(x) == [r \in Runs |-> IF r \in Lost(x) THEN None ELSE job[r]]
SeenLost(x) == IF Lost(x) # {} THEN seen \cup {"reconciled"} ELSE seen

\* A claim that hands out run r.
ClaimRun(x, r) ==
    /\ rstate[x] = "ready"
    /\ turn[r] = "agent" /\ Reconciled(x)[r] = None /\ ~Stuck(r) /\ lj[x][r] = "none"
    /\ \E delivered \in BOOLEAN :
          /\ delivered \/ faults < MaxFaults
          /\ job' = [Reconciled(x) EXCEPT ![r] = x]
          /\ tok' = [q \in Runs |-> IF q = r THEN TRUE              \* older tokens revoked
                                    ELSE IF q \in Lost(x) THEN FALSE ELSE tok[q]]
          /\ moved' = [moved EXCEPT ![r] = FALSE]
          /\ lj' = IF delivered THEN [lj EXCEPT ![x][r] = "claimed"] ELSE lj
          /\ faults' = IF delivered THEN faults ELSE faults + 1
    /\ seen' = SeenLost(x)
    /\ UNCHANGED <<turn, failures, procs, rstate>>

\* A claim with no run to hand out, which still reconciles (204).
ClaimNone(x) ==
    /\ rstate[x] = "ready" /\ Lost(x) # {}
    /\ ~\E r \in Runs : turn[r] = "agent" /\ Reconciled(x)[r] = None /\ ~Stuck(r)
                        /\ lj[x][r] = "none"
    /\ job' = Reconciled(x)
    /\ tok' = [q \in Runs |-> IF q \in Lost(x) THEN FALSE ELSE tok[q]]
    /\ seen' = SeenLost(x)
    /\ UNCHANGED <<turn, moved, failures, procs, rstate, lj, faults>>

Claim(x) == ClaimNone(x) \/ \E r \in Runs : ClaimRun(x, r)

\* Launch the agent through the supervisor.
Launch(x, r) ==
    /\ rstate[x] = "ready" /\ lj[x][r] = "claimed"
    /\ procs' = [procs EXCEPT ![r][x] = IF @ < 2 THEN @ + 1 ELSE @]
    /\ lj' = [lj EXCEPT ![x][r] = "running"]
    /\ UNCHANGED <<turn, moved, job, tok, failures, rstate, faults, seen>>

\* The agent process ends by itself: it stopped, crashed or hit the time
\* limit. An orphan (its runner down or not yet reaped) may end too.
AgentExit(x, r) ==
    /\ procs[r][x] > 0
    /\ procs' = [procs EXCEPT ![r][x] = @ - 1]
    /\ lj' = IF lj[x][r] = "running" THEN [lj EXCEPT ![x][r] = "exited"] ELSE lj
    /\ UNCHANGED <<turn, moved, job, tok, failures, rstate, faults, seen>>

\* Poll answers stop (the run ended or left the agent's turn, or the job is
\* no longer open), and the runner cancels the agent call: group killed.
Stop(x, r) ==
    /\ rstate[x] = "ready" /\ lj[x][r] = "running"
    /\ turn[r] # "agent" \/ job[r] # x
    /\ procs' = [procs EXCEPT ![r][x] = 0]
    /\ lj' = [lj EXCEPT ![x][r] = "exited"]
    /\ seen' = IF turn[r] = "ended" THEN seen \cup {"cancel_kill"} ELSE seen
    /\ UNCHANGED <<turn, moved, job, tok, failures, rstate, faults>>

\* Finish: the runner reports the end of the agent call. A failed attempt if
\* nothing moved the run during the job. Idempotent on a closed job.
Finish(x, r) ==
    /\ rstate[x] = "ready" /\ lj[x][r] = "exited"
    /\ IF job[r] = x
          THEN /\ job' = [job EXCEPT ![r] = None]
               /\ tok' = [tok EXCEPT ![r] = FALSE]
               /\ failures' = [failures EXCEPT ![r] =
                                 IF ~moved[r] /\ turn[r] = "agent" THEN @ + 1 ELSE @]
          ELSE UNCHANGED <<job, tok, failures>>
    /\ lj' = [lj EXCEPT ![x][r] = "none"]
    /\ UNCHANGED <<turn, moved, procs, rstate, faults, seen>>

\* SIGINT/SIGTERM: cancel every agent, then release each job (best effort:
\* factoryd may not hear it, and the next Hello covers that).
Shutdown(x) ==
    /\ rstate[x] = "ready" /\ faults < MaxFaults
    /\ procs' = [r \in Runs |-> [procs[r] EXCEPT ![x] = 0]]
    /\ \E heard \in BOOLEAN :
          IF heard THEN ReleaseAll(x, Runs) ELSE UNCHANGED <<job, tok>>
    /\ rstate' = [rstate EXCEPT ![x] = "down"]
    /\ lj' = [lj EXCEPT ![x] = [r \in Runs |-> "none"]]
    /\ faults' = faults + 1
    /\ UNCHANGED <<turn, moved, failures, seen>>

\* The runner process dies: its memory is lost, its agents live on.
Crash(x) ==
    /\ rstate[x] # "down" /\ faults < MaxFaults
    /\ rstate' = [rstate EXCEPT ![x] = "down"]
    /\ lj' = [lj EXCEPT ![x] = [r \in Runs |-> "none"]]
    /\ faults' = faults + 1
    /\ UNCHANGED <<turn, moved, job, tok, failures, procs, seen>>

---------------------------------------------------------------------------
(* The agent and the human, through factoryd's other services *)

\* The agent moves the run with its token (submit_for_review, escalate):
\* spec -> model stays the agent's turn, a review or verify does not.
Progress(r) ==
    /\ turn[r] = "agent" /\ tok[r]
    /\ job[r] # None /\ procs[r][job[r]] > 0
    /\ \E t \in {"agent", "human"} : turn' = [turn EXCEPT ![r] = t]
    /\ moved' = [moved EXCEPT ![r] = TRUE]
    /\ failures' = [failures EXCEPT ![r] = 0]
    /\ UNCHANGED <<job, tok, procs, rstate, lj, faults, seen>>

\* A human decision, an escalation resolved, or a failed gate: back to the agent.
Review(r) ==
    /\ turn[r] = "human"
    /\ turn' = [turn EXCEPT ![r] = "agent"]
    /\ moved' = [moved EXCEPT ![r] = TRUE]
    /\ failures' = [failures EXCEPT ![r] = 0]
    /\ UNCHANGED <<job, tok, procs, rstate, lj, faults, seen>>

\* Cancel (any turn) or the final approval's archive: the run ends, and its
\* agent tokens stop working (access-tokens).
End(r) ==
    /\ turn[r] # "ended"
    /\ turn' = [turn EXCEPT ![r] = "ended"]
    /\ moved' = [moved EXCEPT ![r] = TRUE]
    /\ tok' = [tok EXCEPT ![r] = FALSE]
    /\ failures' = [failures EXCEPT ![r] = 0]
    /\ UNCHANGED <<job, procs, rstate, lj, faults, seen>>

\* refine retry: a stuck run wants an agent again.
Retry(r) ==
    /\ Stuck(r) /\ turn[r] = "agent"
    /\ failures' = [failures EXCEPT ![r] = 0]
    /\ seen' = seen \cup {"retry"}
    /\ UNCHANGED <<turn, moved, job, tok, procs, rstate, lj, faults>>

\* Everything finished: explicit stuttering, so the deadlock check stays on.
Done ==
    /\ \A r \in Runs : turn[r] = "ended" /\ job[r] = None /\ Agents(r) = {}
    /\ \A x \in Runners : rstate[x] = "ready" /\ \A r \in Runs : lj[x][r] = "none"
    /\ UNCHANGED vars

Next ==
    \/ Done
    \/ \E x \in Runners :
          \/ Start(x) \/ Reap(x) \/ Hello(x) \/ Claim(x) \/ Shutdown(x) \/ Crash(x)
          \/ \E r \in Runs : Launch(x, r) \/ AgentExit(x, r) \/ Stop(x, r) \/ Finish(x, r)
    \/ \E r \in Runs : Progress(r) \/ Review(r) \/ End(r) \/ Retry(r)

\* Fairness, for the liveness properties only, on the runner's own steps:
\* it is a long-running service that is restarted when it stops (Start), and
\* its loop keeps reaping, greeting, claiming, launching, polling and
\* reporting. Claims are fair per run because factoryd hands out the run that
\* has wanted an agent longest (design D5), so no run is passed over for ever. Crash and Shutdown are the environment's and the human's, and
\* the agent's and the human's moves stay unconstrained.
Fairness ==
    \A x \in Runners :
        /\ WF_vars(Start(x)) /\ WF_vars(Reap(x)) /\ WF_vars(Hello(x))
        /\ WF_vars(ClaimNone(x)) /\ \A r \in Runs : WF_vars(ClaimRun(x, r))
        /\ \A r \in Runs : WF_vars(Launch(x, r)) /\ WF_vars(Stop(x, r)) /\ WF_vars(Finish(x, r))

Spec == Init /\ [][Next]_vars /\ Fairness

---------------------------------------------------------------------------
(* Invariants *)

\* A run never has two agent processes, across runners and orphans.
OneAgentPerRun == \A r \in Runs : Cardinality(Agents(r)) <= 1 /\ \A x \in Runners : procs[r][x] <= 1

\* An agent process exists only for its own runner's open job.
AgentsOnlyForOpenJobs == \A r \in Runs, x \in Runners : procs[r][x] > 0 => job[r] = x

\* A live agent token means an open job of a run that has not ended.
TokenOnlyForOpenJob == \A r \in Runs : tok[r] => job[r] # None /\ turn[r] # "ended"

\* A stuck run holds no job: nobody claims it until Retry.
StuckRunsHaveNoJob == \A r \in Runs : Stuck(r) => job[r] = None

\* A runner that has said hello knows every agent it has (no orphans).
NoOrphansOnceReady == \A r \in Runs, x \in Runners :
    rstate[x] = "ready" /\ procs[r][x] > 0 => lj[x][r] = "running"

(* Liveness *)

\* Once a run ends, its agent goes, even across runner crashes.
CancelledRunsLoseTheirAgent == \A r \in Runs : turn[r] = "ended" ~> Agents(r) = {}

\* The agent's turn gets an agent, unless the run gets stuck or moves on.
AgentTurnsGetAnAgentOrStuck ==
    \A r \in Runs : turn[r] = "agent" ~> (Agents(r) # {} \/ Stuck(r) \/ turn[r] # "agent")

---------------------------------------------------------------------------
(* Reachability witnesses (Runner_reach.cfg): each MUST be violated. *)

NeverStuck == \A r \in Runs : ~Stuck(r)

NeverRetried == "retry" \notin seen

NeverKilledByCancel == "cancel_kill" \notin seen

\* A crash left an agent behind, the restart reaped it, and the run has an
\* agent again.
NeverReclaimedAfterReap == ~("reaped_orphan" \in seen /\ \E r \in Runs : Agents(r) # {})

NeverLostClaimReconciled == "reconciled" \notin seen

\* One job carries the agent across a phase change in its own turn (spec -> model).
NeverSameJobAcrossPhases == ~\E r \in Runs : moved[r] /\ turn[r] = "agent" /\ Agents(r) # {}

\* Two runs have agents at the same time (RunnerRuns_reach.cfg).
NeverTwoAgentsAtOnce == ~\A r \in Runs : Agents(r) # {}

=============================================================================
