---
name: wheel
description: Wheel strategy analysis — screen candidates through the hard gates and build cash-secured-put / covered-call trade plans, or review open wheel positions against the exit rules. Use for "/wheel", "/wheel screen", "/wheel manage TICKER", or any wheel / cash-secured-put / covered-call request.
---

# Wheel Strategy

Follow the full protocol in `Investment Analysis/wheel_strategy_system_prompt.md` — read it first and honour every hard gate, tier rule, and exit rule. Two modes:

- **`/wheel` or `/wheel screen`** — Phases 1–3 across the wheel universe, then the portfolio check.
- **`/wheel manage [TICKER]`** — open-position review only: which exit rule fires today and the exact order to place. Always run this part first when positions are open, even in screen mode; managing what you hold outranks opening more.

## Before anything else

Read `wheel-positions.json`. If `wheel_capital_usd` is null or the OPERATOR PROFILE placeholders in the prompt are unfilled, **stop and ask the user** for account size, max-deployed %, risk tolerance, and broker commission — every sizing rule, tier ceiling, and portfolio limit depends on them, and guessing produces plans that cannot be executed. Compute and state the three tier strike ceilings in the session header.

## Data sourcing order

1. **Repo snapshots first.** `crossover-result.json` serves gates 2, 3, 5, 7, 9 and the event checks: `close`, `pct_vs_200MA`, `adv20_shares`, `hv20_pct`, `market_cap` with `is_etf`/`total_assets` (gate 2 admits a large cap **or** a broad-based ETF — funds report AUM, not market cap, so a null `market_cap` on an ETF is not a failure; sector ETFs are also not "broad-based", judge that explicitly), `balance_sheet_ok` (gate 8 — null means UNRESOLVED, and ETFs have no meaningful FCF/net-cash reading so the gate does not apply to them), `sector` (portfolio concentration), `next_earnings_date`, `ex_dividend_date` with `ex_dividend_is_future` (when that flag is false the stored date is the LAST ex-date — estimate the next one from the payout cadence or fetch it, never treat a past date as upcoming), `dividend_yield_pct`. `options-result.json` serves gate 4 and the whole CSP leg via its `wheel` block: the 30–45 DTE expiry with strikes at 0.15/0.20/0.25/0.30 delta, each with bid/ask/mid/OI/IV, `spread_pct_of_mid`, `collateral_usd`, `annualised_yield_pct`, and `gate4_liquidity_ok`. Label `✓ Confirmed (repo snapshot, as of <run_at>)`; stale >2 trading days → refetch.
2. **Live chain if credentials exist.** With `TT_REFRESH_TOKEN` / `TT_CLIENT_SECRET` set, run `python3 "Investment Analysis/tastytrade_fetch.py" TICKER` for live quotes and Greeks; label `✓ Confirmed (Tastytrade)`.
3. **Web search** for anything the snapshots can't cover: IV rank (see below), binary-event screen (gate 10), M&A/litigation/index-deletion checks, and confirmation of earnings and ex-dividend dates.

**Check `quote_context` first.** If `during_us_rth` and `post_close_settled` are both false, the snapshot was taken outside trading hours: bids/asks may be zero and IV-derived deltas unreliable. Say so, treat every quote as `[Estimated — verify before trading]`, and do not pass gate 4 on that data. A `wheel` block carrying an `error` about an unquoted chain means exactly this — report it, don't work around it.

**Watch the delta match.** Each strike row carries `target_delta`, `delta_gap`, and `delta_match_ok`. When `delta_match_ok` is false the ladder is too coarse to hit the tier band — say so and quote the actual delta rather than implying the target was met. Deltas are Black-Scholes without a dividend adjustment, so they run slightly large on high-yield names.

**The IV rank caveat is material.** Gate 6 needs a real 52-week IVR. `iv-history.json` only accumulates from when the workflow started, so `iv_rank_to_date.sample_size` is small for now — treat it as indicative, state the sample size, and cross-check against the broker before entry. A gate resting on an estimate is UNRESOLVED, never passed.

## Updating the ledger

When the user reports a fill, roll, assignment, or close, update `wheel-positions.json`: recompute `net_basis` (it declines with every credit collected), increment `rolls_this_leg`, and move `leg` through csp → shares → cc. On a closed cycle, remove it from `positions` and append the full record to `Investment Analysis/calibration/wheel_cycles.md`, including the buy-and-hold benchmark the framework's Phase 6 requires. Never edit the workflow-owned JSONs (`crossover-result.json`, `options-result.json`, `iv-history.json`) or `watchlist.json`.

## Output discipline

Analysis and mechanical trade plans only — no financial advice, no guaranteed-income or safe-yield language. "No eligible setup this week" is a valid and frequent result; never soften a gate to manufacture a trade.
