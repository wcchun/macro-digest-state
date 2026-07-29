# Wheel Strategy — Closed Cycle Log (Phase 6)

Persistent record for `wheel_strategy_system_prompt.md`. Append one row per CLOSED cycle
(called away, wheel broken, or CSP expired worthless without assignment). Open positions live
in `wheel-positions.json`, not here.

A cycle is only informative next to its benchmark: the same capital in the shares over the same
window. The wheel is expected to lag in strong bull markets. If it lags in FLAT markets, the
strike or expiry rules are wrong.

## Closed cycles

| Cycle start | Cycle end | Ticker | Tier | Legs / rolls | Collateral USD | Premium collected | Dividends | Commissions | Realised P&L USD | P&L % of collateral | Annualised % | Buy & hold same window | Verdict vs benchmark | Exit rule that fired |
|-------------|-----------|--------|------|--------------|----------------|-------------------|-----------|-------------|------------------|---------------------|--------------|------------------------|----------------------|----------------------|

## Gate post-mortems

For each closed cycle, note any gate that passed at entry but looks wrong in hindsight.

| Date | Ticker | Gate | Reading at entry | What actually happened | Keep / adjust threshold |
|------|--------|------|------------------|------------------------|-------------------------|

## Calibration findings

After 10 closed cycles, compare realised annualised yield against the tier hurdle that admitted
each trade, and record the conclusion here.

- _(empty — add bullets as cycles close, e.g. "Tier C 25% hurdle only cleared on IV > 55%; consider raising the IVR floor for Tier C")_
