---
name: straddle
description: Full pre-earnings Long Straddle analysis for a ticker (IVR gate, implied move, Greeks, 6-quarter history, scenarios, exit dashboard). Use for "/straddle TICKER" or any long-straddle earnings analysis request.
---

# Long Straddle Pre-Earnings Analysis

Follow the full protocol in `Investment Analysis/long_straddle_analyst_system_prompt.md`, judged against `Investment Analysis/long_straddle_reference.md`. Read both first. Never skip Phase 3 (6-quarter history). The argument is the ticker, optionally with the earnings date (always verify it regardless).

## Data sourcing order (repo-aware)

1. **Tastytrade script = the "Tastytrade Data Block".** If `TT_REFRESH_TOKEN` and `TT_CLIENT_SECRET` are set, run `python3 "Investment Analysis/tastytrade_fetch.py" TICKER` yourself and treat its output exactly as the prompt's Tastytrade Data Block (skip Searches 4–8, label `✓ Confirmed (Tastytrade)`).
2. **Repo snapshots.** `options-result.json` supplies the IV term structure, `iv30_interpolated`, P/C ratios, and the earnings flag; `iv-history.json` supplies `iv_rank_to_date` for the IVR gate (check `sample_size` — under ~60 samples, cross-check IVR via web before passing/failing the gate); `crossover-result.json` supplies price/ATR/volume context and `next_earnings_date`. Label `✓ Confirmed (repo snapshot, as of <run_at>)`; stale >2 trading days → refetch.
3. **Web search** per the prompt for everything else: earnings-date verification (mandatory, two sources), historical reactions, analyst consensus, confirmed implied move, revenue/EPS consensus.

## After the analysis

Append a row to `Investment Analysis/calibration/straddle_trades.md` — including "no trade" verdicts with which gates failed. When the user reports Day 0 / Day 5 outcomes, fill those columns in. Commit calibration-log changes; never modify the workflow-owned JSONs.
