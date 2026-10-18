"""
settle_cross_venue — realized-PnL proof for the cross-venue basis dislocations.

`cross_venue_basis` flags per-bucket dislocations and labels each day DIRECTIONAL
(venues disagree on level) or BASIS-RV (levels agree). This settles those flags
against the actual resolved highs (IEM ASOS at each venue's station: Kalshi=KNYC,
Polymarket=KLGA) and computes the realized PnL of acting on each flag held to
resolution:

  buy_cheap : buy YES at ask  -> +1 if the bucket resolved YES, else 0
  sell_rich : short YES at bid -> +bid if it resolved NO, else bid-1

PnL is aggregated by VERDICT so we can check the central thesis: if the
classifier is meaningful, BASIS-RV flags should be net-positive (calibration-free
edge) while DIRECTIONAL flags behave like forecast bets (no reliable edge from
price alone). This is the forward proof gate — trust the edge only once BASIS-RV
flags are net-positive over a meaningful resolved sample.

Run:
    IEM_USER_AGENT="..." python -m src.tools.settle_cross_venue [--min-depth 5]

Reads:  data/cross_venue/dislocations-*.jsonl
Writes: data/cross_venue/settled.jsonl (append-only)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from src.market.fees import kalshi_fee
from src.market.resolution import get_resolution
from src.tools.kalshi_settle_calibrate import resolved_high_f

_STATION_FOR = {"kalshi": ("kalshi", "nyc"), "polymarket": ("polymarket", "nyc")}


def _resolved_yes(lo: Optional[float], hi: Optional[float], high: float) -> bool:
    return (lo is None or high >= lo) and (hi is None or high < hi)


def _trade_pnl(d: Dict, won_yes: bool) -> float:
    """Realized PnL (1 contract) of acting on a flag, held to $1/$0 resolution."""
    venue = d["venue"]
    if d["side"] == "buy_cheap":
        entry = d.get("yes_ask") or d["market_mid"]
        fee = kalshi_fee(entry, 1) if venue == "kalshi" else 0.0
        return round((1.0 if won_yes else 0.0) - entry - fee, 4)
    # sell_rich: short YES at the bid (buy NO).
    entry = d.get("yes_bid") or d["market_mid"]
    fee = kalshi_fee(entry, 1) if venue == "kalshi" else 0.0
    return round(entry - (1.0 if won_yes else 0.0) - fee, 4)


def _latest_per_day(snap_dir: Path) -> Dict[str, Dict]:
    latest: Dict[str, Dict] = {}
    for fp in sorted(snap_dir.glob("dislocations-*.jsonl")):
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            day = rec["climate_day"]
            if day not in latest or rec.get("ts", 0) > latest[day].get("ts", 0):
                latest[day] = rec
    return latest


async def settle(min_depth: float) -> List[Dict]:
    from src.config import get_config
    snap_dir = Path(get_config().log_dir) / "cross_venue"
    if not snap_dir.exists():
        print(f"# no cross_venue dir at {snap_dir}; run cross_venue_basis first")
        return []

    days = _latest_per_day(snap_dir)
    today = _dt.datetime.now(_dt.timezone.utc).date()
    out: List[Dict] = []
    high_cache: Dict[str, Optional[float]] = {}

    for day_iso, rec in sorted(days.items()):
        day = _dt.date.fromisoformat(day_iso)
        if day >= today:
            continue  # not resolved yet
        verdict = (rec.get("diagnostic") or {}).get("verdict", "n/a")
        settled_flags = []
        for d in rec.get("dislocations", []):
            if d["depth"] < min_depth:
                continue
            venue_args = _STATION_FOR.get(d["venue"])
            if not venue_args:
                continue
            spec = get_resolution(*venue_args)
            ck = f"{spec.station}:{day_iso}"
            if ck not in high_cache:
                await asyncio.sleep(1.1)
                try:
                    high_cache[ck] = await resolved_high_f(spec.station, spec.tz, day)
                except Exception as exc:
                    print(f"# {ck}: fetch failed: {exc}")
                    high_cache[ck] = None
            high = high_cache[ck]
            if high is None:
                continue
            won = _resolved_yes(d["lo"], d["hi"], high)
            pnl = _trade_pnl(d, won)
            settled_flags.append({**d, "resolved_high": high, "won_yes": won, "pnl": pnl})
        if settled_flags:
            out.append({"climate_day": day_iso, "verdict": verdict, "flags": settled_flags})
    return out


def _summary(results: List[Dict]) -> None:
    if not results:
        print("\n# nothing resolved yet — no cross-venue days have settled.")
        return
    by_verdict = defaultdict(list)
    for r in results:
        tag = "BASIS-RV" if r["verdict"].startswith("BASIS-RV") else (
            "DIRECTIONAL" if r["verdict"].startswith("DIRECTIONAL") else "OTHER")
        by_verdict[tag].extend(r["flags"])

    print(f"\n# settled {len(results)} cross-venue days:\n")
    for day in results:
        won = sum(1 for f in day["flags"] if f["won_yes"] == (f["side"] == "buy_cheap"))
        pnl = sum(f["pnl"] for f in day["flags"])
        print(f"  {day['climate_day']}  {day['verdict'][:38]:<38} "
              f"flags={len(day['flags']):>2} pnl=${pnl:+.2f}")

    print("\n# realized PnL by verdict (1 contract per flag, fee-netted):")
    print(f"  {'verdict':<12} {'flags':>5} {'correct':>8} {'hit%':>6} {'pnl$':>8} {'pnl/flag':>9}")
    print("  " + "-" * 52)
    for tag in ("BASIS-RV", "DIRECTIONAL", "OTHER"):
        fl = by_verdict.get(tag, [])
        if not fl:
            continue
        # A flag is "correct" if its directional thesis paid: buy_cheap won YES,
        # or sell_rich resolved NO.
        correct = sum(1 for f in fl if f["won_yes"] == (f["side"] == "buy_cheap"))
        pnl = sum(f["pnl"] for f in fl)
        print(f"  {tag:<12} {len(fl):>5} {correct:>8} {100*correct/len(fl):>5.0f}% "
              f"{pnl:>+8.2f} {pnl/len(fl):>+9.3f}")
    print("#\n# GATE: trust the basis edge only if BASIS-RV pnl/flag is positive over "
          "a meaningful\n#       resolved sample. DIRECTIONAL flags are forecast bets — "
          "expect ~no price-only edge.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Settle cross-venue dislocations vs resolved highs")
    ap.add_argument("--min-depth", type=float, default=5.0)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    results = asyncio.run(settle(args.min_depth))
    if results and not args.no_write:
        from src.config import get_config
        out_path = Path(get_config().log_dir) / "cross_venue" / "settled.jsonl"
        with out_path.open("a") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")
        print(f"# appended {len(results)} settled days -> {out_path}")
    _summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
