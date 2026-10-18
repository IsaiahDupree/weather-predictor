"""Cross-venue station-basis edge — pure-function math + the directional/RV split.

No network. Validates the basis fit, regime conditioning, price<->pmf transforms,
the conditional convolution, and the date parsing + level diagnostic that decide
whether a cross-venue gap is calibration-free basis RV or a directional bet.
"""

import datetime as dt

from src.edge.station_basis import (
    BasisModel, convolve, convolve_conditional, fit_basis, fit_conditional_basis,
    normalize, pmf_to_bucket_probs, prices_to_pmf,
)
from src.tools.cross_venue_basis import _event_date, _implied_mean, _level_diagnostic


# --- basis fit ------------------------------------------------------------

def test_fit_basis_mean_and_pmf():
    pairs = [(80, 80), (80, 81), (80, 82), (80, 79)]  # deltas 0,1,2,-1
    m = fit_basis(pairs)
    assert m.n == 4
    assert abs(m.mean - 0.5) < 1e-9
    assert abs(sum(m.pmf.values()) - 1.0) < 1e-9
    assert m.pmf[0] == 0.25 and m.pmf[2] == 0.25


def test_fit_conditional_basis_separates_regimes():
    # Cool days (A<85): delta ~0. Hot days (A>=85): delta ~+2. The classic
    # KNYC/KLGA pattern. Conditioning must recover the two regimes.
    cool = [(70 + i % 5, 70 + i % 5) for i in range(20)]            # delta 0
    hot = [(88, 90), (89, 91), (90, 92), (87, 89), (88, 91)] * 2    # delta ~+2
    cbm = fit_conditional_basis(cool + hot, condition_on="A", edges=(85.0,))
    assert cbm.model_for(72).mean == 0.0           # cool regime
    assert cbm.model_for(89).mean >= 1.9           # hot regime ~+2
    # Sub-threshold-sample bins fall back to the pooled model.
    sparse = fit_conditional_basis([(90, 92)], condition_on="A", edges=(85.0,))
    assert sparse.model_for(50).n == 1             # fallback (pooled) used


# --- price <-> pmf transforms --------------------------------------------

def test_normalize_strips_overround():
    q = normalize({"a": 0.6, "b": 0.6})            # sums to 1.2 (book overround)
    assert abs(sum(q.values()) - 1.0) < 1e-9
    assert abs(q["a"] - 0.5) < 1e-9


def test_prices_to_pmf_spreads_and_sums_to_one():
    # One 2°F bucket [84,86) at q=1 -> mass split over degrees 84 and 85.
    pmf = prices_to_pmf([(84.0, 86.0, 1.0)])
    assert abs(pmf[84] - 0.5) < 1e-9 and abs(pmf[85] - 0.5) < 1e-9
    assert abs(sum(pmf.values()) - 1.0) < 1e-9


def test_pmf_to_bucket_probs_half_open():
    pmf = {85: 0.5, 86: 0.5}
    out = pmf_to_bucket_probs(pmf, [("lo", None, 86.0), ("hi", 86.0, None)])
    assert abs(out["lo"] - 0.5) < 1e-9   # 85 only (86 excluded by hi)
    assert abs(out["hi"] - 0.5) < 1e-9   # 86 only


def test_convolve_shifts_distribution():
    pmf = {84: 1.0}
    basis = BasisModel(pmf={2: 1.0}, mean=2.0, std=0.0, n=10)   # always +2
    out = convolve(pmf, basis, sign=1)
    assert out == {86: 1.0}                                     # 84 + 2
    back = convolve(out, basis, sign=-1)
    assert back == {84: 1.0}                                    # reverse


def test_convolve_conditional_uses_regime_basis():
    cool = [(70, 70)] * 10            # delta 0
    hot = [(88, 90)] * 10            # delta +2
    cbm = fit_conditional_basis(cool + hot, condition_on="A", edges=(85.0,))
    # A cool source mass stays put; a hot source mass shifts +2.
    out = convolve_conditional({72: 0.5, 89: 0.5}, cbm, sign=1)
    assert abs(out.get(72, 0) - 0.5) < 1e-9
    assert abs(out.get(91, 0) - 0.5) < 1e-9


# --- date parsing (venue close-convention robustness) ---------------------

def test_event_date_kalshi_and_pm():
    assert _event_date("KXHIGHNY-26JUN13", 0) == dt.date(2026, 6, 13)
    assert _event_date("highest-temperature-in-nyc-on-june-14-2026", 0) == dt.date(2026, 6, 14)
    # PM slug without an explicit year infers it from close_time.
    ct = dt.datetime(2026, 5, 30, 12, tzinfo=dt.timezone.utc).timestamp()
    assert _event_date("highest-temperature-in-nyc-on-may-30", ct) == dt.date(2026, 5, 30)


# --- directional vs basis-RV split ---------------------------------------

def _rows(pairs):
    return [{"label": l, "lo": lo, "hi": hi, "yes_mid": q} for l, lo, hi, q in pairs]


def test_implied_mean_tracks_price_mass():
    rows = _rows([("84-85", 84.0, 86.0, 1.0)])
    assert 84.0 <= _implied_mean(rows) <= 85.0


def test_level_diagnostic_flags_directional_vs_rv():
    cbm = fit_conditional_basis([(85, 86)] * 30, condition_on="A", edges=(85.0,))  # +1 basis
    k_rows = _rows([("84-85", 84.0, 86.0, 1.0)])     # KNYC ~84.5
    # PM agrees on level (KLGA ~85.5 ≈ 84.5 + 1 basis) -> BASIS-RV.
    rv = _level_diagnostic(k_rows, _rows([("85-86", 85.0, 87.0, 1.0)]), cbm)
    assert rv["verdict"].startswith("BASIS-RV")
    # PM way hotter (KLGA ~89.5) than basis justifies -> DIRECTIONAL.
    direc = _level_diagnostic(k_rows, _rows([("89-90", 89.0, 91.0, 1.0)]), cbm)
    assert direc["verdict"].startswith("DIRECTIONAL")
    assert direc["excess_gap_f"] > 1.0
