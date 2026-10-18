"""
Cross-venue basis scanner — Kalshi(KNYC) vs Polymarket(KLGA), calibration-free.

Pulls both venues' live NYC daily-high boards (read-only), fits the KNYC->KLGA
station basis from IEM history, then reprices each venue from the OTHER venue's
market-implied distribution shifted by the basis. Buckets where a venue's quote
deviates from the other-venue-implied fair value by more than costs are flagged
as relative-value dislocations.

This needs NO forecast: it only uses each venue's own prices + the measured
basis. The hedged trade (buy cheap leg / sell rich leg) cancels the shared
weather risk; the residual risk is the basis NOISE (std ~2°F for KNYC/KLGA), so
this is relative value, not riskless arbitrage. Polymarket execution is US-gated
(reads are public; placing the PM leg needs the non-US path) — so from here this
is a signal + paper tool.

Run:
    NWS_USER_AGENT="..." IEM_USER_AGENT="..." \
    KALSHI_ENV=prod python -m src.tools.cross_venue_basis [--min-edge 0.05] [--hist-days 120]

Reads:  IEM history (cached), live Kalshi prod + Polymarket public APIs
Writes: data/basis/nyc_knyc_klga.json (model), data/cross_venue/dislocations-<ts>.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config import get_config
from src.edge.station_basis import (
    BasisModel, ConditionalBasisModel, convolve_conditional, fit_basis,
    fit_conditional_basis, normalize, pmf_to_bucket_probs, prices_to_pmf,
)
from src.market.fees import kalshi_fee_per_contract
from src.market.resolution import get_resolution
from src.tools.obs_history import daily_highs_range

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# --------------------------------------------------------------------------
# Basis model (KNYC -> KLGA), fit from IEM history with on-disk cache.
# --------------------------------------------------------------------------

async def load_pairs(city: str, hist_days: int, refresh: bool) -> List[Tuple[float, float]]:
    """(high_KNYC, high_KLGA) paired daily highs from IEM history (cached)."""
    k_spec = get_resolution("kalshi", city)
    p_spec = get_resolution("polymarket", city)
    if k_spec is None or p_spec is None:
        raise SystemExit(f"need both kalshi+polymarket resolution specs for {city!r}")
    end = _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=hist_days)
    a = await daily_highs_range(k_spec.station, k_spec.tz, start, end, use_cache=not refresh)
    await asyncio.sleep(2)
    b = await daily_highs_range(p_spec.station, p_spec.tz, start, end, use_cache=not refresh)
    return [(a[d], b[d]) for d in sorted(set(a) & set(b))]


# --------------------------------------------------------------------------
# Board extraction: (market, quotes) -> per-bucket rows with normalized mids.
# --------------------------------------------------------------------------

_KALSHI_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$", re.I)
_PM_DATE_RE = re.compile(r"-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?", re.I)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"]) if i}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})


def _event_date(event_ticker: str, close_time: float) -> Optional[_dt.date]:
    """Resolution date parsed from the event id (robust to venue close conventions).

    Kalshi: 'KXHIGHNY-26JUN13' -> 2026-06-13.
    Polymarket: 'highest-temperature-in-nyc-on-june-14-2026' -> 2026-06-14
    (year inferred from close_time when the slug omits it).
    """
    et = event_ticker or ""
    m = _KALSHI_DATE_RE.search(et)
    if m:
        yy, mon, dd = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
        if mon:
            return _dt.date(2000 + yy, mon, dd)
    m = _PM_DATE_RE.search(et.lower())
    if m:
        mon = _MONTHS.get(m.group(1)[:3])
        dd = int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else (
            _dt.datetime.fromtimestamp(close_time, tz=_dt.timezone.utc).year if close_time else 2026)
        if mon:
            return _dt.date(yr, mon, dd)
    return None


async def _board(feed, city: str, tz: str, want_high: bool) -> Dict[str, Dict]:
    """{climate_day_iso: {buckets:[...], event:...}} for one venue, daily-HIGH only."""
    markets = await feed.discover(city)
    out: Dict[str, Dict] = {}
    for m in markets:
        et = (m.event_ticker or "").lower()
        # Polymarket lists both highest/lowest; keep only highest-temperature.
        if "lowest" in et:
            continue
        ed = _event_date(m.event_ticker, m.close_time)
        if ed is None:
            continue
        day = ed.isoformat()
        quotes = await feed.quotes(m)
        rows = []
        for q in quotes:
            mid = q.yes_mid
            rows.append({
                "label": q.label, "lo": q.bucket.lo, "hi": q.bucket.hi,
                "yes_bid": round(float(q.yes_bid or 0.0), 4),
                "yes_ask": round(float(q.yes_ask or 0.0), 4),
                "yes_mid": round(float(mid or 0.0), 4),
                "depth": round(sum(s for _, s in q.yes_ask_levels) or q.yes_ask_size, 1),
            })
        if rows:
            out[day] = {"event": m.event_ticker, "buckets": rows}
    return out


def _norm_mids(rows: List[Dict]) -> Dict[str, float]:
    raw = {r["label"]: r["yes_mid"] for r in rows if r["yes_mid"] > 0}
    return normalize(raw)


def _implied_mean(rows: List[Dict]) -> Optional[float]:
    """Expected high (°F) implied by a venue's own normalized bucket mids."""
    q = _norm_mids(rows)
    pmf = prices_to_pmf([(r["lo"], r["hi"], q.get(r["label"], 0.0)) for r in rows])
    if not pmf:
        return None
    return round(sum(t * p for t, p in pmf.items()), 2)


