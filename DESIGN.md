# WeatherPredictor — Build Design (Lead Architect Spec)

**Target path:** `/Users/isaiahdupree/Documents/Software/WeatherPredictor`
**Status:** Design for implementation. Do NOT write code from this doc beyond what each section specifies; implement file-by-file in the order of Section 4.
**Reuses:** TradingBot's adapter / strategy / risk / router architecture (the BaseAdapter contract, Order/Fill dataclasses, Strategy + Signal base, ExecutionRouter, RiskEngine, websocket feed pattern). These are reproduced locally (not imported) so WeatherPredictor is a standalone repo, but the contracts match 1:1 so engineers who know TradingBot are immediately productive.

**Date context:** 2026-05-30. Polymarket CLOB V2 is live (since 2026-04-28); all V1 paths are dead. Kalshi fee schedule effective 2026-02-05.

---

## 0. What this bot does (one paragraph)

WeatherPredictor trades **daily high/low temperature prediction markets** (bucketed, mutually-exclusive binary YES/NO contracts) on Polymarket and Kalshi. It reads **raw real-time station observations** ahead of the official finalized daily-high value that the market resolves on (the *oracle-lag edge*), prices **ensemble forecast distributions** to find mispriced tail buckets, scalps near-certain near-expiry contracts under a strict volatility gate, and harvests **risk-free intra-market arbitrage** (Dutch books) when a bucket set's YES asks sum below 1. Every order flows through the same gated `ExecutionRouter` → `RiskEngine` path as TradingBot, with prediction-market-appropriate limits and asymmetric small-clip sizing.

---

## 1. Weather API recommendation (decision table)

Four jobs need data. Only **verified** APIs appear below; corrections from the verification verdicts are folded in. Every cell gives **primary + fallback**, free-tier reality, and update cadence.

### Job A — RAW real-time observations (the oracle-lag / near-expiry / Bayesian edge feed)
This is the value the market *will* resolve toward, read **before** the official finalized print. Speed is the entire edge.

| Region | Primary (speed) | Fallback (free / cross-check) | Cadence | Free tier reality |
|---|---|---|---|---|
| **US cities** (NYC, LA, Chicago, Denver, Houston, Austin, Atlanta, Miami, SF, Seattle, Dallas, Philadelphia) | **Synoptic HF-ASOS** (`https://api.synopticdata.com/v2/stations/latest` + `/timeseries`), 1-minute MADIS-fed stream, **2–5 min latency**, token auth | **IEM standard METAR feed** (`https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py`), NOAAPort-fed raw METAR in **1–5 min**, no key | Synoptic 1-min; IEM near-real-time METAR | Synoptic free paths are **.edu Open Access** (region-scoped, manual approval) or a **14-day trial**; real-time 1-min HF-ASOS is effectively **paid** — budget for it. IEM is fully free, **1 req/sec/IP** throttle, no monthly cap. |
| **US — free fallback / QC cross-check** | **NWS api.weather.gov** `/stations/{id}/observations/latest` (returns `rawMessage` METAR + per-field `qualityControl` flags) | — | Hourly METAR + SPECI; current obs refresh ~every 5 min, **~15–30 min ingest latency** (the old 1hr+/2.5–5hr lag was the 2024 degraded state, **resolved Aug 2025**) | Free, no key. **Required self-identifying `User-Agent`** (format `(app-or-domain, contact@email)`); missing/invalid UA → **HTTP 403**. Rate-limit also returns **403** (not 429) with a Reference ID. Validate with `Last-Modified`/`If-Modified-Since` (ETag is **not** documented). Slowest raw source — use for QC flags + cross-check, **not** to beat the clock. |
| **Taipei** | **Taiwan CWA** dataset **O-A0003-001** (`https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001`), station **466920** (臺北/Taipei) | NWS-equivalent n/a; use CWA hourly **O-A0001-001** | O-A0003-001 **every 10 min**; O-A0001-001 hourly | Free key, register at `opendata.cwa.gov.tw/user/authkey`, key prefix `CWB-`/`CWA-`. **Correction:** the key works as **either** a `?Authorization=` query param **OR** an HTTP `Authorization` header (the "Not a header" claim was refuted by the official manual). No published hard daily quota; cache. |
| **Tokyo** | **JMA AMeDAS** (`https://www.jma.go.jp/bosai/amedas/data/latest_time.txt` → `/map/{ts}.json` or `/point/{stationNo}/{date}_{HH}.json`), station **44132** (Otemachi) | NWS-equivalent n/a | **Every 10 min**, ~10–20 min latency | **No auth, no key.** Static JSON on S3/CloudFront, `cache-control: max-age=60`, open CORS. Unofficial feed — paths may change; values are `[value, qcCode]` arrays (parse index 0). Times in JST (+09:00). Best free oracle-lag candidate. |
| **Seoul** | **KMA API Hub** `kma_sfctm3.php` (`https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php?stn=108&...&authKey=...`), or **AWS minute** `awsh.php` for 1-min | data.go.kr legacy `apis.data.go.kr/1360000/...` (serviceKey) | ASOS hourly; **AWS 1-min** | Free `authKey` (query param), register at `apihub.kma.go.kr`. ~10,000/day on data.go.kr dev tier (raisable to ~100k). Korean-first docs. Station **108** = Seoul. |
| **Hong Kong** | **HKO Open Data** `rhrread` (`https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en`), filter `temperature.data[].place == "Hong Kong Observatory"`; or "latest 1-minute mean air temperature" dataset | — | rhrread **hourly + on update**; 1-min mean **every 10 min** | **No auth, no key.** Free, no published quota. Times HKT (+08:00). |

