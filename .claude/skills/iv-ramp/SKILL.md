---
name: iv-ramp
description: Pre-Earnings IV Ramp Harvest — same-day vega scalp on an AMC earnings day (presession check, midday gate check GO/NO-GO, exit monitor). Use for "/iv-ramp TICKER [--presession|--check|--exit]".
---

# Pre-Earnings IV Ramp Harvest

Follow the full protocol in `Investment Analysis/iv_ramp_harvest_system_prompt.md` (companion frameworks: `volume_analyst_system_prompt.md`, `options_sentiment_analyst_system_prompt.md`, `long_straddle_reference.md`). The argument is the ticker plus the phase: presession, open, check (midday gates), or exit. Non-negotiables from the prompt apply — never recommend holding into the print; hard flat by 3:45 PM EDT / 3:45 AM MYT.

## Data sourcing order (repo-aware)

1. **Run the companion script yourself** if `TT_REFRESH_TOKEN` and `TT_CLIENT_SECRET` are set: `python3 "Investment Analysis/iv_ramp_harvest.py" TICKER --presession|--open|--check|--exit`. Interpret its output per the prompt's "WITH THE SCRIPT" section (GO/NO-GO on gates, judgment on ⚠️ borderline readings).
2. **Repo snapshots for presession context.** `options-result.json` gives yesterday's term structure and `term_structure_ratio` (Gate 5 preview) and the earnings flag; `crossover-result.json` gives ADV20, ATR20, and MA levels for the VDU denominator and S/R context. These are previous-close data — clearly label them so, never as live intraday readings.
3. **Web search always** for: AMC timing confirmation (two sources — if BMO, stop per the prompt), same-day macro calendar (Gate 6), and breaking ticker news.

Live intraday IV readings the user pastes are the ground truth for `--check`/`--exit` decisions when the script isn't available.

## After the trade (or skip)

Append the Phase 5 row to `Investment Analysis/calibration/iv_ramp_trades.md`, including skipped setups and which gate killed them. Commit calibration-log changes; never modify the workflow-owned JSONs.
