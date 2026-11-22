#!/usr/bin/env bash
# SANY + TLC over spec/. Called by `make tla` (mode "gate") and
# `make tla-big` (mode "big"); see AGENTS.md, "Formal methods rules".
#
# Runs from inside spec/ because SANY and TLC resolve EXTENDS against the
# working directory, not the directory of the module being checked.
set -euo pipefail

mode=${1:?usage: tla.sh gate|big}
root=$(cd "$(dirname "$0")/.." && pwd)
jar=${TLA_JAR:-$root/tools/tla2tools.jar}
java=${JAVA:-java}
# TLC writes its state queue to ./states by default; keep it out of the tree.
meta=$root/.tlc
# A module without a .cfg is only allowed if it opts out explicitly;
# otherwise a forgotten .cfg would silently drop it from model checking.
helper_mark='\* refinery: helper'

[[ $jar = /* ]] || jar=$root/$jar
cd "$root/spec" 2>/dev/null || { echo "tla: no spec/ directory, nothing to check"; exit 0; }
shopt -s nullglob

die() { echo "tla: $*" >&2; exit 1; }

sany() {
  echo "== SANY $1.tla"
  "$java" -cp "$jar" tla2sany.SANY "$1.tla"
}

# Never pass -deadlock: terminal states must stutter explicitly instead.
tlc() {  # module cfg metadir-tag
  "$java" -XX:+UseParallelGC -jar "$jar" -workers auto \
    -metadir "$meta/$3" -config "$2" "$1.tla"
}

# Names listed under INVARIANT/INVARIANTS in a cfg, comments stripped.
cfg_invariants() {
  awk '{ sub(/\\\*.*/, ""); first = 1 }
       /^[A-Z_]+([ \t]|$)/ { inv = ($1 == "INVARIANT" || $1 == "INVARIANTS"); first = 2 }
       inv { for (i = first; i <= NF; i++) print $i }' "$1"
}

# The cfg without its INVARIANT sections, so one witness can be appended.
cfg_without_invariants() {
  awk '{ line = $0; sub(/\\\*.*/, "") }
       /^[A-Z_]+([ \t]|$)/ { inv = ($1 == "INVARIANT" || $1 == "INVARIANTS") }
       !inv { print line }' "$1"
}

# Every invariant in <M>_reach.cfg negates a behaviour that must be
# reachable, so each one must be violated. One TLC run per witness: with
# -continue, TLC reports only the first violated invariant per state, so
# witnesses would mask each other.
check_reach() {  # module
  local mod=$1 cfg=$1_reach.cfg inv wcfg out
  local -a invs
  mapfile -t invs < <(cfg_invariants "$cfg")
  ((${#invs[@]})) || die "$cfg lists no invariants"
  for inv in "${invs[@]}"; do
    wcfg=$(mktemp "$meta/reach-XXXXXX.cfg")
    { cfg_without_invariants "$cfg"; echo "INVARIANT $inv"; } > "$wcfg"
    out=$(tlc "$mod" "$wcfg" "${mod}_reach_$inv" 2>&1) || true
    rm -f "$wcfg"
    if grep -qF "Invariant $inv is violated" <<<"$out"; then
      echo "== REACH $mod: $inv violated, behaviour reachable"
    else
      echo "$out" | tail -20 >&2
      die "$cfg: $inv was not violated, so the behaviour it negates is unreachable"
    fi
  done
}

gate() {
  local tla mod
  local -a specs=(*.tla)
  ((${#specs[@]})) || { echo "tla: no spec/*.tla files, nothing to check"; return; }
  mkdir -p "$meta"
  for tla in "${specs[@]}"; do
    mod=${tla%.tla}
    sany "$mod"
    if [[ -f $mod.cfg ]]; then
      echo "== TLC  $mod.tla ($mod.cfg)"
      tlc "$mod" "$mod.cfg" "$mod"
    elif grep -qF "$helper_mark" "$tla"; then
      echo "== TLC  $mod.tla skipped: helper module"
    else
      die "spec/$tla has no spec/$mod.cfg and is not marked '$helper_mark'"
    fi
    if [[ -f ${mod}_reach.cfg ]]; then check_reach "$mod"; fi
  done
  for cfg in *_reach.cfg; do
    [[ -f ${cfg%_reach.cfg}.tla ]] || die "spec/$cfg has no matching module"
  done
}

# Occasional deep checks, not part of the gate: these may run for minutes.
big() {
  local cfg mod
  local -a cfgs=(*_big.cfg)
  ((${#cfgs[@]})) || { echo "tla-big: no spec/*_big.cfg files, nothing to check"; return; }
  for cfg in "${cfgs[@]}"; do
    mod=${cfg%_big.cfg}
    [[ -f $mod.tla ]] || die "spec/$cfg has no matching spec/$mod.tla"
    sany "$mod"
    echo "== TLC  $mod.tla ($cfg)"
    tlc "$mod" "$cfg" "${mod}_big"
  done
}

case $mode in
  gate) gate ;;
  big) big ;;
  *) die "unknown mode '$mode' (expected gate or big)" ;;
esac
