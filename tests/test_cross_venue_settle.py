"""Cross-venue settlement + basis walk-forward — pure-function logic, no network."""

from src.tools.basis_backtest import _agg, walk_forward
from src.tools.settle_cross_venue import _resolved_yes, _trade_pnl


# --- settlement PnL -------------------------------------------------------

def test_resolved_yes_half_open():
    assert _resolved_yes(None, 87.0, 85.0) is True      # "<=86" with high 85
    assert _resolved_yes(None, 87.0, 87.0) is False     # hi exclusive
    assert _resolved_yes(87.0, 89.0, 87.0) is True      # lo inclusive
    assert _resolved_yes(87.0, None, 99.0) is True      # open top


def test_trade_pnl_buy_cheap():
    d = {"venue": "polymarket", "side": "buy_cheap", "yes_ask": 0.20, "market_mid": 0.18}
    assert _trade_pnl(d, won_yes=True) == 0.80          # 1 - 0.20, PM no fee
    assert _trade_pnl(d, won_yes=False) == -0.20


def test_trade_pnl_sell_rich_pays_when_no():
    d = {"venue": "polymarket", "side": "sell_rich", "yes_bid": 0.65, "market_mid": 0.66}
    assert _trade_pnl(d, won_yes=False) == 0.65         # short YES, resolved NO
    assert _trade_pnl(d, won_yes=True) == round(0.65 - 1.0, 4)  # -0.35


def test_trade_pnl_kalshi_charges_fee():
    d = {"venue": "kalshi", "side": "buy_cheap", "yes_ask": 0.50, "market_mid": 0.50}
    # fee = ceil(0.07*1*0.5*0.5*100)/100 = ceil(1.75)/100 = 0.02
    assert _trade_pnl(d, won_yes=True) == round(1.0 - 0.50 - 0.02, 4)


# --- walk-forward backtest ------------------------------------------------

def test_agg_basic():
    a = _agg([1.0, -1.0, 2.0, -2.0])
    assert a["n"] == 4 and a["bias"] == 0.0
    assert a["mae"] == 1.5
    assert abs(a["rmse"] - (10 / 4) ** 0.5) < 1e-3   # _agg rounds rmse to 3dp


def test_walk_forward_recovers_constant_basis():
    # Construct a history where KLGA = KNYC + 2 exactly, varying KNYC.
    hist = [(f"2025-01-{(i % 28) + 1:02d}", 70 + (i % 20), 72 + (i % 20)) for i in range(200)]
    res = walk_forward(hist, warmup=60, hot_threshold=85.0)
    # uncond/cond predict KLGA = KNYC + ~2 -> near-zero error; naive (delta 0) off by 2.
    assert res["overall"]["cond"]["mae"] < 0.1
    assert res["overall"]["uncond"]["mae"] < 0.1
    assert abs(res["overall"]["naive"]["bias"] - 2.0) < 1e-6   # KLGA - KNYC = +2
