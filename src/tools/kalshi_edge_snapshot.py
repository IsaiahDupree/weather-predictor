"""
Kalshi weather edge snapshot — read-only data capture + edge report.

For every open Kalshi daily-high temperature market across the configured
cities, this fuses ALL of our live data sources into one per-bucket view and
measures the edge:

  * market    : Kalshi order book (yes bid/ask/mid + ask-side depth)        [prod, read-only]
  * forecast  : Open-Meteo ensemble (GFS/ECMWF/ICON/GEM) -> per-bucket q    [free, no key]
  * obs       : IEM + NWS station obs -> daily-high-so-far + warming trend   [free, no key]
                -> obs nowcast q (monotone-high projection)

It NEVER places an order. It writes the full fused snapshot to a compounding
JSONL dataset (one line per event) and prints a ranked table of the largest
fee-netted edges, separating the two independent edge reads:

  ev_fcst = forecast_q - yes_ask - fee     (ensemble disagrees with the book)
  ev_obs  = nowcast_q  - yes_ask - fee     (live obs already determine the day)

Run:
    KALSHI_ENV=prod WP_DEFAULT_VENUE=kalshi \
    WP_CITIES="nyc,chicago,la,miami,denver,austin,philadelphia" \
    python -m src.tools.kalshi_edge_snapshot [--top N] [--min-depth N] [--no-write]

Output dataset: data/kalshi_snapshots/snapshot-<epoch>.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from src.config import get_config
from src.data.obs_tracker import ObsTracker
from src.market.event_catalog import EventCatalog
from src.market.fees import fee_per_contract
from src.market.snapshot import BucketQuote, MarketSnapshot
from src.runtime.factory import build_forecast_clients, build_obs_feeds, build_market_feed
from src.strategies.common import nowcast_q, project_high


def _is_same_climate_day(snap: MarketSnapshot) -> bool:
    """True iff this market resolves on the station's CURRENT climate day.

    The obs nowcast (daily-high-so-far + warming trend) is only meaningful for
    today's market — applying it to a next-day event reads today's high as if it
    were tomorrow's, a look-ahead artifact. We localize the climate-day midpoint
    (close - 12h) and compare to today's date in the station's timezone.
    """
    if ZoneInfo is None:
        return snap.hours_to_resolution <= 18.0  # crude fallback
    tz = ZoneInfo(snap.resolution.tz)
    close_time = snap.ts + snap.seconds_to_resolution
    event_day = datetime.fromtimestamp(close_time - 12 * 3600.0, tz=ZoneInfo("UTC")).astimezone(tz).date()
    today = datetime.fromtimestamp(snap.ts, tz=ZoneInfo("UTC")).astimezone(tz).date()
    return event_day == today


def _ask_depth(q: BucketQuote) -> float:
    """Total YES-ask size (contracts) available to lift."""
    if q.yes_ask_levels:
        return sum(sz for _, sz in q.yes_ask_levels)
    return q.yes_ask_size


def _bucket_rows(snap: MarketSnapshot, same_day: bool) -> List[Dict]:
    """One row per bucket fusing market + forecast + obs reads with fee-netted EV.

    The obs nowcast is gated to same-climate-day events only; on next-day events
    it would be a look-ahead artifact (today's high != tomorrow's high).
    """
    fq = snap.forecast_q or {}
    nq = nowcast_q(snap) if same_day else {}  # obs read only valid for today
    rows: List[Dict] = []
    for quote in snap.quotes:
        label = quote.label
        ask = float(quote.yes_ask or 0.0)
        depth = _ask_depth(quote)
        forecast_p = fq.get(label)
        obs_p = nq.get(label)
        # Fee netted at the price we'd pay (the ask). No tradeable ask => no edge.
        fee = fee_per_contract(snap.venue, ask) if ask > 0 else 0.0
        ev_fcst = (forecast_p - ask - fee) if (forecast_p is not None and ask > 0) else None
        ev_obs = (obs_p - ask - fee) if (obs_p is not None and ask > 0) else None
        rows.append({
            "symbol": quote.symbol,
            "bucket": label,
            "lo": quote.bucket.lo,
            "hi": quote.bucket.hi,
            "yes_bid": round(float(quote.yes_bid or 0.0), 4),
            "yes_ask": round(ask, 4),
            "yes_mid": round(float(quote.yes_mid or 0.0), 4),
            "ask_depth": round(depth, 1),
            "forecast_q": round(forecast_p, 4) if forecast_p is not None else None,
            "obs_nowcast_q": round(obs_p, 4) if obs_p is not None else None,
            "fee": round(fee, 4),
            "ev_fcst": round(ev_fcst, 4) if ev_fcst is not None else None,
            "ev_obs": round(ev_obs, 4) if ev_obs is not None else None,
        })
    return rows


def _event_record(snap: MarketSnapshot, rows: List[Dict], same_day: bool) -> Dict:
    mu, sigma = project_high(snap)
    return {
        "ts": snap.ts,
        "venue": snap.venue,
        "city": snap.city,
        "event_ticker": snap.event_ticker,
        "station": snap.resolution.station,
        "units": snap.resolution.units,
        "same_climate_day": same_day,
        # fused data-source context
        "obs_temp": snap.obs_temp,
        "obs_age_s": round(snap.obs_age_s, 1) if snap.obs_age_s is not None else None,
        "daily_high_so_far": snap.daily_high_so_far,
        "trend_per_hr": round(snap.trend_per_hr, 3) if snap.trend_per_hr is not None else None,
        "projected_high_mu": round(mu, 2) if mu is not None else None,
        "projected_high_sigma": round(sigma, 2) if sigma is not None else None,
        "resid_sigma": round(snap.resid_sigma, 3),
        "hours_to_resolution": round(snap.hours_to_resolution, 2),
        "n_buckets": len(rows),
        "buckets": rows,
    }


async def capture(cities: List[str]) -> List[Dict]:
    config = get_config()
    venue = "kalshi"

    # 1) live obs -> tracker (free IEM + NWS for the US resolving stations)
    tracker = ObsTracker()
    obs_feeds = build_obs_feeds(config, venue)
    n_obs = 0
    results = await asyncio.gather(*[f.fetch() for f in obs_feeds], return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        for tick in r:
            tracker.ingest(tick)
            n_obs += 1

    # 2) forecast clients (free Open-Meteo ensemble) + market feed + catalog
    forecast_clients = build_forecast_clients(config)
    market_feed = build_market_feed(config, venue)
    catalog = EventCatalog(market_feed, tracker, forecast_clients)

    print(f"# obs feeds={len(obs_feeds)} ingested={n_obs}  "
          f"forecast={[c.name for c in forecast_clients]}  "
          f"kalshi_env={config.kalshi_env}  cities={cities}")

    # 3) fuse -> per-event records
    records: List[Dict] = []
    for city in cities:
        snaps = await catalog.snapshots_for_city(city)
        for snap in snaps:
            same_day = _is_same_climate_day(snap)
            records.append(_event_record(snap, _bucket_rows(snap, same_day), same_day))
    return records


def _print_report(records: List[Dict], top: int, min_depth: float) -> None:
    flat = []
    for rec in records:
        for b in rec["buckets"]:
            if b["ask_depth"] < min_depth or b["yes_ask"] <= 0:
                continue
            best = max([e for e in (b["ev_fcst"], b["ev_obs"]) if e is not None], default=None)
            if best is None:
                continue
            src = "obs" if (b["ev_obs"] is not None and b["ev_obs"] == best) else "fcst"
            flat.append((best, src, rec, b))
    flat.sort(key=lambda t: t[0], reverse=True)

    print(f"\n# {len(records)} events captured; "
          f"top edges (ask_depth>={min_depth:g}, fee-netted), best of ensemble vs obs reads:\n")
    hdr = (f"{'net_ev':>7} {'src':>4} {'city':<6} {'bucket':<14} "
           f"{'ask':>6} {'fcstQ':>6} {'obsQ':>6} {'depth':>7} {'hrs':>5}  symbol")
    print(hdr)
    print("-" * len(hdr))
    for ev, src, rec, b in flat[:top]:
        fq = f"{b['forecast_q']:.3f}" if b["forecast_q"] is not None else "   -  "
        oq = f"{b['obs_nowcast_q']:.3f}" if b["obs_nowcast_q"] is not None else "   -  "
        print(f"{ev:>+7.3f} {src:>4} {rec['city']:<6} {b['bucket']:<14} "
              f"{b['yes_ask']:>6.3f} {fq:>6} {oq:>6} {b['ask_depth']:>7.0f} "
              f"{rec['hours_to_resolution']:>5.1f}  {b['symbol']}")
    pos = sum(1 for ev, *_ in flat if ev > 0)
    print(f"\n# {pos} buckets with positive fee-netted EV out of {len(flat)} liquid buckets scanned.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi weather edge snapshot (read-only)")
    ap.add_argument("--top", type=int, default=25, help="rows to print in the edge table")
    ap.add_argument("--min-depth", type=float, default=1.0, help="min YES-ask depth (contracts) to rank")
    ap.add_argument("--no-write", action="store_true", help="skip writing the JSONL dataset")
    args = ap.parse_args(argv)

    config = get_config()
    cities = list(config.cities)
    records = asyncio.run(capture(cities))

    if not args.no_write and records:
        out_dir = Path(config.log_dir) / "kalshi_snapshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"snapshot-{int(time.time())}.jsonl"
        with out_path.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        print(f"# wrote {len(records)} event records -> {out_path}")

    _print_report(records, args.top, args.min_depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
