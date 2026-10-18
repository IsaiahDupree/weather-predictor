"""
Kalshi weather settlement + calibration — close the proof loop on captured edges.

`kalshi_edge_snapshot` captures fused market+forecast+obs snapshots and *flags*
edges. This tool answers the only question that matters: were those edges REAL?

For every captured event whose climate day is already in the past, it:
  1. fetches the resolved daily HIGH from IEM ASOS (free, no key) over the
     station's LST climate-day window — a close proxy for the NWS CLI value
     Kalshi settles on,
  2. finds the winning bucket (the one containing the resolved high),
  3. scores each probability source (ensemble forecast_q, obs nowcast_q) with a
     Brier score against the realized outcome, and
  4. backtests the flagged +EV edges: buy YES at the captured ask, settle $1/$0,
     report hit-rate and realized PnL per source.

This is the calibration gate from memory: do NOT trade these edges until a source
is shown calibrated (low Brier) AND its flagged edges are net-profitable out of
sample. The longshot pattern lost $724 in the paper book before; this measures it.

Run:
    NWS_USER_AGENT="..." IEM_USER_AGENT="..." \
    python -m src.tools.kalshi_settle_calibrate [--probe STATION YYYY-MM-DD]

  --probe : just fetch+print one resolved daily high (validates the IEM path).

Reads:  data/kalshi_snapshots/*.jsonl
Writes: data/calibration/kalshi_calibration.jsonl   (append-only ledger)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from src.config import get_config
from src.market.fees import kalshi_fee
from src.market.resolution import get_resolution

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_MISSING = {"", "null", "none", "m", "t", "-99", "-99.0", "-99.00", "nan"}


# --------------------------------------------------------------------------
# Resolved daily high (ground truth) via IEM ASOS over the LST climate day
# --------------------------------------------------------------------------

def _lst_window_utc(tz_name: str, day: _dt.date) -> Tuple[_dt.datetime, _dt.datetime]:
    """[start, end) UTC datetimes for the local climate day `day` in `tz_name`."""
    if ZoneInfo is None:  # pragma: no cover
        start = _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
        return start, start + _dt.timedelta(days=1)
    tz = ZoneInfo(tz_name)
    start_local = _dt.datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
    end_local = start_local + _dt.timedelta(days=1)
    return start_local.astimezone(_dt.timezone.utc), end_local.astimezone(_dt.timezone.utc)


async def resolved_high_f(station: str, tz_name: str, day: _dt.date) -> Optional[float]:
    """Max observed air temp (°F) over the station's LST climate day, via IEM ASOS."""
    cfg = get_config()
    start, end = _lst_window_utc(tz_name, day)
    params = [
        ("station", station.strip().upper()),
        ("data", "tmpf"), ("tz", "UTC"), ("format", "onlycomma"),
        ("latlon", "no"), ("missing", "null"), ("trace", "null"),
        ("year1", str(start.year)), ("month1", str(start.month)), ("day1", str(start.day)),
        ("hour1", str(start.hour)), ("minute1", str(start.minute)),
        ("year2", str(end.year)), ("month2", str(end.month)), ("day2", str(end.day)),
        ("hour2", str(end.hour)), ("minute2", str(end.minute)),
    ]
    headers = {"User-Agent": cfg.iem_user_agent, "Accept": "text/csv, text/plain"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        resp = await client.get(_ASOS_URL, params=params)
        resp.raise_for_status()
        body = resp.text
    hi: Optional[float] = None
    for line in body.splitlines()[1:]:
        cols = line.split(",")
        if len(cols) < 3:
            continue
        v = cols[2].strip()
        if v.lower() in _MISSING:
            continue
        try:
            t = float(v)
        except ValueError:
            continue
        if t <= -98.0:
            continue
        if hi is None or t > hi:
            hi = t
    return round(hi, 1) if hi is not None else None


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _winning_bucket(buckets: List[Dict], high: float) -> Optional[str]:
    """Label of the bucket whose [lo, hi) contains the resolved high."""
    for b in buckets:
        lo, hi = b.get("lo"), b.get("hi")
        if (lo is None or high >= lo) and (hi is None or high < hi):
            return b["bucket"]
    return None


def _brier(buckets: List[Dict], qkey: str, winner: str) -> Optional[float]:
    """Multi-bucket Brier: sum_b (q_b - 1{b==winner})^2 over buckets carrying q."""
    qs = [(b["bucket"], b[qkey]) for b in buckets if b.get(qkey) is not None]
    if not qs:
        return None
    return round(sum((q - (1.0 if lbl == winner else 0.0)) ** 2 for lbl, q in qs), 4)


def _backtest_edges(buckets: List[Dict], winner: str, evkey: str,
                    min_depth: float) -> Tuple[int, int, float]:
    """Buy YES at ask on every bucket flagged +EV by `evkey`; settle $1/$0.

    Returns (n_bets, n_wins, realized_pnl_usd) for 1 contract per flagged bucket.
    """
    n = wins = 0
    pnl = 0.0
    for b in buckets:
        ev = b.get(evkey)
        if ev is None or ev <= 0 or b["ask_depth"] < min_depth or b["yes_ask"] <= 0:
            continue
        n += 1
        ask = b["yes_ask"]
        won = (b["bucket"] == winner)
        wins += 1 if won else 0
        payoff = 1.0 if won else 0.0
        pnl += payoff - ask - kalshi_fee(ask, 1)  # 1 contract, taker fee
    return n, wins, round(pnl, 4)


# --------------------------------------------------------------------------
# Main settle-calibrate pass
# --------------------------------------------------------------------------

def _load_latest_per_event(snap_dir: Path) -> Dict[str, Dict]:
    """Latest captured record per event_ticker across all snapshot files."""
    latest: Dict[str, Dict] = {}
    for fp in sorted(snap_dir.glob("snapshot-*.jsonl")):
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            et = rec["event_ticker"]
            if et not in latest or rec["ts"] > latest[et]["ts"]:
                latest[et] = rec
    return latest


def _event_climate_date(rec: Dict) -> _dt.date:
    tz = ZoneInfo(get_resolution("kalshi", rec["city"]).tz) if ZoneInfo else _dt.timezone.utc
    close = rec["ts"] + 0.0  # ts at capture
    # climate-day midpoint = close_of_market - 12h; reconstruct from hours_to_res
    close_time = rec["ts"] + rec["hours_to_resolution"] * 3600.0
    return _dt.datetime.fromtimestamp(close_time - 12 * 3600.0, tz=_dt.timezone.utc).astimezone(tz).date()


async def settle_calibrate(min_depth: float) -> List[Dict]:
    cfg = get_config()
    snap_dir = Path(cfg.log_dir) / "kalshi_snapshots"
    if not snap_dir.exists():
        print(f"# no snapshot dir at {snap_dir}; run kalshi_edge_snapshot first")
        return []

    records = _load_latest_per_event(snap_dir)
    today_utc = _dt.datetime.now(_dt.timezone.utc).date()
    results: List[Dict] = []

    for et, rec in sorted(records.items()):
        spec = get_resolution("kalshi", rec["city"])
        if spec is None:
            continue
        day = _event_climate_date(rec)
        # Only score days that are fully in the past at the station.
        if day >= today_utc:
            continue
        await asyncio.sleep(1.1)  # IEM asks clients not to hammer (429s otherwise)
        try:
            high = await resolved_high_f(spec.station, spec.tz, day)
        except Exception as exc:
            print(f"# {et}: resolved-high fetch failed: {exc}")
            continue
        if high is None:
            print(f"# {et}: no resolved high available yet for {day}")
            continue
        winner = _winning_bucket(rec["buckets"], high)
        out = {
            "event_ticker": et, "city": rec["city"], "station": spec.station,
            "climate_day": day.isoformat(), "resolved_high_f": high,
            "winning_bucket": winner,
            "brier_forecast": _brier(rec["buckets"], "forecast_q", winner),
            "brier_obs": _brier(rec["buckets"], "obs_nowcast_q", winner),
            "capture_ts": rec["ts"],
            "hours_to_res_at_capture": rec["hours_to_resolution"],
        }
        for src, evkey in (("fcst", "ev_fcst"), ("obs", "ev_obs")):
            n, wins, pnl = _backtest_edges(rec["buckets"], winner, evkey, min_depth)
            out[f"{src}_edges"] = n
            out[f"{src}_wins"] = wins
            out[f"{src}_pnl_usd"] = pnl
        results.append(out)
    return results


def _print_summary(results: List[Dict]) -> None:
    if not results:
        print("\n# nothing to settle yet — no captured events have resolved.")
        return
    print(f"\n# settled {len(results)} resolved events:\n")
    hdr = (f"{'city':<6} {'day':<10} {'high':>5} {'winner':<14} "
           f"{'BrierF':>7} {'BrierO':>7} {'fEdg':>4} {'fW':>3} {'fPnL':>7} "
           f"{'oEdg':>4} {'oW':>3} {'oPnL':>7}")
    print(hdr)
    print("-" * len(hdr))
    bf = bo = 0.0
    nbf = nbo = 0
    fpnl = opnl = 0.0
    fe = fw = oe = ow = 0
    for r in results:
        print(f"{r['city']:<6} {r['climate_day']:<10} {r['resolved_high_f']:>5.0f} "
              f"{str(r['winning_bucket']):<14} "
              f"{_fmt(r['brier_forecast']):>7} {_fmt(r['brier_obs']):>7} "
              f"{r['fcst_edges']:>4} {r['fcst_wins']:>3} {r['fcst_pnl_usd']:>+7.2f} "
              f"{r['obs_edges']:>4} {r['obs_wins']:>3} {r['obs_pnl_usd']:>+7.2f}")
        if r["brier_forecast"] is not None:
            bf += r["brier_forecast"]; nbf += 1
        if r["brier_obs"] is not None:
            bo += r["brier_obs"]; nbo += 1
        fpnl += r["fcst_pnl_usd"]; opnl += r["obs_pnl_usd"]
        fe += r["fcst_edges"]; fw += r["fcst_wins"]; oe += r["obs_edges"]; ow += r["obs_wins"]
    print("-" * len(hdr))
    print(f"\n# mean Brier  forecast={bf/nbf:.4f} (n={nbf})  "
          f"obs={bo/nbo:.4f} (n={nbo})" if nbf and nbo else
          f"\n# mean Brier  forecast={(bf/nbf) if nbf else float('nan'):.4f}")
    print(f"# flagged-edge backtest (1 contract each, taker-fee netted):")
    print(f"#   forecast: {fw}/{fe} won, realized PnL ${fpnl:+.2f}")
    print(f"#   obs     : {ow}/{oe} won, realized PnL ${opnl:+.2f}")
    print("#\n# GATE: trade a source only once mean Brier is low AND its edge PnL is "
          "positive\n#       across a meaningful resolved sample (>= ~30 events).")


def _fmt(x: Optional[float]) -> str:
    return f"{x:.3f}" if x is not None else "  -  "


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi weather settlement + calibration")
    ap.add_argument("--probe", nargs=2, metavar=("STATION", "DATE"),
                    help="fetch+print one resolved daily high (e.g. --probe KNYC 2026-06-12)")
    ap.add_argument("--min-depth", type=float, default=5.0,
                    help="min YES-ask depth for an edge to count in the backtest")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    cfg = get_config()

    if args.probe:
        station, date_s = args.probe
        day = _dt.date.fromisoformat(date_s)
        # Use the station's tz if we know it, else America/New_York.
        tz = "America/New_York"
        for (v, c), spec in __import__("src.market.resolution", fromlist=["RESOLUTION_SPECS"]).RESOLUTION_SPECS.items():
            if spec.station == station.upper():
                tz = spec.tz
                break
        high = asyncio.run(resolved_high_f(station, tz, day))
        print(f"resolved high {station} {date_s} ({tz}) = {high}°F")
        return 0

    results = asyncio.run(settle_calibrate(args.min_depth))

    if results and not args.no_write:
        out_dir = Path(cfg.log_dir) / "calibration"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "kalshi_calibration.jsonl"
        with out_path.open("a") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")
        print(f"# appended {len(results)} results -> {out_path}")

    _print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
