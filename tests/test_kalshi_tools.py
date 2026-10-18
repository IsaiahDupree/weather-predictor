"""Kalshi capture + settlement/calibration tools — pure-function behaviour.

No network: these exercise the bucket/Brier/backtest math and the same-climate-day
look-ahead gate against hand-built records (the same JSON shape the live tools emit).
"""

import time

from src.market.bucket import Bucket, BucketGrid
from src.market.resolution import get_resolution
from src.market.snapshot import BucketQuote, MarketSnapshot
from src.tools.kalshi_edge_snapshot import _ask_depth, _bucket_rows, _is_same_climate_day
from src.tools.kalshi_settle_calibrate import (
    _backtest_edges, _brier, _lst_window_utc, _winning_bucket,
)


# --- settlement scoring ---------------------------------------------------

def _buckets():
    # NYC-like grid: <=86, 87-88, 89-90, 91-92 with forecast_q + asks.
    return [
        {"bucket": "86 or below", "lo": None, "hi": 87.0, "yes_ask": 0.80,
         "ask_depth": 100, "forecast_q": 0.12, "obs_nowcast_q": None,
         "ev_fcst": 0.12 - 0.80, "ev_obs": None},
        {"bucket": "87 to 88", "lo": 87.0, "hi": 89.0, "yes_ask": 0.22,
         "ask_depth": 100, "forecast_q": 0.37, "obs_nowcast_q": None,
         "ev_fcst": 0.37 - 0.22, "ev_obs": None},
        {"bucket": "89 to 90", "lo": 89.0, "hi": 91.0, "yes_ask": 0.03,
         "ask_depth": 100, "forecast_q": 0.25, "obs_nowcast_q": None,
         "ev_fcst": 0.25 - 0.03, "ev_obs": None},
        {"bucket": "91 to 92", "lo": 91.0, "hi": 93.0, "yes_ask": 0.01,
         "ask_depth": 100, "forecast_q": 0.25, "obs_nowcast_q": None,
         "ev_fcst": 0.25 - 0.01, "ev_obs": None},
    ]


def test_winning_bucket_half_open():
    b = _buckets()
    assert _winning_bucket(b, 86.0) == "86 or below"   # open lower edge
    assert _winning_bucket(b, 88.0) == "87 to 88"
    assert _winning_bucket(b, 89.0) == "89 to 90"       # lo inclusive
    assert _winning_bucket(b, 92.0) == "91 to 92"
    assert _winning_bucket(b, 200.0) is None            # above every bucket


def test_brier_perfect_and_worst():
    # A grid that puts all mass on the winner scores 0; all mass off scores 2.
    perfect = [
        {"bucket": "win", "forecast_q": 1.0},
        {"bucket": "lose", "forecast_q": 0.0},
    ]
    assert _brier(perfect, "forecast_q", "win") == 0.0
    worst = [
        {"bucket": "win", "forecast_q": 0.0},
        {"bucket": "lose", "forecast_q": 1.0},
    ]
    assert _brier(worst, "forecast_q", "win") == 2.0
    # No q on any bucket -> not scorable.
    assert _brier([{"bucket": "x", "forecast_q": None}], "forecast_q", "x") is None


def test_backtest_edges_pnl_settles_one_dollar():
    b = _buckets()
    # ev_fcst > 0 on three buckets (87-88 @0.22, 89-90 @0.03, 91-92 @0.01);
    # only "86 or below" (q 0.12 vs ask 0.80) is -EV and never bet.
    n, wins, pnl = _backtest_edges(b, "89 to 90", "ev_fcst", min_depth=5.0)
    assert n == 3 and wins == 1          # one flagged bucket resolved YES
    assert pnl > 0                       # the $1 longshot win outweighs cheap losers
    # If the only un-flagged bucket wins, every flagged bet loses -> negative PnL.
    n2, wins2, pnl2 = _backtest_edges(b, "86 or below", "ev_fcst", min_depth=5.0)
    assert n2 == 3 and wins2 == 0 and pnl2 < 0


def test_backtest_respects_min_depth():
    b = _buckets()
    for row in b:
        row["ask_depth"] = 1  # below the gate
    n, wins, pnl = _backtest_edges(b, "89 to 90", "ev_fcst", min_depth=5.0)
    assert n == 0 and pnl == 0.0


def test_lst_window_is_local_midnight_to_midnight():
    import datetime as dt
    start, end = _lst_window_utc("America/New_York", dt.date(2026, 6, 12))
    assert (end - start) == dt.timedelta(days=1)
    # EDT in June = UTC-4, so local midnight 06-12 == 04:00Z 06-12.
    assert start.hour == 4 and start.day == 12


# --- look-ahead gate (capture side) ---------------------------------------

def _snap(hours_to_res: float):
    spec = get_resolution("kalshi", "nyc")
    grid = BucketGrid([Bucket("86 or below", None, 87.0)])
    q = BucketQuote(bucket=grid.buckets[0], symbol="X", yes_bid=0.1, yes_ask=0.12)
    return MarketSnapshot(
        venue="kalshi", city="nyc", event_ticker="KXHIGHNY-X", resolution=spec,
        grid=grid, quotes=[q], forecast_q={"86 or below": 0.5},
        obs_temp=80.0, daily_high_so_far=80.0, trend_per_hr=1.0,
        seconds_to_resolution=hours_to_res * 3600.0, ts=time.time(),
    )


def test_same_day_gate_today_vs_next_day():
    # ~10h out resolves today; ~35h out is the next climate day.
    assert _is_same_climate_day(_snap(10.0)) is True
    assert _is_same_climate_day(_snap(35.0)) is False


def test_obs_nowcast_nulled_on_next_day_events():
    # On a next-day event the obs read must be suppressed (look-ahead guard).
    rows_today = _bucket_rows(_snap(10.0), same_day=True)
    rows_next = _bucket_rows(_snap(10.0), same_day=False)
    assert rows_today[0]["obs_nowcast_q"] is not None
    assert rows_next[0]["obs_nowcast_q"] is None
    assert rows_next[0]["ev_obs"] is None


def test_ask_depth_sums_levels():
    grid = BucketGrid([Bucket("x", None, 87.0)])
    q = BucketQuote(bucket=grid.buckets[0], symbol="X",
                    yes_ask_levels=[(0.1, 50.0), (0.11, 30.0)], yes_ask_size=999)
    assert _ask_depth(q) == 80.0  # prefers the summed ladder over the scalar size
