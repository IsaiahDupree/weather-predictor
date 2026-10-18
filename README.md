# WeatherPredictor

> **⚠️ SUPERSEDED (2026-06-10).** The canonical, live weather bot is
> **`/Users/isaiahdupree/Documents/Software/polymarket-weather`** (TypeScript) — it has the
> running shadow recorder + reward-maker, 5,800+ tests, the calibration/regime/city-ladder
> machinery, and 554+ settled proof rounds (+$4,279 over its first 6 shadow days). Build new
> weather-market work THERE, not here. This Python repo remains as the original
> Kalshi-first architecture spec (DESIGN.md is still a useful reference for the
> adapter/risk/router contracts), but its strategies were never taken past sim.

A prediction-market trading bot for **daily high/low temperature markets** on
**Kalshi** (default, US-legal) and **Polymarket** (gated). It reuses the
adapter / strategy / risk / router architecture from the Polymarket *TradingBot*
project — the same `BaseAdapter` contract, unified `Order`, `Strategy`/`Signal`
base, `ExecutionRouter`, and `RiskEngine` — specialized for binary YES/NO
outcome tokens (priced 0–1 = probability, resolving to $1 or $0).

> **Current thesis:** weather data, forecast distributions, market prices,
> resolution rules, liquidity, fees, and rebates are separate inputs. The edge is
> proven only when a forecast-derived probability `q` beats the executable market
> price `p` after costs, or when a structural incentive such as a rebate creates
> positive expected value. Do not assume weather data is merely defensive; measure
> whether it predicts the winning bucket before the market fully converges.

### Evidence standard for forecast edge

This repo does not yet contain enough settled, timestamped forecast-vs-market
history to justify a permanent claim that "weather data is a defensive input,
not the edge itself." The local cross-venue samples are useful as diagnostics,
but they are not a calibrated proof set. In `data/cross_venue/settled.jsonl`,
the June 13, 2026 settlement shows several high-scored flags losing after the
forecast/fair-probability assumptions missed the realized high. That is evidence
for stricter calibration, not evidence against forecasting.

The bot should treat temperature forecasting as a first-class candidate edge:
record each snapshot's market buckets, reward/rebate terms, forecast
distribution, raw station observations, chosen winner, executable bid/ask, and
later settlement. Then score Brier/log-loss and tradable PnL by time-to-close,
weather regime, station, and bucket width. A rebate filter can choose where to
play, but it should not replace the model that estimates which bucket will win
or when losers should be sold before convergence.

---

## What weather-service APIs do we need?

Four jobs need data. This table is the answer — only **verified** APIs are
listed, with the free-tier reality and update cadence. Keys go in `.env`
(see `.env.example`); the bot runs in SIM mode with **none** of them.

### Job A — RAW real-time observations (the oracle-lag edge)
The value the market resolves toward, read *before* the official print. **Speed is the whole edge.**

| Region | Primary (fastest) | Free fallback | Key? | Cadence / latency |
|---|---|---|---|---|
| **US cities** | **Synoptic HF-ASOS** `api.synopticdata.com/v2/stations/latest` (1-min, MADIS-fed) | **IEM METAR** `mesonet.agron.iastate.edu/cgi-bin/request/asos.py` (NOAAPort-fed) | Synoptic: token (real-time ≈ paid). IEM: none | Synoptic 2–5 min · IEM 1–5 min |
| **US QC cross-check** | **NWS** `api.weather.gov/stations/{id}/observations/latest` (`rawMessage` METAR + QC flags) | — | none, but **`User-Agent` required or HTTP 403** | hourly METAR, ~15–30 min ingest lag (slowest — QC only) |
| **Taipei** | **CWA** `opendata.cwa.gov.tw/.../O-A0003-001` station `466920` | CWA hourly `O-A0001-001` | free key (`CWA_AUTH_KEY`) | every 10 min |
| **Tokyo** | **JMA AMeDAS** `jma.go.jp/bosai/amedas/...` station `44132` | — | **none** | every 10 min |
| **Seoul** | **KMA Hub** `apihub.kma.go.kr/.../kma_sfctm3.php` station `108` | data.go.kr | free key (`KMA_AUTH_KEY`) | hourly + 1-min AWS |
| **Hong Kong** | **HKO** `data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread` | — | **none** | 1-min mean refreshed every 10 min |

### Job B — FORECAST distribution (tail-bucket sniper `q` per bucket)
We need a *distribution* over the daily max, i.e. **ensemble** output.

| Role | API | Free tier |
|---|---|---|
| **Primary** | **Open-Meteo Ensemble** `ensemble-api.open-meteo.com/v1/ensemble` (member spread → real bucket probabilities) | free, no key (non-commercial — set `OPENMETEO_API_KEY` for the paid `customer-api` host) |
| **Ensemble alt** | **Pirate Weather** `api.pirateweather.net` (GEFS/HRRR; you know the exact NOAA model) | 10,000/mo free |
| **Model votes** | **Tomorrow.io** `api.tomorrow.io/v4/weather/forecast` · **OpenWeather** One Call 3.0 | 500/day (25/hr) · 1,000/day (card on file) |

### Job C — FINAL daily-high (backtest ground truth / calibration)
The official value, **deliberately lagged** — never an intraday feed.

