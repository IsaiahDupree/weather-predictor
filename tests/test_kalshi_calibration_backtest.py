"""Kalshi market-calibration backtest — pure-function logic, no network."""

from src.tools.kalshi_history import _candle_mid, market_result
from src.tools.kalshi_calibration_backtest import backtest, brier_ece, reliability


# --- candle parsing -------------------------------------------------------

def test_candle_mid_prefers_bid_ask():
    c = {"yes_bid": {"close_dollars": "0.40"}, "yes_ask": {"close_dollars": "0.44"},
         "price": {"close_dollars": "0.50"}}
    assert _candle_mid(c) == 0.42


def test_candle_mid_falls_back_to_last_price():
    c = {"yes_bid": {}, "yes_ask": {}, "price": {"close_dollars": "0.37", "mean_dollars": "0.39"}}
    assert _candle_mid(c) == 0.37
    assert _candle_mid({"price": {"mean_dollars": "0.39"}}) == 0.39
    assert _candle_mid({"price": {}}) is None


def test_market_result():
    assert market_result({"result": "yes"}) == 1
    assert market_result({"result": "no"}) == 0
    assert market_result({"result": ""}) is None


# --- reliability + metrics ------------------------------------------------

def _s(mid, result, bid=None, ask=None):
    return {"mid": mid, "result": result, "bid": bid if bid is not None else mid,
            "ask": ask if ask is not None else mid, "volume": 100}


def test_reliability_bins_and_gap():
    # 10 samples at 0.05, none win -> low bin realized 0, gap ~ -0.05.
    samples = [_s(0.05, 0) for _ in range(10)] + [_s(0.95, 1) for _ in range(10)]
    table = reliability(samples)
    low = [b for b in table if b["lo"] == 0.0][0]
    hi = [b for b in table if b["lo"] == 0.9][0]
    assert low["n"] == 10 and low["realized_yes"] == 0.0 and low["gap"] < 0
    assert hi["n"] == 10 and hi["realized_yes"] == 1.0


def test_brier_ece_perfect_market():
    # Prices exactly match outcomes -> Brier 0, ECE 0.
    samples = [_s(0.0, 0), _s(1.0, 1)]
    table = reliability(samples)
    m = brier_ece(samples, table)
    assert m["brier"] == 0.0 and m["ece"] == 0.0


def test_backtest_longshot_overpriced_fade_profits_gross():
    # Longshots priced 0.10 that NEVER win: fading (short) profits ~0.10-fee each.
    samples = [_s(0.10, 0) for _ in range(20)]
    bt = backtest(samples, longshot_max=0.15, favorite_min=0.85)
    assert bt["fade_longshots"]["n"] == 20
    assert bt["fade_longshots"]["total_pnl"] > 0      # 0.10/contract minus small fee
    assert bt["buy_longshots"]["total_pnl"] < 0       # buying them loses
    # hit% counts the directional thesis paying off.
    assert bt["fade_longshots"]["hit%"] == 100.0


def test_backtest_uses_honest_fills():
    # Buy at ask (0.12) not mid (0.10); winner pays 1 -> pnl ~ 1-0.12-fee.
    s = [_s(0.10, 1, bid=0.08, ask=0.12)]
    bt = backtest(s, longshot_max=0.15, favorite_min=0.85)
    # buy_longshots buys at ask 0.12: pnl = 1 - 0.12 - fee(0.12)
    assert 0.85 < bt["buy_longshots"]["pnl_per_trade"] < 0.88
