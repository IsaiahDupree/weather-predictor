"""
kalshi_calibration_backtest — is the Kalshi weather market itself mispriced?

A market-only edge needs no forecast: if buckets priced at p don't resolve YES
~p of the time, that miscalibration is tradeable. Prediction markets classically
show a FAVORITE-LONGSHOT BIAS (cheap longshots overpriced, favorites
underpriced). This backtests it on real settled Kalshi daily-high markets using
only public data: the candlestick YES price at a chosen lead time before close,
paired with the realized result.

For each settled bucket we record (price_at_lead, result), then:
  * build a reliability table (price bin -> realized YES frequency) + ECE + Brier,
  * backtest three fee-netted strategies at honest fills (buy=ask, sell=bid):
      fade_longshots : SELL YES on buckets with mid <= longshot_max
      buy_longshots  : BUY  YES on the same buckets (the opposite)
      buy_favorites  : BUY  YES on buckets with mid >= favorite_min

A positive fade_longshots PnL with longshot bins resolving BELOW their price is
the classic, fundable bias. Out-of-sample by construction (settled history).

Run:
    KALSHI_ENV=prod WP_CITIES="nyc,chicago,la,miami,denver,austin,philadelphia" \
    python -m src.tools.kalshi_calibration_backtest [--lead-hours 6] [--min-volume 20]

Writes: data/backtest/kalshi_calibration_samples.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from src.config import get_config
from src.data.feeds.kalshi_market_feed import KalshiMarketFeed
from src.market.fees import kalshi_fee
from src.tools.kalshi_history import candlestick_at, market_result, settled_events, _parse_iso


async def collect(cities: List[str], lead_hours: float, max_events: int,
                  concurrency: int) -> List[Dict]:
    samples: List[Dict] = []
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20.0) as client:
        for city in cities:
            series = KalshiMarketFeed.series_for(city)
            if not series:
                continue
            try:
                events = await settled_events(client, series)
            except httpx.HTTPError as exc:
                print(f"# {city}: settled_events failed: {exc}")
                continue
            events = events[:max_events]
            print(f"# {city} ({series}): {len(events)} settled events")
            await asyncio.sleep(1.0)  # be polite between series

            async def one(ev, m):
                ticker = m.get("ticker")
                result = market_result(m)
                close_ts = _parse_iso(m.get("close_time"))
                if not ticker or result is None or close_ts is None:
                    return None
                async with sem:
                    await asyncio.sleep(0.05)
                    cs = await candlestick_at(client, series, ticker, close_ts, lead_hours)
                if cs is None or cs["mid"] is None or not (0.0 < cs["mid"] < 1.0):
                    return None
                return {
                    "city": city, "ticker": ticker, "result": result,
                    "mid": cs["mid"], "bid": cs["bid"], "ask": cs["ask"],
                    "volume": cs["volume"], "lead_hours": lead_hours,
                }

            tasks = [one(ev, m) for ev in events for m in (ev.get("markets") or [])]
            for r in await asyncio.gather(*tasks):
                if r:
                    samples.append(r)
    return samples


# --- analysis -------------------------------------------------------------

def reliability(samples: List[Dict], bins: int = 10) -> List[Dict]:
    table = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = [s for s in samples if (lo <= s["mid"] < hi) or (hi == 1.0 and s["mid"] == 1.0)]
        if not sel:
            continue
        mp = sum(s["mid"] for s in sel) / len(sel)
        freq = sum(s["result"] for s in sel) / len(sel)
        table.append({"lo": round(lo, 2), "hi": round(hi, 2), "n": len(sel),
                      "mean_price": round(mp, 4), "realized_yes": round(freq, 4),
                      "gap": round(freq - mp, 4)})
    return table


def brier_ece(samples: List[Dict], table: List[Dict]) -> Dict[str, float]:
    n = len(samples)
    brier = sum((s["mid"] - s["result"]) ** 2 for s in samples) / n if n else float("nan")
    ece = sum(b["n"] * abs(b["gap"]) for b in table) / n if n else float("nan")
    return {"brier": round(brier, 4), "ece": round(ece, 4), "n": n}


def _fee(p: float) -> float:
    return kalshi_fee(p, 1)


def backtest(samples: List[Dict], longshot_max: float, favorite_min: float) -> Dict:
    def buy(s):  # buy YES at ask (fallback mid)
        p = s["ask"] if s["ask"] not in (None, 0.0) else s["mid"]
        return round(s["result"] - p - _fee(p), 4), p
    def sell(s):  # short YES at bid (fallback mid)
        p = s["bid"] if s["bid"] not in (None, 0.0) else s["mid"]
        return round(p - s["result"] - _fee(p), 4), p

    out = {}
    ls = [s for s in samples if s["mid"] <= longshot_max]
    fav = [s for s in samples if s["mid"] >= favorite_min]
    for name, sel, fn in (
        ("fade_longshots", ls, sell),
        ("buy_longshots", ls, buy),
        ("buy_favorites", fav, buy),
    ):
        if not sel:
            out[name] = {"n": 0}
            continue
        pnls = [fn(s)[0] for s in sel]
        wins = sum(1 for s in sel if (s["result"] == 1) == (fn is buy))
        out[name] = {
            "n": len(sel), "wins": wins, "hit%": round(100 * wins / len(sel), 1),
            "total_pnl": round(sum(pnls), 2), "pnl_per_trade": round(sum(pnls) / len(sel), 4),
        }
    return out


def _print(samples, table, metrics, bt, longshot_max, favorite_min):
    print(f"\n# {metrics['n']} settled bucket-samples  Brier={metrics['brier']}  ECE={metrics['ece']}")
    print("\n# reliability (market price bin -> realized YES frequency):")
    print(f"  {'bin':<12} {'n':>5} {'mean_price':>11} {'realized':>9} {'gap':>8}")
    print("  " + "-" * 48)
    for b in table:
        flag = "  <-- overpriced" if b["gap"] < -0.03 else ("  <-- underpriced" if b["gap"] > 0.03 else "")
        print(f"  [{b['lo']:.1f},{b['hi']:.1f}) {b['n']:>5} {b['mean_price']:>11.3f} "
              f"{b['realized_yes']:>9.3f} {b['gap']:>+8.3f}{flag}")
    print(f"\n# strategy backtest (1 contract/trade, honest fills, taker-fee netted):")
    print(f"  {'strategy':<16} {'n':>5} {'hit%':>6} {'total$':>9} {'$/trade':>9}")
    print("  " + "-" * 48)
    for k in ("fade_longshots", "buy_longshots", "buy_favorites"):
        b = bt[k]
        if b.get("n"):
            print(f"  {k:<16} {b['n']:>5} {b['hit%']:>5.0f}% {b['total_pnl']:>+9.2f} {b['pnl_per_trade']:>+9.4f}")
        else:
            print(f"  {k:<16} {0:>5}  (no samples in band)")
    print(f"#\n# longshot band mid<={longshot_max}, favorite band mid>={favorite_min}. "
          f"Positive fade_longshots + negative gaps in low bins = favorite-longshot bias.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi weather market-calibration backtest")
    ap.add_argument("--lead-hours", type=float, default=24.0,
                    help="price lead time before close (24h ~ day-ahead, outcome still uncertain)")
    ap.add_argument("--min-volume", type=float, default=0.0, help="min candle volume to include")
    ap.add_argument("--longshot-max", type=float, default=0.15)
    ap.add_argument("--favorite-min", type=float, default=0.85)
    ap.add_argument("--max-events", type=int, default=45, help="settled events per city")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    cfg = get_config()
    samples = asyncio.run(collect(list(cfg.cities), args.lead_hours, args.max_events, args.concurrency))
    samples = [s for s in samples if s["volume"] >= args.min_volume]
    if not samples:
        print("# no samples collected.")
        return 0

    table = reliability(samples)
    metrics = brier_ece(samples, table)
    bt = backtest(samples, args.longshot_max, args.favorite_min)

    if not args.no_write:
        out_dir = Path(cfg.log_dir) / "backtest"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "kalshi_calibration_samples.jsonl"
        with out_path.open("w") as fh:
            for s in samples:
                fh.write(json.dumps(s) + "\n")
        print(f"# wrote {len(samples)} samples -> {out_path}")

    _print(samples, table, metrics, bt, args.longshot_max, args.favorite_min)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