def _level_diagnostic(k_rows: List[Dict], p_rows: List[Dict],
                      cond_ab: ConditionalBasisModel) -> Dict:
    """Separate a station-basis gap from a directional temperature disagreement.

    Kalshi's implied KNYC mean, shifted by the conditional basis for that regime,
    is what KLGA "should" average. The EXCESS of PM's implied KLGA mean over that
    is the venues disagreeing on the weather itself — a directional call (needs a
    forecast), NOT a calibration-free basis edge.
    """
    k_mean = _implied_mean(k_rows)   # KNYC
    p_mean = _implied_mean(p_rows)   # KLGA
    if k_mean is None or p_mean is None:
        return {"verdict": "n/a"}
    basis_mean = cond_ab.model_for(k_mean).mean
    expected_klga = k_mean + basis_mean
    excess = round(p_mean - expected_klga, 2)
    verdict = ("DIRECTIONAL — venues disagree on level (needs forecast)"
               if abs(excess) > 1.0 else "BASIS-RV — venues agree on level")
    return {
        "knyc_implied_mean": k_mean, "klga_implied_mean": p_mean,
        "basis_mean_for_regime": basis_mean, "expected_klga_mean": round(expected_klga, 2),
        "excess_gap_f": excess, "verdict": verdict,
    }


# --------------------------------------------------------------------------
# Repricing: implied pmf from one venue -> fair prices on the other.
# --------------------------------------------------------------------------

def _reprice(src_rows: List[Dict], dst_rows: List[Dict],
             cbm: ConditionalBasisModel, sign: int) -> Dict[str, float]:
    """Fair YES q for each dst bucket, implied by src prices shifted by the
    regime-conditional basis.

    sign=+1 reprices station B (PM/KLGA) from station A (Kalshi/KNYC) src.
    sign=-1 reprices station A (Kalshi/KNYC) from station B (PM/KLGA) src.
    """
    src_q = _norm_mids(src_rows)
    src_buckets = [(r["lo"], r["hi"], src_q.get(r["label"], 0.0)) for r in src_rows]
    src_pmf = prices_to_pmf(src_buckets)
    dst_pmf = convolve_conditional(src_pmf, cbm, sign=sign)
    dst_grid = [(r["label"], r["lo"], r["hi"]) for r in dst_rows]
    return pmf_to_bucket_probs(dst_pmf, dst_grid)


