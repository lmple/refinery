----------------------------- MODULE RunnerRuns -----------------------------
(* Runner.tla checked with two runs and two runners: runs compete for     *)
(* claims, and one runner's crash or shutdown affects several jobs.       *)
(* A separate module only because make tla runs one .cfg per module.      *)
EXTENDS Runner
=============================================================================
