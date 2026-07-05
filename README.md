# Trading Research Hub

One repo wiring together three layers of stock-market study. `CLAUDE.md` holds the
strict file-ownership rules; `watchlist.json` is the single source of truth for
which tickers each layer covers.

## Layer 1 — automated data (GitHub Actions)

| Workflow | Schedule | Script | Output |
|----------|----------|--------|--------|
| `MA Crossover` | 21:00 UTC weekdays | `scripts/crossover.py` | `crossover-result.json` — MAs (20/50/200), true golden/death-cross events, ATR20, ADV20, volume ratio, next earnings date |
| `Options Chain` | 20:45 UTC weekdays | `scripts/options_snapshot.py` | `options-result.json` — ATM IV term structure, term-structure ratio, Volume/OI put-call ratios, skew proxy, max pain, top-5 OI strikes; also appends to `iv-history.json` to build an IV rank over time |

Both read the tickers flagged `"technicals": true` in `watchlist.json` and commit
results to the branch they run on (main, once merged).

## Layer 2 — automated narrative (scheduled Claude Routines)

- **Tech-Sector Macro Digest** (`Routines_instructions/Tech-Sector_Macro_Digest.md`)
  — ranks sector-level macro news vs QQQ; state in `digest-state.json`.
- **Stock News Digest** (`Routines_instructions/Stock_news_digest_routine.md`)
  — per-ticker news ranking for tickers flagged `"news": true`; state in
  `stock-digest-state.json`. Reads the Layer-1 JSONs (read-only) to add a
  TECHNICALS line per ticker and a closing **STRATEGY TRIAGE** that flags which
  Layer-3 playbook is worth running (earnings straddle, same-day IV ramp,
  volume/breakout check, options-sentiment read).

The `.md` files here are the source documents — paste changes into the Routine
configs when you edit them.

## Layer 3 — on-demand deep dives (Claude Code skills / Claude Projects)

Four analyst frameworks in `Investment Analysis/`, each usable two ways: as a
skill in Claude Code inside this repo (Claude fetches its own data and persists
the calibration log) or standalone in Claude Projects (paste-a-data-block).

| Skill | Framework | Companion script |
|-------|-----------|------------------|
| `/options-sentiment TICKER` | `options_sentiment_analyst_system_prompt.md` | — |
| `/straddle TICKER` | `long_straddle_analyst_system_prompt.md` + `long_straddle_reference.md` | `tastytrade_fetch.py` |
| `/iv-ramp TICKER --phase` | `iv_ramp_harvest_system_prompt.md` | `iv_ramp_harvest.py` |
| `/volume TICKER` | `volume_analyst_system_prompt.md` | — |

Calibration logs live in `Investment Analysis/calibration/` — hit rates and
threshold overrides accumulate there instead of being lost in chat history.

Tastytrade scripts need `TT_REFRESH_TOKEN` and `TT_CLIENT_SECRET` in the
environment; without them the skills fall back to repo snapshots + web search.

Nothing in this repo is investment advice; all outputs are analysis of likely
price impact and framework checks only.
