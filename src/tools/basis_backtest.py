"""
basis_backtest — walk-forward out-of-sample validation of the station basis.

The cross-venue edge rests on ONE structural claim: the (regime-conditional)
KNYC->KLGA basis is a stable, predictive relationship, not an in-sample artifact.
This tests it honestly. Walking forward day by day, we fit the basis on PAST
days only and predict the held-out day's KLGA high from its KNYC high three ways:

  naive  : KLGA = KNYC            (delta = 0)
  uncond : KLGA = KNYC + E[delta]                 (one pooled mean)
  cond   : KLGA = KNYC + E[delta | KNYC regime]   (regime-conditional mean)

We report OOS MAE / RMSE / bias overall and split by regime. If `cond` beats
`uncond` beats `naive` out of sample — especially on HOT days, where the regime
effect lives — the conditioning is real signal and the calibration-free
repricing is sound. The OOS residual std is the irreducible basis noise: the
unhedged slippage any cross-venue trade must overcome.

Run:
    IEM_USER_AGENT="..." python -m src.tools.basis_backtest [--city nyc] [--days 730] [--warmup 120]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
from typing import Dict, List, Optional, Tuple

from src.config import get_config
from src.edge.station_basis import fit_basis, fit_conditional_basis
from src.market.resolution import get_resolution
from src.tools.obs_history import daily_highs_range


async def _paired_history(city: str, days: int, refresh: bool) -> List[Tuple[str, float, float]]:
    """[(date_iso, high_A, high_B)] chronological for the city's two venue stations."""
    k_spec = get_resolution("kalshi", city)
    p_spec = get_resolution("polymarket", city)
    if k_spec is None or p_spec is None:
        raise SystemExit(f"need both kalshi+polymarket specs for {city!r}")
    end = _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=days)
    a = await daily_highs_range(k_spec.station, k_spec.tz, start, end, use_cache=not refresh)
    await asyncio.sleep(2)
    b = await daily_highs_range(p_spec.station, p_spec.tz, start, end, use_cache=not refresh)
    return [(d, a[d], b[d]) for d in sorted(set(a) & set(b))]


def _agg(errors: List[float]) -> Dict[str, float]:
    n = len(errors)
    if n == 0:
        return {"n": 0, "mae": float("nan"), "rmse": float("nan"), "bias": float("nan")}
    mae = sum(abs(e) for e in errors) / n
    rmse = (sum(e * e for e in errors) / n) ** 0.5
    bias = sum(errors) / n
    return {"n": n, "mae": round(mae, 3), "rmse": round(rmse, 3), "bias": round(bias, 3)}


def walk_forward(history: List[Tuple[str, float, float]], warmup: int,
                 hot_threshold: float) -> Dict:
    """Day-by-day OOS prediction of KLGA from KNYC; errors per method + regime."""
    err = {"naive": [], "uncond": [], "cond": []}
    err_hot = {"naive": [], "uncond": [], "cond": []}
    err_cool = {"naive": [], "uncond": [], "cond": []}
    rows = []

    for t in range(warmup, len(history)):
        train = [(a, b) for (_, a, b) in history[:t]]   # PAST only — no leakage
        _, knyc, klga = history[t]
        uncond = fit_basis(train)
        cond = fit_conditional_basis(train, condition_on="A", edges=(80.0, hot_threshold))

        preds = {
            "naive": knyc,
            "uncond": knyc + uncond.mean,
            "cond": knyc + cond.model_for(knyc).mean,
        }
        is_hot = knyc >= hot_threshold
        for k, p in preds.items():
            e = klga - p
            err[k].append(e)
            (err_hot if is_hot else err_cool)[k].append(e)
        rows.append({"date": history[t][0], "knyc": knyc, "klga": klga,
                     "hot": is_hot, **{f"err_{k}": round(klga - p, 2) for k, p in preds.items()}})

    return {
        "overall": {k: _agg(v) for k, v in err.items()},
        "hot": {k: _agg(v) for k, v in err_hot.items()},
        "cool": {k: _agg(v) for k, v in err_cool.items()},
        "rows": rows,
    }


def _print(res: Dict, hot_threshold: float) -> None:
    def block(title, d):
        print(f"\n# {title}")
        print(f"  {'method':<8} {'n':>4} {'MAE':>7} {'RMSE':>7} {'bias':>7}")
        print("  " + "-" * 36)
        for m in ("naive", "uncond", "cond"):
            a = d[m]
            print(f"  {m:<8} {a['n']:>4} {a['mae']:>7.3f} {a['rmse']:>7.3f} {a['bias']:>+7.3f}")

    block("OVERALL (OOS, predicting KLGA from KNYC)", res["overall"])
    block(f"HOT days (KNYC >= {hot_threshold:.0f}F) — where the regime effect lives", res["hot"])
    block(f"COOL days (KNYC < {hot_threshold:.0f}F)", res["cool"])

    ov, ho = res["overall"], res["hot"]
    cond_beats_uncond = ho["cond"]["mae"] <= ho["uncond"]["mae"]
    uncond_beats_naive = ov["uncond"]["mae"] <= ov["naive"]["mae"]
    print("\n# VERDICT")
    print(f"#   conditioning helps on HOT days OOS: {'YES' if cond_beats_uncond else 'NO'} "
          f"(hot MAE cond {ho['cond']['mae']:.2f} vs uncond {ho['uncond']['mae']:.2f})")
    print(f"#   basis beats naive(delta=0) overall: {'YES' if uncond_beats_naive else 'NO'} "
          f"(MAE uncond {ov['uncond']['mae']:.2f} vs naive {ov['naive']['mae']:.2f})")
    print(f"#   irreducible OOS basis noise (cond RMSE): {ov['cond']['rmse']:.2f}F "
          f"-> any cross-venue hedge carries ~this much residual temp risk.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Walk-forward OOS validation of the station basis")
    ap.add_argument("--city", default="nyc")
    ap.add_argument("--days", type=int, default=730, help="history window to backtest over")
    ap.add_argument("--warmup", type=int, default=120, help="min training days before first prediction")
    ap.add_argument("--hot-threshold", type=float, default=85.0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)

    history = asyncio.run(_paired_history(args.city, args.days, args.refresh))
    print(f"# {args.city}: {len(history)} paired days "
          f"({history[0][0]}..{history[-1][0]}), warmup={args.warmup}")
    res = walk_forward(history, args.warmup, args.hot_threshold)
    _print(res, args.hot_threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
