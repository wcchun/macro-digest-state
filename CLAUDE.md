# Tech-Sector Macro Digest — Repo Context

This repo is the persistent state store for the scheduled "Tech-Sector Macro Digest" Routine.
Claude Code reads this file automatically at the start of every run.

## Files in this repo
- `digest-state.json` — the delta state. Read at the start of each run, rewritten and pushed to `main` at the end. Do not hand-edit unless resetting.
- `CLAUDE.md` — this file. Holds the Refinement Log below.

## How state works
- Empty `digest-state.json` (`{}` or no `stories`) → next run is a full-window digest with no delta markers.
- To force a fresh full digest: replace `digest-state.json` contents with `{}` and commit to `main`.
- The Routine writes `digest-state.json` back to the **default branch (main)** every run so the next run reads current state.

═══════════════════════════════════════════════════════════
## Refinement Log
═══════════════════════════════════════════════════════════

Calibration rules I add over time. The digest Routine honours every rule here on every run.
Add a new bullet whenever a ranking disagrees with my view.

Examples of the kind of rule that belongs here:
- (example) Cap analyst/strategist outlook pieces at 3, even from major banks.
- (example) Crypto news scores 1 unless it spills into Nasdaq flows at scale (COIN/MSTR-size).
- (example) Weight semiconductor policy news +1 higher — I'm overweight semis.
- (example) Regional-Fed-president speakers cap at 6; only Chair/Vice-Chair/NY Fed reach 7+.

### Active rules
_(empty — add rules as you go)_
