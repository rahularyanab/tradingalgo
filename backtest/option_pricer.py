"""
Black-Scholes option pricing for Nifty European options.

Used in backtesting to estimate option premiums at entry and exit,
since historical option chain data is not freely available.

Inputs:
  S     - Nifty spot price
  K     - strike price
  T     - time to expiry in years (e.g., 5 days = 5/365)
  sigma - annualised implied volatility (e.g., 0.14 for 14%)
  r     - risk-free rate (Indian repo rate, default 6.5%)
"""

import enum
import numpy as np
from scipy.stats import norm


class OptionType(enum.Enum):
    CALL = "CE"
    PUT  = "PE"


RISK_FREE_RATE = 0.065   # RBI repo rate (annualised)


def price_option(
    S: float,
    K: float,
    T: float,
    sigma: float,
    option_type: OptionType,
    r: float = RISK_FREE_RATE,
) -> float:
    """Return Black-Scholes theoretical price of a European option."""
    if T <= 0:
        # At expiry: intrinsic value only
        if option_type == OptionType.CALL:
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_type == OptionType.CALL:
        price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return max(round(float(price), 2), 0.05)


def price_option_at_spot(
    spot: float,
    strike: int,
    days_to_expiry: float,
    sigma: float,
    option_type: OptionType,
) -> float:
    """Convenience wrapper: accepts days instead of years."""
    T = max(days_to_expiry / 365.25, 0.0)
    return price_option(spot, strike, T, sigma, option_type)


def estimate_iv_from_vix(india_vix: float) -> float:
    """
    India VIX is the 30-day IV for Nifty (annualised %).
    For weekly options, scale up slightly (shorter term = higher IV).
    """
    return (india_vix / 100) * 1.1
