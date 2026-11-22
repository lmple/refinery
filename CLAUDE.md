# CLAUDE.md — Refinery

@AGENTS.md

## Claude Code specifics

- Use **plan mode** for anything larger than a single task; present the plan
  before editing files.
- Work one task from `openspec/changes/<name>/tasks.md` at a time; tick it off
  when its tests pass and it is committed.
- At each review gate (after spec+model, after tests), stop and wait for
  explicit approval. Do not continue on your own.
- Use subagents for read-only exploration (e.g. "find every place a run state
  changes"); do not delegate edits to subagents.
- Prefer `make` targets over ad-hoc commands so results match CI.
- When TLC reports a violation, show the counterexample trace as a compact table
  (step, action, changed variables) and classify the failure (spec / test / code
  bug) before proposing anything.
- Hooks in `.claude/settings.json` (once added) may block edits outside the
  current phase. If an edit is blocked, do not work around it: report it and
  ask.
- Keep context small: suggest `/clear` between unrelated tasks.

## Session start checklist

1. Read `docs/vision.md` (if present) and the active change in
   `openspec/changes/`.
2. Check which phase the change is in (spec / model / tests / impl / verify).
3. State the phase and the next task in one line before doing anything.
