# Options Sentiment — Calibration Log

Persistent home for Phase 4 of `options_sentiment_analyst_system_prompt.md`.
Append one row per scored observation; review hit rates after 10 observations
per signal per ticker. In Claude Projects, paste new rows back into this file.

## Observation log

| Date | Ticker | Signal | Reading | Predicted direction | Actual outcome | Hit/Miss | Notes |
|------|--------|--------|---------|--------------------|----------------|----------|-------|
| 2026-09-22 | ONDS | All (1-5) | NO READ - no confirmed data | None - verdict withheld | _pending_ | _pending_ | Tier B (mcap $4.30B, beta 2.68). Absent from options-result.json/iv-history.json (watchlist swap not yet on main); no Tastytrade creds; all options-data domains blocked by egress policy. Only stale figures found (Vol P/C 0.57, OI P/C 0.48, 3 unusual contracts - all as-of 2026-09-04, 12 sessions stale, no call/put split) - excluded per 2-day staleness rule. Signals 6-7 N/A (no catalyst; Tier B and not within 5 sessions of monthly opex). Composite not computable. |

## Threshold override log (ticker-specific)

| Ticker | Signal | Standard threshold | Observed better threshold | Sample size | Notes |
|--------|--------|-------------------|--------------------------|-------------|-------|
