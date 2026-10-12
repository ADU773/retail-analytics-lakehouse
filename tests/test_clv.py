from __future__ import annotations

import numpy as np

from retail_lakehouse.transform import clv


def test_bgnbd_recovers_simulated_parameters() -> None:
    true = clv.BgNbdParams(r=0.8, alpha=6.0, a=0.9, b=3.0)
    rng = np.random.default_rng(7)
    x, t_x, T = clv.simulate_bgnbd(rng, true, n=3_000, T=52.0)
    fit = clv.fit_bgnbd(x, t_x, T)
    # The mean purchase rate r/alpha is the best-identified quantity.
    assert abs(fit.r / fit.alpha - true.r / true.alpha) / (true.r / true.alpha) < 0.2
    # Aggregate forecast: expected purchases in the next 26 weeks are finite and positive.
    exp = clv.bgnbd_expected_purchases(fit, 26.0, x, t_x, T)
    assert np.all(np.isfinite(exp)) and np.all(exp >= 0)


def test_probability_alive_behaviour() -> None:
    p = clv.BgNbdParams(r=0.5, alpha=5.0, a=1.5, b=4.0)
    x = np.array([0.0, 5.0, 5.0])
    t_x = np.array([0.0, 50.0, 5.0])
    T = np.array([52.0, 52.0, 52.0])
    alive = clv.bgnbd_probability_alive(p, x, t_x, T)
    assert alive[0] == 1.0  # no repeat purchase -> cannot have dropped out (BG/NBD)
    assert alive[1] > alive[2]  # recent buyer more likely alive than one silent for 47 weeks
    assert np.all((alive >= 0) & (alive <= 1))
    e = clv.bgnbd_expected_purchases(p, 52.0, x, t_x, T)
    assert e[1] > e[2]


def test_gamma_gamma_shrinks_towards_population_mean() -> None:
    rng = np.random.default_rng(3)
    x = rng.integers(1, 8, size=2_000).astype(float)
    m = rng.gamma(4.0, 25.0, size=2_000)
    params = clv.fit_gamma_gamma(x, m)
    pop = params.p * params.v / (params.q - 1)
    ev = clv.gamma_gamma_expected_value(
        params, np.array([1.0, 50.0, 0.0]), np.array([500.0, 500.0, 0.0])
    )
    assert pop < ev[0] < ev[1] < 500.0 + 1e-6  # more purchases -> closer to own mean
    assert abs(ev[2] - pop) < 1e-6  # no repeat purchases -> population mean
