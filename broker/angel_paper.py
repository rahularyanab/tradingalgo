"""
Paper trading ledger for the Angel adapter.

The bot runs for real — real Angel market data, real signals, real exit logic — but orders never leave the
machine: they are recorded here and filled against the live price. Positions and the order book are
derived from the ledger, so the bot's own safety checks (reconcile, fill verification, stop logic) behave
exactly as they would with a real broker.

Fills are OPTIMISTIC: a marketable limit order fills at the current LTP with no slippage or queue. Real
results will be a little worse. The ledger is per person, per day (it starts empty each morning — the
bot is intraday only) and survives restarts.
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class PaperLedger:
    def __init__(self, path: Path, today: date | None = None):
        self.path = Path(path)
        self.today = (today or date.today()).isoformat()
        self.orders: list = []
        self.seq = 0
        self._load()

    # ── persistence ──
    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            if d.get("date") == self.today:
                self.orders, self.seq = d["orders"], d["seq"]
        except Exception:
            pass

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"date": self.today, "seq": self.seq, "orders": self.orders}))
        except Exception as e:
            logger.warning(f"Paper ledger not saved: {e}")

    # ── orders ──
    def place(self, exchange, tradingsymbol, transaction_type, quantity, price, order_type, product, ltp) -> str:
        """Record an order; fill it at once if it is marketable at `ltp` (None = no price, stays open)."""
        self.seq += 1
        oid = f"PAPER{datetime.now():%y%m%d}{self.seq:05d}"
        o = {"order_id": oid, "exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
             "quantity": int(quantity), "price": float(price or 0), "order_type": order_type, "product": product,
             "variety": "regular", "status": "OPEN", "filled_quantity": 0, "pending_quantity": int(quantity),
             "average_price": 0.0, "status_message": None, "order_timestamp": datetime.now().isoformat(timespec="seconds")}
        self.orders.append(o)
        self._try_fill(o, ltp)
        self._save()
        return oid

    def _try_fill(self, o: dict, ltp):
        if o["status"] != "OPEN" or ltp is None or ltp <= 0:
            return
        marketable = (o["order_type"] == "MARKET"
                      or (o["transaction_type"] == "BUY" and o["price"] >= ltp)
                      or (o["transaction_type"] == "SELL" and o["price"] <= ltp))
        if marketable:
            o.update(status="COMPLETE", filled_quantity=o["quantity"], pending_quantity=0, average_price=round(float(ltp), 2))

    def settle(self, ltp_for):
        """Give resting orders another chance to fill. `ltp_for(symbol)` -> price or None."""
        changed = False
        for o in self.orders:
            if o["status"] == "OPEN":
                before = o["status"]
                self._try_fill(o, ltp_for(o["tradingsymbol"]))
                changed |= o["status"] != before
        if changed:
            self._save()

    def get(self, order_id: str):
        return next((o for o in self.orders if o["order_id"] == order_id), None)

    def modify(self, order_id: str, price=None, quantity=None, ltp=None):
        o = self.get(order_id)
        if o is None or o["status"] != "OPEN":
            raise ValueError(f"Order {order_id} is not open and cannot be modified.")
        if price is not None:
            o["price"] = float(price)
        if quantity is not None:
            o["quantity"] = int(quantity)
            o["pending_quantity"] = int(quantity) - o["filled_quantity"]
        self._try_fill(o, ltp)
        self._save()

    def cancel(self, order_id: str):
        o = self.get(order_id)
        if o is None or o["status"] != "OPEN":
            raise ValueError(f"Order {order_id} is not open and cannot be cancelled.")
        o["status"] = "CANCELLED"
        self._save()

    def order_book(self) -> list:
        out = []
        for o in self.orders:
            c = dict(o)
            c["order_timestamp"] = datetime.fromisoformat(o["order_timestamp"])
            out.append(c)
        return out

    # ── positions ──
    def positions(self) -> list:
        book: dict = {}
        for o in self.orders:
            if o["status"] != "COMPLETE":
                continue
            p = book.setdefault(o["tradingsymbol"], {"tradingsymbol": o["tradingsymbol"], "exchange": o["exchange"], "product": o["product"],
                                                     "buy_quantity": 0, "sell_quantity": 0, "buy_value": 0.0, "sell_value": 0.0})
            q, v = o["filled_quantity"], o["filled_quantity"] * o["average_price"]
            if o["transaction_type"] == "BUY":
                p["buy_quantity"] += q; p["buy_value"] += v
            else:
                p["sell_quantity"] += q; p["sell_value"] += v
        out = []
        for p in book.values():
            net = p["buy_quantity"] - p["sell_quantity"]
            out.append({
                "tradingsymbol": p["tradingsymbol"], "exchange": p["exchange"], "product": p["product"], "quantity": net,
                "buy_quantity": p["buy_quantity"], "sell_quantity": p["sell_quantity"],
                "buy_price": round(p["buy_value"] / p["buy_quantity"], 2) if p["buy_quantity"] else 0.0,
                "sell_price": round(p["sell_value"] / p["sell_quantity"], 2) if p["sell_quantity"] else 0.0,
                "average_price": round((p["sell_value"] / p["sell_quantity"]) if net < 0 else (p["buy_value"] / p["buy_quantity"]), 2) if net else 0.0,
                "pnl": round(p["sell_value"] - p["buy_value"], 2) if net == 0 else 0.0,
                "last_price": 0.0,
            })
        return out
