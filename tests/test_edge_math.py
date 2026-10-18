"""Edge math — validated against the source article's worked examples."""

import math

from src.edge.ev import ev_full, edge, ev_per_dollar_staked, executable_cost, ev_against_book
from src.edge.kelly import kelly_fraction, edge_ratio, fixed_scaled, size_position
from src.edge.bayesian import bayes_update, posterior_exceeds, horizon_obs_weight
from src.edge.nowcast import project, prob_exceeds, prob_in_interval, normal_cdf
from src.edge.vol_gate import passes, safety_margin
from src.edge.arb_scanner import binary_arb, scan
from src.edge.distribution import normal_to_bucket_probs, high_market_bucket_probs, disagreement
from src.market.bucket import BucketGrid


def test_ev_full_matches_article():
    assert abs(ev_full(0.60, 0.40) - 0.20) < 1e-12
    assert abs(ev_full(0.35, 0.40) - (-0.05)) < 1e-12
    # full form == q - p
    assert abs(ev_full(0.62, 0.55) - edge(0.62, 0.55)) < 1e-12


def test_kelly_matches_article():
    assert abs(kelly_fraction(0.60, 0.40) - 0.3333) < 1e-3
    assert abs(kelly_fraction(0.45, 0.40) - 0.0833) < 1e-3
    assert kelly_fraction(0.40, 0.40) == 0.0       # no edge -> no bet
    assert kelly_fraction(0.30, 0.40) == 0.0       # negative edge -> no bet


def test_edge_ratio_taipei():
    assert abs(edge_ratio(0.007) - 141.857) < 0.01   # ~142x payoff


def test_bayes_chicago_example():
    assert abs(bayes_update(0.35, 0.85, 0.39) - 0.7628) < 1e-3


def test_binary_arb():
    assert abs(binary_arb(0.46, 0.50) - 0.04) < 1e-12
    assert binary_arb(0.55, 0.50) is None            # sums > 1, no arb


def test_executable_cost_sweeps_book():
    avg, filled, cost = executable_cost([(0.40, 5), (0.45, 10)], 8)
    assert filled == 8
    assert abs(cost - (5 * 0.40 + 3 * 0.45)) < 1e-9
    assert abs(avg - cost / 8) < 1e-9


def test_ev_against_book_nets_fees():
    ev, avg, filled = ev_against_book(0.60, [(0.40, 100)], 10, fee_per_contract=0.01)
    assert filled == 10
    assert abs(ev - (0.60 - 0.40 - 0.01)) < 1e-9


def test_fixed_scaled_clip():
    assert abs(fixed_scaled(0.20, base_clip=2.0, threshold=0.05, width=0.20) - 1.5) < 1e-12
    assert fixed_scaled(0.04, base_clip=2.0, threshold=0.05) == 0.0   # below threshold
    # capped at cap_mult * base_clip
    assert abs(fixed_scaled(5.0, base_clip=2.0, threshold=0.0, width=0.1, cap_mult=3.0) - 6.0) < 1e-12


def test_size_position_takes_minimum_and_caps():
    # fixed-scaled small clip dominates Kelly notional; result capped at min of all
    n = size_position(q=0.6, price=0.4, bankroll=1000, lam=0.25, base_clip=2.0,
                      ev_threshold=0.05, max_order_notional=25, liquidity_cap_notional=1000)
    assert 0 < n <= 25
    # liquidity cap binds
    n2 = size_position(q=0.9, price=0.5, bankroll=10000, lam=0.5, base_clip=50,
                       ev_threshold=0.05, max_order_notional=10000, liquidity_cap_notional=3.0)
    assert n2 == 3.0


def test_vol_gate():
    assert passes(5.0, 2.0, k=3.0) is False      # 5 < 3*2=6 -> not safe, skip
    assert passes(8.0, 2.0, k=3.0) is True       # 8 >= 6 -> safe
    assert passes(-1.0, 2.0) is False            # wrong side of the threshold
    assert passes(5.0, 0.0) is True              # nothing left to move
    assert math.isinf(safety_margin(5.0, 0.0))


def test_distribution_sums_to_one_and_monotonic_high():
    grid = BucketGrid.kalshi_style(69, 81, 2)
    probs = normal_to_bucket_probs(75.0, 2.5, grid)
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    # daily-high monotonicity: buckets entirely below the high-so-far are dead
    hp = high_market_bucket_probs(76.0, mu=77.0, sigma=2.0, grid=grid)
    assert hp["69-70"] == 0.0 and hp["71-72"] == 0.0 and hp["73-74"] == 0.0
    assert hp[">=81"] > 0.0
    assert abs(sum(hp.values()) - 1.0) < 1e-6


def test_nowcast_projection_and_prob():
    mu, sigma = project(72.0, trend_per_hr=2.0, hours_remaining=4.0, resid_sigma_per_sqrt_hr=1.5)
    assert mu > 72.0 and sigma > 0
    assert 0.0 <= prob_exceeds(mu, sigma, 90.0) <= 1.0
    assert abs(prob_in_interval(75, 2, None, None) - 1.0) < 1e-9


def test_disagreement():
    a = {"x": 0.8, "y": 0.2}
    b = {"x": 0.2, "y": 0.8}
    assert abs(disagreement([a, b]) - 0.6) < 1e-9


def test_arb_scan_dutch_book():
    books = {"A": [(0.30, 50)], "B": [(0.30, 50)], "C": [(0.30, 50)]}
    opp = scan(books, set_qty=10, fee_per_contract=0.0)
    assert opp is not None and opp.is_profitable
    assert abs(opp.profit_per_set - (1.0 - 0.90)) < 1e-9
    # no arb when asks sum to >= 1
    assert scan({"A": [(0.6, 50)], "B": [(0.6, 50)]}, set_qty=5) is None
