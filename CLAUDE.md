# Digest Repo Context — Macro + Stock Routines

This repo is the persistent state store for TWO scheduled Routines:
1. **Tech-Sector Macro Digest** — sector-level macro news ranking (QQQ benchmark)
2. **Stock News Digest** — per-ticker stock-specific news ranking (watchlist-based)

Claude Code reads this file automatically at the start of every run of either Routine.

## Files in this repo
- `digest-state.json` — delta state for the MACRO routine ONLY.
- `stock-digest-state.json` — delta state for the STOCK routine ONLY, keyed per ticker.
- `CLAUDE.md` — this file. Holds the Refinement Logs below.

**Ownership is strict:** each Routine reads and writes ITS OWN state file and must
never modify the other's. This is restated as Active rule #1 below so both
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
