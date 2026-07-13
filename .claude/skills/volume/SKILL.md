---
name: volume
description: Volume signal analysis for a ticker against the quantitative framework (breakout confirmation, pullback health, VDU, climax bars, opening range, VWAP, late-day) with U-curve-adjusted intraday thresholds. Use for "/volume TICKER [signals]".
---

# Volume Analysis

Follow the full protocol in `Investment Analysis/volume_analyst_system_prompt.md` — read it first; never skip Phase 0 (ADV/ATR baselines) and always apply the U-curve adjustment for intraday work. The argument is the ticker, optionally with specific signals or a timeframe; default to the signals relevant to the user's question.

## Data sourcing order (repo-aware)

1. **Repo snapshot first.** `crossover-result.json` supplies 20-day ADV (`adv20_shares`), ATR20 (`atr20`, `atr20_pct`), today's volume and `volume_vs_adv20`, MA levels (S/R context), and the last cross event. Label `✓ Confirmed (repo snapshot, as of <run_at>)`; stale >2 trading days → refetch via web. Note: the snapshot has no 50-day ADV — fetch it via web when the divergence check matters.
2. **Web search** per the prompt for everything else: intraday candle volumes, premarket volume, key S/R levels, special-day checks (OpEx Friday, rebalance, FOMC).

## After the analysis

Append observation rows to `Investment Analysis/calibration/volume_calibration.md`; when a threshold clearly over/under-performed, add it to the override table per the prompt's calibration instructions. Commit calibration-log changes; never modify the workflow-owned JSONs.
