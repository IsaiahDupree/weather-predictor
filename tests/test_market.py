"""Resolution map + bucket grid."""

from src.market.resolution import get_resolution, station_for, known_cities, STATION_COORDS
from src.market.bucket import Bucket, BucketGrid, winning_bucket
from src.market.fees import kalshi_fee, fee_per_contract


def test_venues_use_different_nyc_stations():
    # The load-bearing correction: Kalshi NYC = Central Park, Polymarket NYC = LaGuardia.
    assert station_for("kalshi", "nyc") == "KNYC"
    assert station_for("polymarket", "nyc") == "KLGA"
    assert get_resolution("kalshi", "chicago").station == "KMDW"   # Midway, not O'Hare


def test_resolution_units_and_coords():
    assert get_resolution("kalshi", "nyc").units == "F"
    assert get_resolution("polymarket", "taipei").units == "C"
    assert get_resolution("polymarket", "taipei").station == "466920"
    assert get_resolution("kalshi", "nyc").latlon in STATION_COORDS.values()
    assert get_resolution("nope", "nowhere") is None


def test_known_cities():
    assert "nyc" in known_cities("kalshi")
    assert "taipei" in known_cities("polymarket")


def test_bucket_contains_half_open():
    b = Bucket("71-72", 71, 73)      # [71, 73)
    assert b.contains(71) and b.contains(72.9) and not b.contains(73) and not b.contains(70)


def test_kalshi_style_grid_edges_and_which():
    grid = BucketGrid.kalshi_style(69, 81, 2)
    assert grid.buckets[0].lo is None      # open below
    assert grid.buckets[-1].hi is None     # open above
    assert grid.which(60).label == "<=68"
    assert grid.which(90).label == ">=81"
    assert grid.which(75).label == "75-76"
    assert winning_bucket(grid, 79.4).label == "79-80"


def test_probs_from_samples():
    grid = BucketGrid.kalshi_style(69, 81, 2)
    probs = grid.probs_from_samples([75, 75, 76, 80, 90])
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs[">=81"] == 0.2     # one of five samples (90)


def test_kalshi_parabolic_fee_peaks_midbook():
    # fee is highest near p=0.5 and small at the extremes
    assert kalshi_fee(0.5, 100) > kalshi_fee(0.05, 100)
    assert fee_per_contract("polymarket", 0.5) == 0.0
    assert fee_per_contract("kalshi", 0.5) > 0.0
