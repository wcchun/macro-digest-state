---
name: options-sentiment
description: Run the 7-signal Options Sentiment Analysis on a ticker (IVR, P/C ratios, skew, term structure, unusual activity, implied move, max pain). Use when the user asks for an options sentiment read, positioning check, or "/options-sentiment TICKER".
---

# Options Sentiment Analysis

Follow the full protocol in `Investment Analysis/options_sentiment_analyst_system_prompt.md` — read it first and honour every phase, threshold table, and data-labelling rule. The argument is the ticker (e.g. `/options-sentiment PLTR`); if a phase/signal subset is named, run only those.

## Data sourcing order (repo-aware override of the prompt's search list)

1. **Repo snapshots first.** Read `options-result.json`, `iv-history.json`, and `crossover-result.json` in the repo root. They directly supply: spot, ATM IV term structure, `iv30_interpolated`, `term_structure_ratio` (Signal 4), Volume P/C and OI P/C (Signal 2), `skew_25d_proxy_volpts` (Signal 3 level — the 5-session skew *change* still needs `iv-history.json` context or web), `max_pain_nearest_expiry` (Signal 7), earnings flag, and `iv_rank_to_date` (Signal 1 — check `sample_size`; below ~60 samples treat the rank as low-confidence and cross-check via web). Label these `✓ Confirmed (repo snapshot, as of <run_at>)`. If `run_at` is older than 2 trading days, treat as stale — refetch instead.
2. **Live scripts if credentials exist.** If `TT_REFRESH_TOKEN` and `TT_CLIENT_SECRET` are set, run `python3 "Investment Analysis/tastytrade_fetch.py" TICKER` for live IV/Greeks and label `✓ Confirmed (Tastytrade)`.
3. **Web search fallback** exactly as the system prompt specifies, for anything not covered above (unusual options activity detail, implied-move quotes, skew history).

## After the analysis

Append the observation rows to `Investment Analysis/calibration/options_sentiment.md` (date, ticker, each signal's reading and predicted direction). When the user later reports the outcome, score Hit/Miss there per the prompt's Phase 4 milestones. Commit calibration-log changes; never modify the workflow-owned JSONs.
