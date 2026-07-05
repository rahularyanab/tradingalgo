"""
Order placement via Zerodha KiteConnect.
All option selling trades use NRML product (supports overnight holding).

NFO market orders are blocked via API — all orders use ORDER_TYPE_LIMIT.
  SELL entry/exit : pass current LTP — function subtracts ₹2 internally to cross bid
  BUY  entry/exit : pass current LTP — function adds ₹2 internally to cross ask
"""

import math
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def _tick(price: float, up: bool = False) -> float:
    """Round to nearest ₹0.05 tick. up=True for buys (round up), False for sells (round down)."""
    ticked = (math.ceil if up else math.floor)(price / 0.05) * 0.05
    return max(ticked, 0.05)


@dataclass
class OrderResult:
    success:    bool
    order_id:   Optional[str]
    error:      Optional[str]
    symbol:     str
    action:     str     # "BUY" | "SELL"
    quantity:   int
    order_type: str


def place_sell_order(
    kite,
    symbol:   str,
    quantity: int,
    product:  str   = "NRML",
    price:    Optional[float] = None,
) -> OrderResult:
    """
    Place a SELL limit order for an NFO option.
    price: current LTP (required — NFO market orders blocked via API).
    """
    if price is None:
        logger.warning(f"place_sell_order called without price for {symbol} — order will likely fail")
    order_kwargs = dict(
        variety          = kite.VARIETY_REGULAR,
        exchange         = kite.EXCHANGE_NFO,
        tradingsymbol    = symbol,
        transaction_type = kite.TRANSACTION_TYPE_SELL,
        quantity         = quantity,
        product          = product,
        order_type       = kite.ORDER_TYPE_LIMIT if price is not None else kite.ORDER_TYPE_MARKET,
    )
    if price is not None:
        # Subtract ₹2 to cross current bid — avoids the order sitting pending when LTP > bid
        order_kwargs["price"] = _tick(price - 2, up=False)
    try:
        order_id = kite.place_order(**order_kwargs)
        logger.info(f"SELL order placed: {symbol}  qty={quantity}  price={order_kwargs.get('price', 'MARKET')}  order_id={order_id}")
        return OrderResult(True, str(order_id), None, symbol, "SELL", quantity, product)
    except Exception as e:
        logger.error(f"SELL order failed: {symbol}  error={e}")
        return OrderResult(False, None, str(e), symbol, "SELL", quantity, product)


def place_buy_order(
    kite,
    symbol:   str,
    quantity: int,
    product:  str   = "NRML",
    price:    Optional[float] = None,
) -> OrderResult:
    """
    Place a BUY limit order (covering a sold option).
    price: current LTP + buffer (required — NFO market orders blocked via API).
    """
    if price is None:
        logger.warning(f"place_buy_order called without price for {symbol} — order will likely fail")
    order_kwargs = dict(
        variety          = kite.VARIETY_REGULAR,
        exchange         = kite.EXCHANGE_NFO,
        tradingsymbol    = symbol,
        transaction_type = kite.TRANSACTION_TYPE_BUY,
        quantity         = quantity,
        product          = product,
        order_type       = kite.ORDER_TYPE_LIMIT if price is not None else kite.ORDER_TYPE_MARKET,
    )
    if price is not None:
        # Add ₹2 to cross current ask — ensures immediate fill
        order_kwargs["price"] = _tick(price + 2, up=True)
    try:
        order_id = kite.place_order(**order_kwargs)
        logger.info(f"BUY order placed: {symbol}  qty={quantity}  price={order_kwargs.get('price', 'MARKET')}  order_id={order_id}")
        return OrderResult(True, str(order_id), None, symbol, "BUY", quantity, product)
    except Exception as e:
        logger.error(f"BUY order failed: {symbol}  error={e}")
        return OrderResult(False, None, str(e), symbol, "BUY", quantity, product)


def place_spread_entry(
    kite,
    sell_symbol:  str,
    hedge_symbol: str,
    quantity:     int,
    product:      str            = "NRML",
    sell_price:   Optional[float] = None,
    hedge_price:  Optional[float] = None,
) -> tuple[OrderResult, OrderResult]:
    """
    Enter a spread: BUY hedge first (margin benefit), then SELL main strike.
    Returns (hedge_buy_result, main_sell_result).
    """
    logger.info(f"Spread entry: BUY {hedge_symbol}@{hedge_price} then SELL {sell_symbol}@{sell_price}  qty={quantity}")
    hedge_result = place_buy_order(kite, hedge_symbol, quantity, product, price=hedge_price)
    if not hedge_result.success:
        logger.error("Hedge buy failed — aborting spread entry")
        return hedge_result, OrderResult(False, None, "Hedge buy failed", sell_symbol, "SELL", quantity, product)
    main_result = place_sell_order(kite, sell_symbol, quantity, product, price=sell_price)
    return hedge_result, main_result


def place_spread_exit(
    kite,
    sell_symbol:  str,
    hedge_symbol: str,
    quantity:     int,
    product:      str            = "NRML",
    buy_price:    Optional[float] = None,
    hedge_sell_price: Optional[float] = None,
) -> tuple[OrderResult, OrderResult]:
    """
    Exit a spread: BUY back sold option first, then SELL the hedge.
    Returns (main_buy_result, hedge_sell_result).
    """
    logger.info(f"Spread exit: BUY {sell_symbol}@{buy_price} then SELL {hedge_symbol}@{hedge_sell_price}  qty={quantity}")
    main_result  = place_buy_order(kite,  sell_symbol,  quantity, product, price=buy_price)
    hedge_result = place_sell_order(kite, hedge_symbol, quantity, product, price=hedge_sell_price)
    return main_result, hedge_result
