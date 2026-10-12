"""Customer lifetime value: BG/NBD (purchase frequency + dropout) and Gamma-Gamma (spend).

Implemented from the published likelihoods with numpy/scipy (no CLV library dependency):

* Fader, Hardie & Lee (2005), "Counting Your Customers the Easy Way: An Alternative to the
  Pareto/NBD Model" - BG/NBD log-likelihood, conditional expected purchases and P(alive).
* Fader, Hardie & Lee (2005), "RFM and CLV: Using Iso-Value Curves for Customer Base
  Analysis" - Gamma-Gamma model of average transaction value.

Inputs follow the usual convention: ``x`` = number of *repeat* purchase days, ``t_x`` = time
of the last purchase and ``T`` = customer age, both measured in weeks since the first
purchase. ``m`` = average value of the repeat purchases.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln, hyp2f1


@dataclass(frozen=True)
class BgNbdParams:
    """BG/NBD parameters: Gamma(r, alpha) purchase rates, Beta(a, b) dropout probabilities."""

    r: float
    alpha: float
    a: float
    b: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.r, self.alpha, self.a, self.b)


@dataclass(frozen=True)
class GammaGammaParams:
    """Gamma-Gamma parameters for the distribution of average transaction values."""

    p: float
    q: float
    v: float

    @property
    def population_mean(self) -> float:
        return self.p * self.v / (self.q - 1) if self.q > 1 else float("nan")


def bgnbd_log_likelihood(
    params: tuple[float, float, float, float], x: np.ndarray, t_x: np.ndarray, T: np.ndarray
) -> np.ndarray:
    """Per-customer BG/NBD log-likelihood."""
    r, alpha, a, b = params
    x = np.asarray(x, dtype=float)
    a1 = gammaln(r + x) - gammaln(r) + r * np.log(alpha)
    a2 = gammaln(a + b) + gammaln(b + x) - gammaln(b) - gammaln(a + b + x)
    a3 = -(r + x) * np.log(alpha + T)
    a4 = np.full_like(x, -np.inf)
    rep = x > 0
    a4[rep] = np.log(a) - np.log(b + x[rep] - 1) - (r + x[rep]) * np.log(alpha + t_x[rep])
    return a1 + a2 + np.logaddexp(a3, a4)


def fit_bgnbd(
    x: np.ndarray, t_x: np.ndarray, T: np.ndarray, penalizer: float = 1e-4
) -> BgNbdParams:
    """Maximum-likelihood BG/NBD fit (log-parameterised L-BFGS-B, small L2 penalty)."""
    x, t_x, T = (np.asarray(v, dtype=float) for v in (x, t_x, T))

    def nll(log_params: np.ndarray) -> float:
        params = np.exp(log_params)
        ll = bgnbd_log_likelihood(tuple(params), x, t_x, T)
        return -float(ll.mean()) + penalizer * float(np.sum(log_params**2))

    start = np.log([0.5, max(1.0, float(np.mean(T)) / 4), 1.0, 2.0])
    best = None
    for init in (start, start + np.array([0.0, 0.0, 0.5, 0.5])):
        res = minimize(nll, init, method="L-BFGS-B", bounds=[(-8.0, 8.0)] * 4)
        if best is None or res.fun < best.fun:
            best = res
    r, alpha, a, b = np.exp(best.x)  # type: ignore[union-attr]
    return BgNbdParams(float(r), float(alpha), float(a), float(b))


def bgnbd_expected_purchases(
    params: BgNbdParams, t: float, x: np.ndarray, t_x: np.ndarray, T: np.ndarray
) -> np.ndarray:
    """Expected number of purchases in the next ``t`` weeks given each customer's history."""
    r, alpha, a, b = params.as_tuple()
    x = np.asarray(x, dtype=float)
    with np.errstate(all="ignore"):
        hyp = hyp2f1(r + x, b + x, a + b + x - 1, t / (alpha + T + t))
        bracket = 1 - ((alpha + T) / (alpha + T + t)) ** (r + x) * hyp
        first = (a + b + x - 1) / (a - 1)
        denom = 1 + (x > 0) * (a / (b + x - 1)) * ((alpha + T) / (alpha + t_x)) ** (r + x)
        out = first * bracket / denom
    return np.where(np.isfinite(out) & (out >= 0), out, np.nan)


def bgnbd_probability_alive(
    params: BgNbdParams, x: np.ndarray, t_x: np.ndarray, T: np.ndarray
) -> np.ndarray:
    """P(customer is still active) given purchase history."""
    r, alpha, a, b = params.as_tuple()
    x = np.asarray(x, dtype=float)
    with np.errstate(all="ignore"):
        ratio = (a / (b + x - 1)) * ((alpha + T) / (alpha + t_x)) ** (r + x)
        out = 1.0 / (1.0 + np.where(x > 0, ratio, 0.0))
    return np.clip(out, 0.0, 1.0)


def gamma_gamma_log_likelihood(
    params: tuple[float, float, float], x: np.ndarray, m: np.ndarray
) -> np.ndarray:
    """Per-customer Gamma-Gamma log-likelihood (customers with x > 0 only)."""
    p, q, v = params
    px = p * x
    return (
        gammaln(px + q)
        - gammaln(px)
        - gammaln(q)
        + q * np.log(v)
        + (px - 1) * np.log(m)
        + px * np.log(x)
        - (px + q) * np.log(x * m + v)
    )


def fit_gamma_gamma(x: np.ndarray, m: np.ndarray, penalizer: float = 1e-4) -> GammaGammaParams:
    """Maximum-likelihood Gamma-Gamma fit on repeat customers."""
    x, m = np.asarray(x, dtype=float), np.asarray(m, dtype=float)
    mask = (x > 0) & (m > 0)
    x, m = x[mask], m[mask]

    def nll(log_params: np.ndarray) -> float:
        ll = gamma_gamma_log_likelihood(tuple(np.exp(log_params)), x, m)
        return -float(ll.mean()) + penalizer * float(np.sum(log_params**2))

    start = np.log([2.0, 3.0, max(1.0, float(np.mean(m)))])
    res = minimize(nll, start, method="L-BFGS-B", bounds=[(-6.0, 10.0)] * 3)
    p, q, v = np.exp(res.x)
    return GammaGammaParams(float(p), float(q), float(v))


def gamma_gamma_expected_value(
    params: GammaGammaParams, x: np.ndarray, m: np.ndarray
) -> np.ndarray:
    """Posterior mean transaction value (shrinks each customer's mean towards the population)."""
    p, q, v = params.p, params.q, params.v
    x = np.asarray(x, dtype=float)
    m = np.where(np.asarray(x) > 0, np.asarray(m, dtype=float), 0.0)
    pop = p * v / (q - 1)
    weight = (p * x) / (p * x + q - 1)
    return (1 - weight) * pop + weight * m


def simulate_bgnbd(
    rng: np.random.Generator, params: BgNbdParams, n: int, T: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Simulate (x, t_x, T) from a BG/NBD process - used for parameter-recovery tests."""
    lam = rng.gamma(params.r, 1.0 / params.alpha, size=n)
    p = rng.beta(params.a, params.b, size=n)
    x = np.zeros(n)
    t_x = np.zeros(n)
    for i in range(n):
        t = 0.0
        while True:
            t += rng.exponential(1.0 / lam[i])
            if t > T:
                break
            x[i] += 1
            t_x[i] = t
            if rng.random() < p[i]:
                break
    return x, t_x, np.full(n, T)
