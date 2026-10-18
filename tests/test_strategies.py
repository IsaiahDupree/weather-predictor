"""Each strategy fires on the scenario it's designed for (synthetic snapshots)."""

from src.market.snapshot import MarketSnapshot, BucketQuote
from src.market.bucket import BucketGrid
from src.market.resolution import get_resolution
from src.strategies.tail_bucket_sniper import TailBucketSniper
from src.strategies.oracle_lag import OracleLag
from src.strategies.near_expiry_scalp import NearExpiryScalp
from src.strategies.intra_market_arb import IntraMarketArb
from src.strategies.bayesian_intraday import BayesianIntraday

GRID = BucketGrid.kalshi_style(69, 81, 2)
RES = get_resolution("kalshi", "nyc")


def _q(label, ya, na, depth=200):
    b = next(bb for bb in GRID if bb.label == label)
    return BucketQuote(bucket=b, symbol=f"KXHIGHNY-{label}", yes_ask=ya, yes_bid=ya - 0.01,
                       no_ask=na, no_bid=na - 0.01, yes_ask_levels=[(ya, depth)], yes_ask_size=depth)


def _snap(quotes, **kw):
    base = dict(venue="kalshi", city="nyc", event_ticker="KXHIGHNY-26MAY30", resolution=RES,
                grid=GRID, quotes=quotes, fee_per_contract=0.0)
    base.update(kw)
    return MarketSnapshot(**base)


def test_tail_bucket_sniper_fires_on_cheap_mispriced():
    quotes = [_q("79-80", 0.04, 0.96), _q("75-76", 0.30, 0.70)]
    fq = {"79-80": 0.12, "75-76": 0.28}
    sigs = TailBucketSniper().generate_signals(_snap(quotes, forecast_q=fq))
    labels = [s.metadata["bucket_label"] for s in sigs]
    assert "79-80" in labels                       # cheap (0.04) but forecast 0.12 -> +EV tail
    assert all(s.metadata["ev"] > 0 for s in sigs)


def test_near_expiry_scalp_only_when_locked():
    # high already 79.5, near expiry, '79-80' YES @ 0.96, low remaining vol -> fires
    snap = _snap([_q("79-80", 0.96, 0.04)], forecast_q={"79-80": 0.5},
                 obs_temp=79.5, daily_high_so_far=79.5, obs_age_s=20,
                 seconds_to_resolution=600, fraction_of_day_elapsed=0.95,
                 trend_per_hr=0.0, resid_sigma=0.3)
    sigs = NearExpiryScalp().generate_signals(snap)
    assert sigs and sigs[0].metadata["vol_gate_pass"] is True

    # same but far from expiry -> no scalp
    far = _snap([_q("79-80", 0.96, 0.04)], forecast_q={"79-80": 0.5},
                obs_temp=79.5, daily_high_so_far=79.5, seconds_to_resolution=6 * 3600,
                fraction_of_day_elapsed=0.4, trend_per_hr=0.0, resid_sigma=0.3)
    assert NearExpiryScalp().generate_signals(far) == []


def test_intra_market_arb_detects_dutch_book():
    quotes = [_q(b.label, 0.10, 0.90) for b in GRID]   # 8 * 0.10 = 0.80 < 1
    sigs = IntraMarketArb().generate_signals(_snap(quotes, forecast_q={}))
    assert len(sigs) == len(GRID)
    assert all(s.metadata["is_arbitrage"] for s in sigs)
    assert sigs[0].metadata["arb_profit_per_set"] > 0


def test_oracle_lag_emits_with_vol_gate_flag():
    # high near a locked top bucket, fresh obs, market lagging
    snap = _snap([_q(">=81", 0.30, 0.70)], forecast_q={">=81": 0.9},
                 obs_temp=82.0, daily_high_so_far=82.0, obs_age_s=20,
                 seconds_to_resolution=900, fraction_of_day_elapsed=0.9,
                 trend_per_hr=0.0, resid_sigma=0.5)
    sigs = OracleLag().generate_signals(snap)
    assert sigs and sigs[0].metadata["q"] > 0.5


def test_bayesian_intraday_updates_prior_with_obs():
    snap = _snap([_q("79-80", 0.10, 0.90)], forecast_q={"79-80": 0.30},
                 obs_temp=78.5, daily_high_so_far=78.5, obs_age_s=60,
                 seconds_to_resolution=2 * 3600, fraction_of_day_elapsed=0.8,
                 trend_per_hr=0.5, resid_sigma=1.0)
    sigs = BayesianIntraday().generate_signals(snap)
    # posterior should differ from the prior (obs moved it)
    if sigs:
        assert sigs[0].metadata["prior_q"] == 0.30
        assert "obs_weight" in sigs[0].metadata
