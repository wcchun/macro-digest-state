# Trading Research Hub — Routines, Workflows, and Analysis Skills

This repo is the shared state store and integration point for THREE layers:

1. **Layer 1 — automated data (GitHub Actions):** `crossover.yml` and `options.yml`
   run `scripts/*.py` daily after the US close and commit technical/options
   snapshots for the tickers in `watchlist.json`.
2. **Layer 2 — automated narrative (scheduled Routines):**
   **Tech-Sector Macro Digest** (sector-level macro news, QQQ benchmark) and
   **Stock News Digest** (per-ticker news, watchlist-based). The stock digest
   reads the Layer-1 JSONs read-only and ends with a STRATEGY TRIAGE section
   pointing at the Layer-3 playbooks.
3. **Layer 3 — on-demand deep dives (skills):** `/options-sentiment`, `/straddle`,
   `/iv-ramp`, `/volume` wrap the system prompts in `Investment Analysis/` and
   persist their calibration logs in `Investment Analysis/calibration/`.

Claude Code reads this file automatically at the start of every run of either Routine.

## File ownership matrix — WHO WRITES WHAT (strict)

| File | Written by | Everyone else |
|------|-----------|---------------|
| `digest-state.json` | Macro Digest Routine ONLY | hands off |
| `stock-digest-state.json` | Stock News Digest Routine ONLY | hands off |
| `watchlist.json` | the user (by hand) | read-only |
| `crossover-result.json` | `crossover.yml` workflow | read-only |
| `options-result.json`, `iv-history.json` | `options.yml` workflow | read-only |
| `Investment Analysis/calibration/*.md` | Layer-3 analysis skills (append) | read-only |
| `Routines_instructions/*.md`, `Investment Analysis/*.md`, `scripts/*` | the user | read-only |
| `CLAUDE.md` Refinement Logs | user + Routines (adding calibration rules) | — |

If a file you don't own shows as modified in your working tree, exclude it from
your commit (revert it). This is restated as Active rule #1 below so both
Routines are bound by it.

## How state works
- Both Routines write their state file back to the **default branch (main)** at the
  end of every run so the next run reads current state. Never leave state on a side branch.
- Empty state (`{}` or missing) → next run is a full-window digest with no delta markers.
- **Reset the macro digest:** replace `digest-state.json` contents with `{}`, commit to main.
- **Reset the stock digest (all tickers):** replace `stock-digest-state.json` with `{}`, commit to main.
- **Reset ONE ticker only:** delete just that ticker's entry from the `"tickers"` object
  in `stock-digest-state.json` — that ticker re-initialises on its next run while the
  others stay in delta mode.
- The two Routines run on staggered schedules to avoid pushing to main at the same
  moment. If a push is rejected because the other Routine pushed first, pull/rebase
  and push again — never resolve by force-pushing or dropping the other file's changes.

═══════════════════════════════════════════════════════════
## Refinement Log
═══════════════════════════════════════════════════════════

Calibration rules I add over time. **BOTH Routines honour every rule in this section
on every run** — so only put rules here that should apply globally (macro scoring,
source handling, formatting, repo hygiene). Ticker-specific rules go in the
Stock Digest Refinement Log below instead.

Add a new bullet whenever a ranking disagrees with my view.

Examples of the kind of rule that belongs here:
- (example) Cap analyst/strategist outlook pieces at 3, even from major banks.
- (example) Crypto news scores 1 unless it spills into Nasdaq flows at scale (COIN/MSTR-size).
- (example) Weight semiconductor policy news +1 higher — I'm overweight semis.
- (example) Regional-Fed-president speakers cap at 6; only Chair/Vice-Chair/NY Fed reach 7+.

### Active rules
- File ownership: the Macro Digest Routine must never read, write, or commit changes
  to `stock-digest-state.json`; the Stock News Digest Routine must never read, write,
  or commit changes to `digest-state.json`. If the other routine's state file shows as
  modified in the working tree, revert it before committing.
- Neither Routine may write to the workflow-owned or user-owned files in the
  ownership matrix above (`watchlist.json`, `crossover-result.json`,
  `options-result.json`, `iv-history.json`, `scripts/`, the instruction/prompt
  markdown files). Read freely; never commit changes to them.

═══════════════════════════════════════════════════════════
## Stock Digest Refinement Log
═══════════════════════════════════════════════════════════

Rules here apply to the **Stock News Digest Routine only** (the macro Routine ignores
this section). Use it for per-ticker calibration.

Examples of the kind of rule that belongs here:
- (example) PLTR: any story sourced only from retail-focused outlets caps at 3.
- (example) PLTR: cap "AIP bootcamp" / customer-count announcements at 4 unless a
  dollar figure or named large customer is attached.
- (example) NVDA (when added): supply-chain rumors from Taiwanese trade press cap at 4
  until confirmed by a second source.

### Active rules
_(empty — add rules as you go)_