def _dislocations(rows: List[Dict], fair: Dict[str, float], venue: str,
                  min_edge: float, basis_buffer: float) -> List[Dict]:
    """Flag buckets whose market mid deviates from fair beyond cost+basis buffer."""
    market_q = _norm_mids(rows)
    out = []
    for r in rows:
        lbl = r["label"]
        fq = fair.get(lbl)
        mq = market_q.get(lbl)
        if fq is None or mq is None or r["depth"] <= 0:
            continue
        disloc = mq - fq                          # +: market rich vs fair, -: cheap
        # Cost to act on THIS leg: Kalshi taker fee (PM ~0) + half the bid/ask spread.
        spread = max(0.0, r["yes_ask"] - r["yes_bid"])
        cost = (kalshi_fee_per_contract(r["yes_ask"]) if venue == "kalshi" else 0.0) + spread / 2.0
        net = abs(disloc) - cost - basis_buffer
        if net >= min_edge:
            out.append({
                "venue": venue, "bucket": lbl, "lo": r["lo"], "hi": r["hi"],
                "market_mid": round(mq, 4), "fair_q": round(fq, 4),
                "yes_ask": r["yes_ask"], "yes_bid": r["yes_bid"],
                "disloc": round(disloc, 4), "cost": round(cost, 4),
                "net_edge": round(net, 4), "depth": r["depth"],
                "side": "sell_rich" if disloc > 0 else "buy_cheap",
            })
    return out


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

async def scan(city: str, min_edge: float, hist_days: int, refresh: bool, basis_buffer: float) -> Dict:
    cfg = get_config()
    from src.data.feeds.kalshi_market_feed import KalshiMarketFeed
    from src.data.feeds.pm_market_feed import PolymarketMarketFeed

    pairs = await load_pairs(city, hist_days, refresh)
    k_spec, p_spec = get_resolution("kalshi", city), get_resolution("polymarket", city)
    uncond = fit_basis(pairs)
    # Condition each direction on the SOURCE station's own high level.
    cond_ab = fit_conditional_basis(pairs, condition_on="A")  # KNYC-source -> KLGA
    cond_ba = fit_conditional_basis(pairs, condition_on="B")  # KLGA-source -> KNYC
    _persist_basis(cfg, city, k_spec, p_spec, uncond, cond_ab)

    print(f"# basis {k_spec.station}->{p_spec.station} (n={uncond.n}): "
          f"unconditional mean={uncond.mean:+.2f}F std={uncond.std:.2f}F")
    print(f"# regime-conditional (on {k_spec.station} high):")
    for lo, hi, m in cond_ab.bins:
        rng = f"{'<' + str(int(hi)) if lo is None else ('>=' + str(int(lo)) if hi is None else f'{int(lo)}-{int(hi)}')}"
        print(f"#   {rng:>8}F: n={m.n:>3} mean={m.mean:+.2f}F std={m.std:.2f}F")
    if uncond.n < 30:
        print(f"# WARNING basis sample small (n={uncond.n}); widen --hist-days")

    kfeed, pfeed = KalshiMarketFeed(cfg), PolymarketMarketFeed(cfg)
    k_board = await _board(kfeed, city, k_spec.tz, want_high=True)
    p_board = await _board(pfeed, city, p_spec.tz, want_high=True)

    shared_days = sorted(set(k_board) & set(p_board))
    print(f"# kalshi days={sorted(k_board)}  pm days={sorted(p_board)}  shared={shared_days}")

    results = []
    for day in shared_days:
        k_rows = k_board[day]["buckets"]
        p_rows = p_board[day]["buckets"]
        # Fair PM (KLGA) from Kalshi (KNYC); fair Kalshi (KNYC) from PM (KLGA),
        # each using the regime-conditional basis keyed on the source station.
        fair_pm = _reprice(k_rows, p_rows, cond_ab, sign=+1)
        fair_k = _reprice(p_rows, k_rows, cond_ba, sign=-1)
        disl = (_dislocations(p_rows, fair_pm, "polymarket", min_edge, basis_buffer)
                + _dislocations(k_rows, fair_k, "kalshi", min_edge, basis_buffer))
        diag = _level_diagnostic(k_rows, p_rows, cond_ab)
        results.append({
            "ts": time.time(), "city": city, "climate_day": day,
            "kalshi_event": k_board[day]["event"], "pm_event": p_board[day]["event"],
            "basis_mean": uncond.mean, "basis_std": uncond.std, "basis_n": uncond.n,
            "diagnostic": diag,
            "dislocations": sorted(disl, key=lambda d: -d["net_edge"]),
        })
    return {"basis": {"mean": uncond.mean, "std": uncond.std, "n": uncond.n}, "days": results}


