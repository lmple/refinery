------------------------------ MODULE RunPair ------------------------------
(* Run.tla checked with two runs competing for one change: exercises the  *)
(* one-active-run-per-change guard, including two StartRuns in flight.    *)
(* A separate module only because make tla runs one .cfg per module.      *)
EXTENDS Run
=============================================================================