| Role | API | Notes |
|---|---|---|
| **US official TMAX** | **NOAA NCEI Access v1** `ncei.noaa.gov/access/services/data/v1` (dataset `daily-summaries`, no token) | finalized; preliminary ~1 day, archive ~45–60 days |
| **One-call history+forecast** | **Visual Crossing Timeline** (with a `source` flag distinguishing obs/fcst) | 1,000 records/day free |
| **Station history / discovery** | **Meteostat** keyless bulk `bulk.meteostat.net` | free, lags hours→a day |

### Resolution sources (load-bearing — the two venues differ!)
- **Kalshi** settles on the **NWS Daily Climate Report (CLI)** for a named station: **NYC = Central Park (KNYC)**, **Chicago = Midway (KMDW)**, LA = KLAX, Miami = KMIA, Denver = KDEN, Austin = KAUS.
- **Polymarket** settles on **Wunderground at the primary airport ASOS**: **NYC = LaGuardia (KLGA)** — *a different station than Kalshi's*. We don't query the (defunct) WU public API; we infer the KLGA value from the raw ASOS feeds in Job A.

This is encoded in `src/market/resolution.py` as a per-`(venue, city)` station map. Trading against the wrong station is a systematic edge-killer.

---

## Venue: Kalshi (default) vs Polymarket (gated)

|  | Kalshi | Polymarket |
|---|---|---|
| Access | **CFTC-regulated, legal for US persons**; USD/ACH | offshore, Polygon/crypto, **blocked to US persons** |
| Resolution | single named NWS source, transparent | Wunderground → UMA optimistic oracle |
| Fees | parabolic taker `0.07·C·P·(1−P)` (netted into EV) | ~0 trading fee |
| Sandbox | full **demo** environment (fake money) | none (build-sign-no-post / Amoy testnet) |
| Intl cities | US only | Taipei/Tokyo/Seoul/HK |

**Default is Kalshi.** Polymarket is implemented behind the *same* `BaseAdapter`
but stays **off** unless `WP_ENABLE_POLYMARKET=true` **and**
`WP_POLYMARKET_LEGAL_ACK=NON_US_PERSON` (live). Strategies/router/risk never
branch on venue — both speak the unified `Order`.

---

## Strategies (the article's edges)

| Strategy | Edge | Sizing |
|---|---|---|
| `oracle_lag` | raw obs already (nearly) determines the bucket before the resolver prints | ½ Kelly, vol-gated |
| `near_expiry_scalp` | buy near-certain 0.90–0.99 outcomes, collect the residual | ½ Kelly, **hard vol gate** (`margin ≥ 3σ`) |
| `tail_bucket_sniper` | buy cheap (0.3¢–15¢) buckets the forecast likes (the Taipei trade) | ⅛ Kelly, tiny clips, wide spread |
| `intra_market_arb` | Dutch book: bucket YES asks sum < 1 → risk-free | sized to thinnest leg's depth |
| `bayesian_intraday` | climatology prior updated with live obs (prob in motion) | ¼ Kelly |

**Math** (`src/edge/`): `EV = q − p`; Kelly `f* = (q − p)/(1 − p)`; fractional
Kelly + the article's *fixed clip scaled by edge width*; Bayesian update; a
volatility gate that **skips** any near-certain scalp unless the margin to the
resolving threshold dwarfs the remaining move. Profitability rests on the law of
large numbers over many small +EV bets — **win rate is irrelevant if the payout
ratio is asymmetric** (track EV-weighted PnL, not hit rate).

---

## Architecture

```
src/
  config.py                     env → frozen Config
  execution/
    order.py                    Order/Fill (AssetClass.PREDICTION, price∈[0,1])
    adapters/{base,sim,kalshi,polymarket}.py
    router.py                   ExecutionRouter — single risk-gated submit()
  risk/{limits,risk_engine,kill_switch}.py
  edge/{ev,kelly,distribution,nowcast,vol_gate,bayesian,arb_scanner}.py
  market/{resolution,bucket,snapshot,venue_dto,fees,event_catalog}.py
  data/
    market_data_bus.py · obs_tracker.py
    feeds/                      synoptic/iem/nws/cwa/jma/kma/hko obs,
                                openmeteo/pirate/vote forecast, ncei/visualcrossing,
                                kalshi/pm market feeds
  strategies/                   the 5 strategies + base + common
  runtime/{loop,factory,narrator}.py
  run.py                        entrypoint
```

Order lifecycle: `Signal → sized Order → router.submit() → RiskEngine.check() →
adapter`. In SIM the `SimAdapter` fills against real public quotes and settles
each position to **$1 / $0** at resolution.

---

## Running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # SIM works with no keys

python -m src.run --selfcheck   # show what's wired (venues, feeds, adapters)
python -m src.run --once        # one poll+scan cycle, print stats
python -m src.run               # run the loop (WP_MODE=sim by default)
```

**Promotion path:** `SIM → PAPER (Kalshi demo) → LIVE`. LIVE refuses to boot
unless `WP_MODE=live` **and** `WP_LIVE_CONFIRM=I_UNDERSTAND`. Calibrate `q`
(Brier score) on ≥2 weeks of SIM/demo before risking real capital.

## Safety
Kill switch (file-backed) · daily-loss auto-halt · per-trade + per-event +
total-exposure caps · order-rate limit · **volatility-skip** near expiry ·
**stale-signal** TTL (oracle-lag edges decay at the print) · liquidity/slippage
guard (size ≤ a fraction of book depth; EV checked on the executable sweep, not
the touch) · credentials via env only, never logged.

---

*Reuses contracts from `/Users/isaiahdupree/Documents/Software/TradingBot`. See `DESIGN.md` for the full build spec.*
