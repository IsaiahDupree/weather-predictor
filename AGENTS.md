# WeatherPredictor — Codex project context

Prediction-market trading bot for daily-temperature markets on **Kalshi**
(default, US-legal) and **Polymarket** (gated, off by default). Reuses the
adapter/strategy/risk/router architecture from
`/Users/isaiahdupree/Documents/Software/TradingBot` (contracts reproduced
locally so this is a standalone repo).

## Core idea
Trade `EV = q − p` on binary YES/NO outcome tokens (price ∈ [0,1] = probability,
resolves $1/$0). Forecasting is a first-class candidate edge, not just a
defensive input: prove it by comparing forecast-derived `q` against executable
market prices and settlements after fees/rebates. See `README.md` for the
evidence standard and `DESIGN.md` for the full build spec.

## Layout (package root = `src/`, imports use `src.` prefix)
- `config.py` — env → frozen `Config` (`get_config()`). Mode default = **sim**.
- `execution/` — `order.py` (Order/Fill, `AssetClass.PREDICTION`), `adapters/` (base, sim, kalshi, polymarket), `router.py` (single risk-gated `submit()`).
- `risk/` — `limits.py`, `risk_engine.py` (gates incl. VOL_GATE/STALE_SIGNAL/LIQUIDITY/EVENT_CONCENTRATION + daily-loss auto-halt), `kill_switch.py` (file-backed).
- `edge/` — `ev`, `kelly`, `distribution`, `nowcast`, `vol_gate`, `bayesian`, `arb_scanner` (pure functions).
- `market/` — `resolution.py` (per-(venue,city) **station map** — Kalshi NYC=KNYC, Polymarket NYC=KLGA), `bucket.py`, `snapshot.py` (MarketSnapshot), `venue_dto.py` (MarketFeed contract), `fees.py`, `event_catalog.py`.
- `data/` — `market_data_bus.py` (ObsTick/PriceTick), `obs_tracker.py`, `feeds/` (7 obs + 3 forecast + 2 backtest + 2 market feeds).
- `strategies/` — oracle_lag, near_expiry_scalp, tail_bucket_sniper, intra_market_arb, bayesian_intraday (+ base, common).
- `runtime/` — `loop.py` (scan→edge→size→risk→submit), `factory.py` (lazily wires concrete leaves), `narrator.py`.
- `run.py` — `python -m src.run [--selfcheck|--once]`.

## Conventions / rules
- **No mocks.** Real endpoints, real flows. Tests use local aiohttp servers + in-memory stores, never the network.
- All prices are probabilities in [0,1]. Kalshi adapter converts to integer cents; Polymarket snaps to tick.
- Credentials via env only (`.env`, see `.env.example`); never logged (`Config.redacted()`, replay-log redaction).
- LIVE mode refuses to boot without `WP_MODE=live` + `WP_LIVE_CONFIRM=I_UNDERSTAND`. Polymarket needs `WP_ENABLE_POLYMARKET=true` + `WP_POLYMARKET_LEGAL_ACK=NON_US_PERSON`.
- Promotion path: SIM → PAPER (Kalshi demo) → LIVE. Calibrate `q` (Brier) before live.
- Run `python -m src.run --selfcheck` to verify wiring after changes.

## Quick checks
```bash
python -m py_compile $(git ls-files 'src/*.py')   # or: python -m compileall src
python -m src.run --selfcheck
pytest -q
```
