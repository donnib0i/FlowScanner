"""
core/greeks.py -- Black-Scholes greeks and implied volatility. Pure math: no
I/O, no pandas, no network, so it can be checked against independently computed
values.

Conventions, fixed because every consumer depends on them:
  delta   per $1 of spot
  gamma   per $1 of spot, per $1 of spot
  vega    per 1 percentage point of implied vol
  theta   per calendar day
  vanna   dDelta/dVol, per 1 percentage point of vol
  charm   dDelta/dTime, per calendar day
  r       defaults to 0.0

The zero rate is not an oversight. `bs_delta` has always assumed it, and every
signal in the repo is calibrated on deltas produced that way; changing the
default would shift them all underneath thresholds tuned against them. For 0DTE
the rate term is negligible regardless.

`core.options` imports and re-exports `norm_cdf` and `bs_delta` from here, so
`core.scanner`'s public surface is unchanged.
"""
from typing import Dict, Optional
import math

# Floor for time to expiry: one minute, expressed in years. Gamma diverges as
# T -> 0 rather than converging to a limit, so without a floor an ATM 0DTE
# strike returns a number that swamps every other strike in a chain.
_MIN_T = 1.0 / (365 * 1440)

# Floor for volatility, matching the long-standing bs_delta behaviour.
_MIN_SIGMA = 0.05

_SQRT_2PI = math.sqrt(2 * math.pi)


def norm_cdf(x: float) -> float:
    """Abramowitz & Stegun approximation (max error 7.5e-8)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    c = 1.0 - (1.0 / _SQRT_2PI) * math.exp(-0.5 * x * x) * p
    return c if x >= 0 else 1.0 - c


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def bs_delta(S: float, K: float, T: float, sigma: float, opt_type: str = "call") -> float:
    """Black-Scholes delta without scipy."""
    T     = max(T, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    if S <= 0 or K <= 0:
        return (1.0 if opt_type == "call" else -1.0) if S > K else 0.0
    # Near expiry: d1 → ±∞ and delta collapses to 0/1. Use limit directly.
    if T < 0.0001:   # < ~52 minutes — digital payoff regime
        if opt_type == "call":
            return 1.0 if S >= K else 0.0
        else:
            return -1.0 if S <= K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d  = norm_cdf(d1)
    return d if opt_type == "call" else d - 1.0


def _zero_greeks(S: float, K: float, opt_type: str) -> Dict[str, float]:
    """Degenerate inputs: return a finite, directionally sane result."""
    if opt_type == "call":
        delta = 1.0 if S > K else 0.0
    else:
        delta = -1.0 if S < K else 0.0
    return {"delta": delta, "gamma": 0.0, "vega": 0.0,
            "theta": 0.0, "vanna": 0.0, "charm": 0.0}


def bs_greeks(S: float, K: float, T: float, sigma: float,
              r: float = 0.0, opt_type: str = "call") -> Dict[str, float]:
    """
    All greeks in one pass. Every value is finite for every input: callers
    aggregate thousands of these and a single nan poisons the whole sum.

    Note delta here is the continuous Black-Scholes delta, not the digital
    limit `bs_delta` switches to inside 52 minutes. Gamma is meaningless in that
    limit (the digital delta is a step function), so this keeps the smooth form
    and relies on the T floor to bound it.
    """
    if S <= 0 or K <= 0 or not math.isfinite(S) or not math.isfinite(K):
        return _zero_greeks(S, K, opt_type)

    T     = max(T, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    sqrt_T = math.sqrt(T)

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf = norm_pdf(d1)

    gamma = pdf / (S * sigma * sqrt_T)
    vega  = S * pdf * sqrt_T / 100.0          # per 1 vol point
    theta = -(S * pdf * sigma) / (2 * sqrt_T) / 365.0   # per day, r=0 term only
    if r:
        disc = math.exp(-r * T)
        if opt_type == "call":
            theta -= (r * K * disc * norm_cdf(d2)) / 365.0
        else:
            theta += (r * K * disc * norm_cdf(-d2)) / 365.0

    delta = norm_cdf(d1) if opt_type == "call" else norm_cdf(d1) - 1.0

    # vanna = dDelta/dVol; sign flips across the money with d2.
    vanna = -pdf * d2 / sigma / 100.0
    # charm = dDelta/dTime, per day.
    charm = -pdf * (2 * r * T - d2 * sigma * sqrt_T) / (2 * T * sigma * sqrt_T) / 365.0
    if opt_type == "put":
        charm = charm   # charm of a put differs only by the carry term, zero at r=0

    out = {"delta": delta, "gamma": gamma, "vega": vega,
           "theta": theta, "vanna": vanna, "charm": charm}
    return {k: (v if math.isfinite(v) else 0.0) for k, v in out.items()}


def _bs_price(S: float, K: float, T: float, sigma: float,
              r: float, opt_type: str) -> float:
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    disc = math.exp(-r * T)
    if opt_type == "call":
        return S * norm_cdf(d1) - K * disc * norm_cdf(d2)
    return K * disc * norm_cdf(-d2) - S * norm_cdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float,
                r: float = 0.0, opt_type: str = "call") -> Optional[float]:
    """
    Back-solve volatility from a contract's mid price.

    Exists because yfinance returns impliedVolatility of 0.0 or NaN for a large
    share of far-OTM strikes -- exactly the wing strikes carrying the biggest
    open interest. Zero IV means zero gamma, which silently deletes those
    strikes from a gamma profile and biases everything toward the money.

    Returns None rather than a number when the price is unattainable. A wrong
    volatility is worse than a missing one: the caller can count and report an
    exclusion, but it cannot detect a plausible-looking lie.
    """
    if not (math.isfinite(price) and price > 0) or S <= 0 or K <= 0:
        return None
    T = max(T, _MIN_T)

    disc = math.exp(-r * T)
    intrinsic = max(S - K * disc, 0.0) if opt_type == "call" else max(K * disc - S, 0.0)
    upper = S if opt_type == "call" else K * disc
    # Below intrinsic is arbitrage; at or above the bound no finite vol reaches it.
    if price < intrinsic - 1e-12 or price >= upper:
        return None

    lo, hi = 1e-4, 5.0
    sigma = 0.20

    # Newton first: quadratic convergence where vega is meaningful.
    for _ in range(50):
        try:
            diff = _bs_price(S, K, T, sigma, r, opt_type) - price
        except (ValueError, OverflowError):
            break
        if abs(diff) < 1e-8:
            return sigma
        vega = S * norm_pdf(
            (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        ) * math.sqrt(T)
        if vega < 1e-8:
            break                      # deep OTM: vega underflows, Newton stalls
        sigma -= diff / vega
        if not (lo < sigma < hi):
            break                      # wandered out of range; bisect instead

    # Bisection fallback. Deep OTM this is the expected path, not an edge case.
    lo, hi = 1e-4, 5.0
    try:
        if _bs_price(S, K, T, hi, r, opt_type) < price:
            return None
    except (ValueError, OverflowError):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        try:
            val = _bs_price(S, K, T, mid, r, opt_type)
        except (ValueError, OverflowError):
            return None
        if abs(val - price) < 1e-9:
            return mid
        if val < price:
            lo = mid
        else:
            hi = mid
    mid = 0.5 * (lo + hi)
    return mid if 1e-3 < mid < 5.0 else None
