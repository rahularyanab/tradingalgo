"""
Zerodha paper trading: real Zerodha market data, SIMULATED orders.

Wraps a KiteConnect client. Market-data calls (ltp, quote, historical_data, instruments, ...) go to Zerodha
unchanged; every order / position / order-book call is answered by a PaperLedger (broker/angel_paper.py),
so nothing is ever sent to the exchange and the account's real positions are never seen by the bot (the
owner's own trades in the same account can't be mistaken for the paper bot's).

The data login is the owner's daily Zerodha token (read-only use): see broker.load_token().
"""
import logging

from broker.angel_paper import PaperLedger

logger = logging.getLogger(__name__)

_ORDER_METHODS = {"place_order", "modify_order", "cancel_order", "positions", "orders", "order_history",
                  "trades", "holdings", "margins", "exit_order", "place_gtt", "modify_gtt", "delete_gtt", "convert_position"}


class PaperKite:
    paper = True

    def __init__(self, kite, ledger: PaperLedger, exchange: str):
        self._kite = kite
        self.ledger = ledger
        self.exchange = exchange             # the derivatives segment this instance trades (NFO / BFO)
        self.EXCHANGE_NFO = exchange         # order helpers default to kite.EXCHANGE_NFO

    def __getattr__(self, name):
        # Market data and constants come from the real client. Anything order-like that is not implemented
        # below must never reach Zerodha by accident.
        if name in _ORDER_METHODS:
            raise AttributeError(f"PaperKite.{name} is not available in paper mode")
        return getattr(self._kite, name)

    # ── prices ──
    def _ltp_of_symbol(self, symbol: str):
        key = f"{self.exchange}:{symbol}"
        try:
            v = (self._kite.ltp([key]).get(key) or {}).get("last_price")
            return float(v) if v else None
        except Exception as e:
            logger.warning(f"[PAPER] no price for {symbol}: {e}")
            return None

    # ── orders ──
    def place_order(self, variety=None, exchange=None, tradingsymbol=None, transaction_type=None, quantity=None,
                    product=None, order_type=None, price=None, validity=None, trigger_price=None, tag=None, **_):
        order_type = order_type or "LIMIT"
        if order_type == "LIMIT" and price is None:
            raise ValueError("A LIMIT order needs a price.")
        exchange = exchange or self.exchange
        oid = self.ledger.place(exchange, tradingsymbol, transaction_type, quantity, price, order_type, product or "NRML",
                                self._ltp_of_symbol(tradingsymbol))
        o = self.ledger.get(oid)
        logger.info(f"[PAPER] {transaction_type} {tradingsymbol} qty={quantity} price={price} -> {oid} {o['status']}"
                    + (f" @ {o['average_price']}" if o["status"] == "COMPLETE" else ""))
        return oid

    def modify_order(self, variety=None, order_id=None, quantity=None, price=None, order_type=None, trigger_price=None, validity=None, **_):
        sym = (self.ledger.get(order_id) or {}).get("tradingsymbol", "")
        self.ledger.modify(order_id, price=price, quantity=quantity, ltp=self._ltp_of_symbol(sym) if sym else None)
        return order_id

    def cancel_order(self, variety=None, order_id=None, parent_order_id=None):
        self.ledger.cancel(order_id)
        return order_id

    def orders(self) -> list:
        self.ledger.settle(self._ltp_of_symbol)
        return self.ledger.order_book()

    def positions(self) -> dict:
        self.ledger.settle(self._ltp_of_symbol)
        net = self.ledger.positions()
        return {"net": net, "day": net}