def _persist_basis(cfg, city, k_spec, p_spec, uncond, cond_ab) -> None:
    out_dir = Path(cfg.log_dir) / "basis"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{city}_{k_spec.station}_{p_spec.station}.json"
    path.write_text(json.dumps({
        "city": city, "station_a": k_spec.station, "station_b": p_spec.station,
        "unconditional": {"pmf": {str(k): v for k, v in uncond.pmf.items()},
                          "mean": uncond.mean, "std": uncond.std, "n": uncond.n},
        "conditional_on_a": [
            {"lo": lo, "hi": hi, "mean": m.mean, "std": m.std, "n": m.n,
             "pmf": {str(k): v for k, v in m.pmf.items()}}
            for lo, hi, m in cond_ab.bins],
    }, indent=2))


def _print(report: Dict, min_edge: float) -> None:
    total = 0
    for day in report["days"]:
        d = day["dislocations"]
        dg = day.get("diagnostic", {})
        print(f"\n# {day['climate_day']}  kalshi={day['kalshi_event']}  pm={day['pm_event']}")
        if dg.get("verdict") and dg["verdict"] != "n/a":
            print(f"#   KNYC implied {dg['knyc_implied_mean']}F + regime basis "
                  f"{dg['basis_mean_for_regime']:+.2f} = expect KLGA {dg['expected_klga_mean']}F; "
                  f"PM implies KLGA {dg['klga_implied_mean']}F  -> excess {dg['excess_gap_f']:+.2f}F")
            print(f"#   VERDICT: {dg['verdict']}")
        print(f"#   {len(d)} bucket dislocations >= {min_edge}")
        if not d:
            continue
        hdr = (f"  {'venue':<11} {'bucket':<14} {'side':<10} "
               f"{'mid':>6} {'fair':>6} {'disloc':>7} {'cost':>6} {'net':>7} {'depth':>8}")
        print(hdr); print("  " + "-" * (len(hdr) - 2))
        for x in d:
            total += 1
            print(f"  {x['venue']:<11} {x['bucket']:<14} {x['side']:<10} "
                  f"{x['market_mid']:>6.3f} {x['fair_q']:>6.3f} {x['disloc']:>+7.3f} "
                  f"{x['cost']:>6.3f} {x['net_edge']:>+7.3f} {x['depth']:>8.0f}")
    print(f"\n# {total} total dislocations flagged. NOTE relative-value, not arb: "
          f"basis std {report['basis']['std']:.1f}F is unhedged residual risk. "
          f"PM leg execution is US-gated.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cross-venue station-basis scanner (read-only)")
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--min-edge", type=float, default=0.05,
                    help="min net edge (after cost+basis buffer) to flag")
    ap.add_argument("--hist-days", type=int, default=120, help="IEM history days for the basis fit")
    ap.add_argument("--basis-buffer", type=float, default=0.0,
                    help="extra prob buffer for basis noise before flagging (e.g. 0.03)")
    ap.add_argument("--refresh-basis", action="store_true", help="refit the basis from IEM (ignore cache)")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    cfg = get_config()
    report = asyncio.run(scan(args.city, args.min_edge, args.hist_days,
                              args.refresh_basis, args.basis_buffer))

    if not args.no_write and report["days"]:
        out_dir = Path(cfg.log_dir) / "cross_venue"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"dislocations-{int(time.time())}.jsonl"
        with out_path.open("w") as fh:
            for day in report["days"]:
                fh.write(json.dumps(day) + "\n")
        print(f"# wrote {len(report['days'])} day records -> {out_path}")

    _print(report, args.min_edge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