**Optional true-upstream note:** MADIS is the firehose Synoptic taps, but its netCDF + Fortran-API + LDM/OPeNDAP + application-approval overhead makes direct ingest impractical. **Consume MADIS via Synoptic HF-ASOS**, not directly.

### Job B — FORECAST distribution (tail-bucket sniper `q` per bucket)
We need a **probability distribution over daily Tmax/Tmin**, not a single number. That means **ensemble** output.

| Role | API | Why | Free tier / cadence |
|---|---|---|---|
| **Primary forecast engine** | **Open-Meteo** Forecast + **Ensemble API** + Previous-Runs API (`api.open-meteo.com/v1/forecast`, ensemble + previous-runs hosts) | Free, no key, 16-day horizon, multi-model, and the **Ensemble API exposes member spread** → real distribution over `temperature_2m_max/min` (exactly what prices tail buckets). | Free host is **non-commercial** (limits **600/min, 5,000/hr, 10,000/day**, hard **429** on overage). **A real-money bot is arguably commercial → use the paid `customer-api.open-meteo.com?apikey=` host (Standard EUR 29/mo, ~1M calls/mo).** Per-model cadence (GFS/HRRR hourly, ECMWF 6h). |
| **Transparent ensemble alt** | **Pirate Weather** (`api.pirateweather.net/forecast/{key}/{lat},{lon}`) | Literally GFS/HRRR/NBM/**GEFS** — you know the exact NOAA model; exposes ensemble-style spread; strong 3km HRRR over US cities. | **Correction:** free tier is **10,000 calls/mo**; **20,000/mo is the paid ($2/mo donation) tier**, NOT an automatic free raise. Hobby project, best-effort uptime. |
| **Independent model "vote" cross-check** | **Tomorrow.io** (`api.tomorrow.io/v4/weather/forecast`) and/or **OpenWeatherMap One Call 3.0** (`api.openweathermap.org/data/3.0/onecall`) | Proprietary blends; when they diverge from Open-Meteo it flags a possibly-mispriced bucket. | Tomorrow.io free **500/day but binding 25/hr**. OWM **1,000/day** free but **credit-card-on-file required** even free; must manually lower default 2,000 cap to 1,000 to avoid charges. No ensemble decomposition on either. |

### Job C — FINAL daily-high (backtest ground truth + settlement reconstruction)
The official value the market resolves toward, used to validate `q` calibration and backtest — **deliberately lagged**, never an intraday feed.

| Role | API | Free tier / cadence |
|---|---|---|
| **US official daily TMAX/TMIN** | **NOAA NCEI** — prefer **Access Data Service v1** (`https://www.ncei.noaa.gov/access/services/data/v1`, dataset `daily-summaries` = GHCND, **no token**); CDO v2 (`/cdo-web/api/v2/`, free email token in `token` header, **5 req/s, 10,000/day**) as alt | **Finalized/lagged:** preliminary ~1 day, reconstructed each weekend from **>25 sources**, archive-ready ~**45–60 days** after month-end. Match the market's exact day-boundary. |
| **One-call backtest (history + forecast + source flag)** | **Visual Crossing Timeline** (`weather.visualcrossing.com/.../timeline/{loc}/{d1}/{d2}?key=`) — returns real station-obs history + 15-day forecast with a `source` flag distinguishing obs/fcst/stats | **Correction:** a forecast query (daily OR hourly) = **1 record**, not ~24/day; the ~24/day cost applies only to **historical hourly** queries. Free **1,000 records/day**, then $0.0001/record. **Must read the `source` field** or you calibrate on the wrong data. |
| **Per-station history + station discovery** | **Meteostat** — use the **free keyless bulk CSV** (`bulk.meteostat.net` + `data.meteostat.net`) / Python lib for backfills, **not** the 500-req/month RapidAPI tier | Bulk is free/unlimited/no key. Observation-based, lags hours→~1 day. Use `/stations/nearby` to identify the exact settlement station; **verify it equals the market's named station**. |

### Critical resolution-source corrections (load-bearing)
- **Polymarket** names **Wunderground** at the city's primary airport ASOS station (NYC = **KLGA/LaGuardia**) as the *data source of record*, value taken **whole degrees F, once finalized, revisions ignored**. The general research claim "Wunderground resolves Polymarket weather markets is mostly inaccurate" was about the **dead public WU API** — it does **not** contradict the per-market rules, which **do** name Wunderground. We do not query the dead WU API; we infer the KLGA value from the raw ASOS feeds in Job A and treat Wunderground's KLGA history page as the human-readable reference.
- **Kalshi** resolves on the **NWS Daily Climate Report (CLI)** for a named station — **NYC = Central Park (KNYC), NOT LaGuardia**; **Chicago = Midway (MDW), NOT O'Hare**; LA = LAX; Miami = MIA; Austin = Austin-Bergstrom (AUS); Denver = DEN. **The two venues use different NYC stations.** This drives a per-(venue,city) station map in config — forecasting against the wrong station is a systematic edge-killer.
- **Climate-day boundary (Kalshi):** local **LST** 24h window; under DST this appears on the wall clock as **1:00 AM–12:59 AM local next day**. Match this when computing "daily high so far".

---

## 2. Venue recommendation — Polymarket vs Kalshi

| Dimension | Polymarket | Kalshi |
|---|---|---|
| **Legal / access** | Offshore, Polygon/crypto (pUSD), **blocked to US persons**. Needs a funded Polygon wallet + on-chain allowances. | **CFTC-regulated DCM, legal for US persons.** USD bank/ACH/debit/wire (+ USDC now). KYC required. |
| **Resolution transparency** | Wunderground data source → **UMA Optimistic Oracle V2** (proposer bond ~$750 pUSD, ~2h dispute window, DVM escalation). Dispute/payout can lag. | **Single named government source** (final NWS CLI), named station per contract. Cleaner, but "final" is really *value-at-expiration*; revisions after expiration ignored; delay-to-11AM-ET on METAR inconsistency. |
| **Fees** | ~**0 trading fees** (gas in POL for allowances/settlement; trades gasless via relayer). | **Real fees:** taker `roundup(0.07·C·P·(1−P))`, maker `roundup(0.0175·C·P·(1−P))`, resting maker orders often **fee-exempt**. Parabolic, peaks at P=0.50 — **destroys edge on cheap high-frequency tail scalping**; always net fees into EV. |
| **Liquidity** | Deeper on popular US-city temp events; 150+ temp markets incl. many international cities (Taipei, Tokyo, Seoul, HK). | Thinner on tail brackets / far-out dates; brackets are 2°F interior + open-ended edges. |
| **API ergonomics** | Read: Gamma (`gamma-api.polymarket.com`) + CLOB (`clob.polymarket.com` `/book` `/midpoint` `/price` `/spread` `/tick-size`) + WS. Trade: **use the unified `Polymarket/py-sdk`** (the README of `py-clob-client-v2` itself steers new projects to py-sdk; legacy `py-clob-client` is archived/dead). Neg-risk multi-outcome → `neg_risk=true`. **No native dry-run** (build/sign without posting, or Amoy testnet chain 80002). | Read endpoints **public, no auth**. Trade: RSA-PSS+SHA256 signing; official SDKs **`kalshi_python_sync` / `kalshi_python_async`** (old `kalshi-python` email/password auth deprecated). **Full demo/sandbox** (`external-api.demo.kalshi.co` / `demo-api.kalshi.co`) with fake money. `client_order_id` idempotency. |
| **Currency/version traps** | **V2 only:** pUSD collateral (`0xC011...82DFB`), CTF Exchange `0xE111180000d2663C0091e4f400237545B87B996B`, Neg Risk CTF Exchange `0xe2222d279d744050d28e00520010520000310F59`, Neg Risk Adapter `0xd91E80...35296`, CTF `0x4D97...476045`. Approve allowances on the **new** contracts. `signature_type` 0=EOA,1=Magic/proxy,2=Gnosis-safe (funder = proxy address for 1/2; new accounts may default to type 3). | Recommended hosts now **`external-api.kalshi.com`** (prod) / **`external-api.demo.kalshi.co`** (demo); `api.elections.kalshi.com` / `demo-api.kalshi.co` still supported (legacy). Sign string = `timestamp_ms + METHOD + PATH` (PATH includes `/trade-api/v2`, **excludes** query string). |

**Default venue: Kalshi.** Rationale: legal for a US person (the operator), clean single-source resolution, a real sandbox for paper trading, and simpler auth. **Cost:** real fees + thinner tails. **Polymarket is the second adapter** for fee-free fills, deeper liquidity, and international cities (Taipei/Tokyo/Seoul/HK) — gated behind an explicit `WP_ENABLE_POLYMARKET=true` + legality acknowledgement (see Open Questions). **Both sit behind one `BaseAdapter` interface**, so strategies/router/risk never branch on venue; they speak the unified `Order`.

---

## 3. Architecture — module map (mirrors TradingBot)

```
WeatherPredictor/
├── src/
│   ├── execution/
│   │   ├── order.py              # Order + Fill (+ AssetClass.PREDICTION); binary YES/NO mapping
│   │   ├── adapters/
│   │   │   ├── base.py           # BaseAdapter contract (reproduced from TradingBot)
│   │   │   ├── sim.py            # SimAdapter — internal matching engine for sim mode
│   │   │   ├── kalshi.py         # KalshiAdapter (default)
│   │   │   └── polymarket.py     # PolymarketAdapter (gated)
│   │   └── router.py             # ExecutionRouter — single async submit() gate
│   ├── data/
│   │   ├── market_data_bus.py    # normalized tick bus (obs + market prices)
│   │   └── feeds/
│   │       ├── base_feed.py      # async-generator feed contract
│   │       ├── synoptic_obs.py   # US raw 1-min HF-ASOS (primary edge feed)
│   │       ├── iem_obs.py        # US near-real-time METAR (free fallback)
│   │       ├── nws_obs.py        # US QC cross-check + rawMessage METAR
│   │       ├── cwa_obs.py        # Taipei (466920)
│   │       ├── jma_obs.py        # Tokyo (44132)
│   │       ├── kma_obs.py        # Seoul (108)
│   │       ├── hko_obs.py        # Hong Kong (HKO HQ)
│   │       ├── forecast_openmeteo.py   # ensemble distribution (primary)
│   │       ├── forecast_pirate.py      # ensemble alt / model vote
│   │       ├── forecast_vote.py        # Tomorrow.io / OWM cross-check
│   │       ├── ncei_final.py     # final TMAX ground truth (backtest)
│   │       ├── visualcrossing_hist.py  # one-call history+forecast (backtest)
│   │       ├── pm_market_feed.py # Polymarket Gamma+CLOB+WS price feed
│   │       └── kalshi_market_feed.py   # Kalshi markets/orderbook+WS price feed
│   ├── edge/
│   │   ├── ev.py                 # EV_yes = q - p; per-contract economics
│   │   ├── kelly.py              # f* = (q-p)/(1-p); fractional + fixed-scaled-by-edge
│   │   ├── distribution.py       # ensemble members -> bucket prob vector q[]
│   │   ├── bayesian.py           # climatology prior + intraday nowcast posterior
│   │   ├── nowcast.py            # mu = T_now + r*h, sigma from residual-window vol
│   │   ├── vol_gate.py           # margin_to_threshold >= k*sigma_remaining (k>=3)
│   │   └── arb_scanner.py        # sum(YES asks) < 1 - fees Dutch-book detector
│   ├── strategies/
│   │   ├── base_strategy.py      # Strategy(abc) + Signal (reproduced)
│   │   ├── oracle_lag.py
│   │   ├── near_expiry_scalp.py
│   │   ├── tail_bucket_sniper.py
│   │   ├── intra_market_arb.py
│   │   └── bayesian_intraday.py
│   ├── risk/
│   │   ├── limits.py             # RiskLimits (prediction-market values)
│   │   ├── risk_engine.py        # check() gate (reproduced + new codes)
│   │   └── kill_switch.py
│   ├── market/
│   │   ├── resolution.py         # per-(venue,city) station + day-boundary map
│   │   ├── bucket.py             # bucket grid model, edge/open-ended buckets
│   │   └── event_catalog.py      # discover/refresh open temp events per venue
│   ├── runtime/
│   │   ├── loop.py               # scan -> edge -> size -> risk -> submit
│   │   ├── scheduler.py          # poll cadences per feed; force-expire stale signals
│   │   └── narrator.py           # structured logging / replay
│   ├── config.py                 # env loading, venue/city/mode config
│   └── __init__.py
├── data/
│   ├── order_events.jsonl        # replay log (router)
│   └── climatology/              # cached station climatology for priors
├── tests/                        # real aiohttp test servers + in-memory stores
├── .env.example
├── requirements.txt
├── CLAUDE.md
└── DESIGN.md (this file)
```

### `AssetClass.PREDICTION` and the binary mapping

Add `PREDICTION` to the `AssetClass` enum. A binary YES/NO bucket maps onto the unified `Order` as:

| Order field | Prediction-market meaning |
|---|---|
| `symbol` | Venue-native instrument id. Kalshi: market **ticker** (e.g. `KXHIGHNY-26MAY30-B70.5`). Polymarket: **clobTokenId** (the YES token; `clobTokenIds[0]`). |
| `side` | `BUY` = buy the YES (or NO) outcome token. To take the "NO" view on Kalshi we set `side='no'` in metadata and `BUY`; on Polymarket we buy the NO token id. Internally normalize to **"buy YES of outcome X"**. |
| `qty` | Number of **shares/contracts** (Kalshi `count`, whole integers; Polymarket shares). |
| `limit_price` | Price in **[0,1]** (= probability). Kalshi adapter converts to integer **cents 1–99** (`yes_price`/`no_price`); Polymarket keeps decimal, snapped to tick. |
| `order_type` | `LIMIT`/`MARKET`. Kalshi: market = omit price + IOC/FOK; Polymarket: GTC/GTD/FOK/FAK. |
| `time_in_force` | maps to GTC/GTD/FOK/FAK (PM) or good_till_canceled/IOC/FOK (Kalshi). |
| `broker` | `"kalshi"` / `"polymarket"`. |
| `mode` | `SIM` / `PAPER` (Kalshi demo / PM Amoy or build-sign-no-post) / `LIVE`. |
| `client_order_id` | UUID idempotency — passed through to Kalshi `client_order_id`; PM uses it as our dedup key in the router. |
| `metadata` | `{event_ticker, bucket_label, outcome:'yes'|'no', token_id_no, tick_size, neg_risk, resolution_station, climate_day, q_at_entry, ev_at_entry, strategy}`. |
| `capsule_id` | reserved (kept for parity with TradingBot router sizing/rescale). |

**Resolution payout:** a winning share pays **$1** (Kalshi) or **$1 pUSD** (Polymarket); losers pay $0. `Fill` records entry price; PnL at resolution = `(1 - entry) * qty` if won else `-entry * qty`. The SimAdapter's matching engine and a settlement journal compute this.

---

## 4. Per-module file list (dependency order — build top to bottom, no merge conflicts)

**Tier 0 — primitives (no internal deps)**
1. `src/config.py` — load env, expose `Config` (mode, venues, cities, bankroll, thresholds, API keys). No deps.
2. `src/execution/order.py` — `Side`, `AssetClass(+PREDICTION)`, `OrderType`, `TimeInForce`, `Mode`, `Order`, `Fill`, `to_dict()`, `add_fill()`. No deps.
3. `src/strategies/base_strategy.py` — `Strategy(abc)`, `Signal`, `compute_position_size()` (fixed_fraction + half-Kelly). No deps.
4. `src/data/feeds/base_feed.py` — `BaseFeed` async-generator contract: `stream()`, `latest()`, `health()`. No deps.
5. `src/data/market_data_bus.py` — normalized in-memory pub/sub bus for obs ticks + market-price ticks. Deps: none.

**Tier 1 — domain models**
6. `src/market/resolution.py` — `ResolutionSpec` per (venue, city): station id, source name, day-boundary rule (LST/DST), precision. The single source of truth for "which station, which day". Deps: config.
7. `src/market/bucket.py` — `Bucket` (label, lo, hi, open-ended flag), `BucketGrid`, `which_bucket(temp)`. Deps: none.
8. `src/risk/limits.py` — `RiskLimits` dataclass with prediction-market values (Section 6). Deps: none.
9. `src/risk/kill_switch.py` — file/flag-backed kill switch, `is_tripped()`, `trip(reason)`. Deps: none.

**Tier 2 — edge math (pure functions, unit-testable in isolation)**
10. `src/edge/ev.py` — `ev_yes(q,p)`, `ev_no(q,p)`, `executable_cost(book, qty)` (full-sweep, not touch). Deps: none.
11. `src/edge/kelly.py` — `kelly_fraction(q,p,side)`, `fractional(f, lam)`, `fixed_scaled(ev, T, W, clip, cap_mult)`. Deps: ev.
12. `src/edge/distribution.py` — `ensemble_to_bucket_probs(members, grid)` → `q[]`; calibration helpers. Deps: bucket.
13. `src/edge/nowcast.py` — `project(T_now, trend, hours)` → `(mu, sigma)`; physically-capped. Deps: none.
14. `src/edge/vol_gate.py` — `passes(margin, sigma_remaining, k=3)`. Deps: none.
15. `src/edge/bayesian.py` — `posterior(prior_q, mu, sigma, threshold)` precision-weighted Bayes. Deps: nowcast, distribution.
16. `src/edge/arb_scanner.py` — `scan(bucket_asks, fees)` → Dutch-book opportunity or None (executable, fee-inclusive). Deps: ev.

**Tier 3 — execution contract + adapters**
17. `src/execution/adapters/base.py` — `BaseAdapter` reproduced: `name`, `asset_classes`, `supports_modes`, `is_available()`, async `submit/cancel/cancel_all/flatten/get_positions/health/probe`. Deps: order.
18. `src/execution/adapters/sim.py` — `SimAdapter`: matching engine, fills at ref price, settlement journal computing $1/0 payout. Deps: base, order, market/bucket.
19. `src/execution/adapters/kalshi.py` — `KalshiAdapter`: RSA-PSS signing, cents conversion, demo/live hosts, `client_order_id` idempotency, fee calc. Deps: base, order, config.
20. `src/execution/adapters/polymarket.py` — `PolymarketAdapter`: py-sdk, Gamma token resolution, neg_risk, tick snapping, build-sign-no-post dry-run. Gated by `is_available()` on creds + `WP_ENABLE_POLYMARKET`. Deps: base, order, config.
21. `src/risk/risk_engine.py` — `RiskEngine.check(...) -> RiskCheckResult`; gates incl. new `VOL_GATE`, `STALE_SIGNAL`, `LIQUIDITY`, `EVENT_CONCENTRATION`. Deps: limits, kill_switch.
22. `src/execution/router.py` — `ExecutionRouter`: single async `submit(order, ref_price)`, pre-trade risk gate, idempotency, replay log to `data/order_events.jsonl`, RTT stats, `register_adapter()`, `get_default_router()`. Deps: risk_engine, adapters.

**Tier 4 — feeds (concrete)**
23–29. Weather obs feeds: `synoptic_obs.py`, `iem_obs.py`, `nws_obs.py`, `cwa_obs.py`, `jma_obs.py`, `kma_obs.py`, `hko_obs.py` — each subclasses `BaseFeed`, normalizes to `ObsTick(station, ts, temp_f, source, qc)`, pushes to bus. Deps: base_feed, bus, config.
30–33. Forecast feeds: `forecast_openmeteo.py` (+ ensemble), `forecast_pirate.py`, `forecast_vote.py`, plus backtest-only `ncei_final.py`, `visualcrossing_hist.py`. Deps: base_feed, bus, config.
34. `src/data/feeds/kalshi_market_feed.py` — public markets/orderbook + WS price stream. Deps: base_feed, bus.
35. `src/data/feeds/pm_market_feed.py` — Gamma events + CLOB book/midpoint + WS market channel. Deps: base_feed, bus.

**Tier 5 — market discovery + strategies**
36. `src/market/event_catalog.py` — discover/refresh open temp events per venue; map event→buckets→tokens/tickers; cache. Deps: market feeds, resolution, bucket.
37–41. Strategies (each subclasses `Strategy`, returns `Signal` from `generate_signal`): `oracle_lag.py`, `near_expiry_scalp.py`, `tail_bucket_sniper.py`, `intra_market_arb.py`, `bayesian_intraday.py`. Deps: base_strategy, edge/*, market/*.

**Tier 6 — runtime**
42. `src/runtime/narrator.py` — structured logging + replay. Deps: config.
43. `src/runtime/scheduler.py` — per-feed poll cadence, stale-signal force-expiry. Deps: feeds, config.
44. `src/runtime/loop.py` — main `scan → edge → size → risk → submit` loop wiring catalog + strategies + router. Deps: everything above.

**Tier 7 — tests** (mirror TradingBot: real aiohttp test servers + in-memory stores, **no mocks**)
45. `tests/` — per-module unit tests (edge math, bucket, resolution), adapter integration tests against local aiohttp servers replaying recorded Gamma/CLOB/Kalshi/weather payloads, end-to-end sim-mode loop test, risk-gate tests, vol-gate/arb tests.

---

## 5. Math mapping (how the strategy-math is coded)

**Per-contract economics (in `edge/ev.py`).** Buying YES at price `p` (in [0,1]) with true prob `q`:
```
ev_yes(q, p) = q - p          # = q*(1-p) - (1-q)*p
ev_no(q, p)  = p - q
```
Enter only if `ev > T`, where `T = T_base + fee_roundtrip + spread/2 + model_buffer`. `T_base` and buffers are per-strategy config. **Executable cost** uses the full book sweep (`executable_cost(book, qty)`), not the touch — if the average fill erases EV, the loop drops the order.

**Kelly + fractional (in `edge/kelly.py`).**
```
kelly_fraction(q, p, YES) = (q - p) / (1 - p)   # = EV / amount-at-risk-per-$payoff
kelly_fraction(q, p, NO)  = (p - q) / p
fractional(f, lam)        = lam * f             # lam in [1/8, 1/2]
```
- Oracle-lag & locked near-expiry scalps: `lam` up to **1/2** (high-confidence, near-certain `q`), but **capped at liquidity** (Section 6 LIQUIDITY).
- Tail-bucket sniper: deliberately **1/8–1/4** (extreme variance; tail `q` least reliable); spread across several cheap buckets.
- Bayesian intraday: `lam` scales **inversely with posterior sigma** (more uncertainty → smaller).
- Arbitrage: Kelly ≈ 1 in theory, but **size to the thinnest leg's executable depth**, not bankroll.

**Fixed-scaled-by-edge (the article's "fixed clip scaled by edge", in `edge/kelly.py`).**
```
stake = C * clamp((EV - T) / W, 0, cap_mult)
```
`C` = base small dollar clip, `W` = edge-width normalizer, `cap_mult` caps the multiple. Wider EV gaps → proportionally bigger (capped) clips; every clip stays small. The loop takes `min(fractional-Kelly notional, fixed-scaled notional, liquidity cap, per-trade cap)`.

**Distribution → q (in `edge/distribution.py`).** Take Open-Meteo (or Pirate GEFS) ensemble members' daily Tmax, bin onto the venue's `BucketGrid`, normalize → `q[]` per bucket. Calibrate against NCEI/Visual-Crossing history; trust `q` only for cities with sufficient calibration history.

**Bayesian intraday (in `edge/bayesian.py` + `nowcast.py`).** Prior `q_0` from cached station climatology. Nowcast: `mu = T_now + trend*h` (damped/physically-capped), `sigma` from residual-window realized vol. Posterior `q = Phi((mu - threshold)/sigma)` for a YES-above-threshold view, precision-weighted with the prior. Re-run on every obs tick; flip/exit when `q` crosses back through `p`.

**Volatility skip + asymmetric small-size-high-volume (in `edge/vol_gate.py` + risk).** Before any near-certain (high-`p`) or locked-bucket trade:
```
vol_gate.passes = (margin_to_threshold >= k * sigma_remaining), k >= 3
```
If it fails, **SKIP regardless of apparent EV** (this is the line between safe scalping and pennies-in-front-of-a-steamroller). Asymmetric discipline is encoded as different `lam` and clip caps per strategy (above): tail bets = tiny clips + wide diversification; high-`p` scalps = ironclad vol gate. The bot's profitability rests on the **law of large numbers across many small positive-EV bets**, so the router enforces small clips and the risk engine caps per-event and cross-event-correlated exposure. Track **EV-weighted PnL, not hit rate**.

**Signal contract.** Each strategy's `generate_signal()` returns `Signal(side, strength=clamp(EV/W,0,1), reason, metadata={q,p,ev,kelly_f,vol_gate_pass,executable_cost})`. The loop maps `Signal` → sized `Order` → `router.submit(order, ref_price=market_mid)`.

---

## 6. Safety

- **Mode default = SIM.** Progression `SIM → PAPER → LIVE`, identical `Order` shape. SIM uses `SimAdapter`. PAPER uses **Kalshi demo** (`external-api.demo.kalshi.co`, fake money) and Polymarket **build-sign-no-post** (or Amoy chain 80002). LIVE requires explicit `WP_MODE=live` **and** `WP_LIVE_CONFIRM=I_UNDERSTAND`.
- **Kill switch** (`risk/kill_switch.py`): file-backed, checked first in `RiskEngine.check()` → `HALTED`. Trips on daily-loss breach, repeated adapter errors, or manual.
- **Daily-loss cap** (`DAILY_LOSS`): auto-halt via breach handler (TradingBot pattern). Default **$X/day** (user-set, Open Questions).
- **Per-trade cap** (`ORDER_NOTIONAL` = `max_order_notional`): never exceed a fixed $ per order **and** never exceed a fixed % of bankroll, regardless of Kelly.
- **Liquidity/slippage guard** (new `LIQUIDITY` gate): order size ≤ **25–50% of best-level depth**; cost computed on full sweep; if average fill erases EV → reject. For arbitrage, the `sum<1` condition must hold on **executable, fee-inclusive** prices and **all legs must fill near-simultaneously** or filled legs are immediately unwound.
- **Volatility-skip near expiry** (new `VOL_GATE` gate): reject high-`p`/locked-bucket orders unless `margin >= k*sigma_remaining` (k≥3).
- **Stale-signal force-expiry** (new `STALE_SIGNAL` gate + scheduler): oracle-lag/Bayesian edges decay to zero at the oracle print; signals older than their TTL are dropped before submit.
- **Event-concentration cap** (new `EVENT_CONCENTRATION` gate): cap total exposure per event **and** across correlated weather events (same front/region) — "independent" bets correlate on a single forecast bust.
- **Fee-aware EV (Kalshi):** `T` always includes the parabolic taker fee; prefer resting maker (fee-exempt) orders where the strategy allows.
- **Credentials via env only.** No keys in code/logs. `Order.to_dict()` and the replay log redact secrets. Private keys (Polygon PK, Kalshi RSA PEM) loaded from env/file path, never committed.
- **Idempotency:** every order carries a UUID `client_order_id`; router dedups; Kalshi passes it through.

### RiskLimits (prediction-market starting values, `risk/limits.py`)
```
max_order_notional        = $25         # per-trade cap
max_position_per_event    = $100
max_total_exposure        = 50% of bankroll
max_orders_per_minute     = 30
daily_loss_cap            = user-set (e.g. 10% of bankroll)
min_ev_threshold T_base   = 0.05  (+ fees + spread/2 + model buffer)
vol_gate_k                = 3
liquidity_depth_frac      = 0.33
tail_clip C               = $2          (1/8–1/4 Kelly)
scalp lam                 = 0.5 (gated)
forbidden: any event whose resolution station is not in the (venue,city) map
```

---

## 7. Open questions / decisions for the user

1. **Polymarket legality.** Polymarket is **blocked to US persons**. Are you trading as a non-US person / non-US entity, or should we **ship Kalshi-only** and leave the PolymarketAdapter implemented-but-disabled (`WP_ENABLE_POLYMARKET=false`)? Default recommendation: **Kalshi-only to start.**
2. **Bankroll.** What starting bankroll drives Kelly/clip sizing and the daily-loss cap? (Sizing math is fraction-of-bankroll.)
3. **Cities.** Start with the **US Kalshi set** (NYC=Central Park, Chicago=Midway, LA, Miami, Denver, Austin) or include Polymarket's international cities (Taipei/Tokyo/Seoul/HK) from day one? International cities require the Asian-agency feeds + Polymarket access.
4. **Paper vs live first.** Confirm **SIM → Kalshi-demo PAPER → LIVE** with a minimum paper run length / PnL-calibration gate before any live capital. Recommendation: **2+ weeks demo + calibrated `q` (Brier score) before live.**
5. **Synoptic budget.** The real oracle-lag edge needs **paid Synoptic HF-ASOS** (1-min, 2–5 min latency). Approve the paid plan, or run free IEM-METAR-only (slower edge) initially?
6. **Open-Meteo commercial license.** A real-money bot is arguably commercial — approve the **paid `customer-api.open-meteo.com` plan** (EUR 29/mo) to stay compliant, or accept free-tier non-commercial risk during paper?
7. **Strategy scope for v1.** Ship all 5 strategies, or start with the two safest (**intra-market arbitrage** = risk-free, **near-expiry scalp** = gated) plus **tail-bucket sniper**, and add oracle-lag/Bayesian once feeds are calibrated?

---

## 8. `.env.example`

```dotenv
# ── Runtime ───────────────────────────────────────────────
WP_MODE=sim                       # sim | paper | live
WP_LIVE_CONFIRM=                  # must equal I_UNDERSTAND to enable live
WP_BANKROLL_USD=                  # e.g. 1000
WP_CITIES=nyc,chicago,la,miami,denver,austin
WP_DEFAULT_VENUE=kalshi           # kalshi | polymarket

# ── Risk limits (override defaults) ───────────────────────
WP_MAX_ORDER_NOTIONAL=25
WP_MAX_POSITION_PER_EVENT=100
WP_MAX_TOTAL_EXPOSURE_FRAC=0.5
WP_DAILY_LOSS_CAP_USD=
WP_MIN_EV_THRESHOLD=0.05
WP_VOL_GATE_K=3
WP_LIQUIDITY_DEPTH_FRAC=0.33

# ── Kalshi (default venue) ────────────────────────────────
KALSHI_ENV=demo                   # demo | prod
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PEM_PATH=      # path to RSA private key PEM
# prod host: https://external-api.kalshi.com/trade-api/v2
# demo host: https://external-api.demo.kalshi.co/trade-api/v2

# ── Polymarket (gated, off by default) ────────────────────
WP_ENABLE_POLYMARKET=false
WP_POLYMARKET_LEGAL_ACK=          # must equal NON_US_PERSON to enable
POLYMARKET_PK=                    # Polygon private key (L1 signer)
POLYMARKET_FUNDER=                # proxy wallet address (signature_type 1/2)
POLYMARKET_SIGNATURE_TYPE=1       # 0=EOA, 1=Magic/proxy, 2=Gnosis-safe
POLYMARKET_CHAIN_ID=137           # 137 mainnet | 80002 Amoy testnet (paper)
# Gamma:  https://gamma-api.polymarket.com   CLOB: https://clob.polymarket.com

# ── Weather: RAW real-time obs (oracle-lag edge) ──────────
SYNOPTIC_TOKEN=                   # US HF-ASOS 1-min (primary edge); paid for real-time
IEM_USER_AGENT=weatherpredictor/1.0 (you@example.com)   # free US METAR fallback
NWS_USER_AGENT=weatherpredictor/1.0 (you@example.com)   # REQUIRED or 403; QC cross-check
CWA_AUTH_KEY=                     # Taipei (station 466920); prefix CWB-/CWA-
KMA_AUTH_KEY=                     # Seoul (station 108)
# JMA (Tokyo, station 44132) and HKO (Hong Kong) need NO key

# ── Weather: FORECAST (tail-bucket sniper) ────────────────
OPENMETEO_API_KEY=                # paid customer-api host for commercial use
OPENMETEO_HOST=https://api.open-meteo.com    # or https://customer-api.open-meteo.com
PIRATE_WEATHER_API_KEY=           # ensemble alt / model vote
TOMORROW_IO_API_KEY=              # cross-check vote (500/day, 25/hr)
OPENWEATHERMAP_API_KEY=           # cross-check vote (card-on-file; cap at 1000/day)

# ── Weather: FINAL daily-high (backtest / ground truth) ───
NCEI_CDO_TOKEN=                   # optional; v1 Access service needs no token
VISUALCROSSING_API_KEY=           # one-call history+forecast backtest
# Meteostat backfill uses keyless bulk.meteostat.net / data.meteostat.net

# ── Misc ──────────────────────────────────────────────────
WP_LOG_DIR=./data
WP_KILL_SWITCH_FILE=./data/KILL_SWITCH
```
```
```
