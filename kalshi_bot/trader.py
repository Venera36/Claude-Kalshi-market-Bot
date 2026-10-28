"""Order execution with hard guard rails. Nothing here runs unless TRADING_MODE is paper, demo or live.

Order shape (Kalshi V2 event-market orders, POST /portfolio/events/orders): everything is quoted on the YES leg.
  buy YES at P  -> side "bid", price P
  buy NO  at Q  -> side "ask", price 1-Q   (selling YES is the same as buying NO)
Orders are limit orders at the ask with immediate_or_cancel, so they can never rest on the book or fill above the limit.
Positions are held to settlement (no selling code), so the most an order can lose is what it cost.
"""
import math
import time
import uuid
import logging
import requests

from . import auth, config as C, scoring, util

log = logging.getLogger("trader")
RANK = {"low": 0, "medium": 1, "high": 2}


# ---------------------------------------------------------------- configuration checks
def validate_config():
    """Problems that must stop startup. Empty list = OK."""
    errs, mode = [], C.TRADING_MODE
    if mode == "off":
        return errs
    demo_url = "demo" in C.KALSHI_BASE_URL.lower()
    if mode == "demo" and not demo_url:
        errs.append("TRADING_MODE=demo but KALSHI_BASE_URL is not a demo address. Refusing: that would send orders to the REAL exchange. "
                    "Use https://external-api.demo.kalshi.co/trade-api/v2 with demo API keys.")
    if mode == "live" and demo_url:
        errs.append("TRADING_MODE=live but KALSHI_BASE_URL points at the demo environment.")
    if mode == "live" and C.LIVE_TRADING_CONFIRM != C.LIVE_PHRASE:
        errs.append(f"Live trading needs LIVE_TRADING_CONFIRM={C.LIVE_PHRASE}")
    if mode in ("demo", "live"):
        try:
            auth.load_signer()
        except Exception as e:
            errs.append(f"Kalshi credentials problem: {e}")
    for name in ("MAX_ORDER_USD", "MAX_DAILY_USD", "MAX_PER_MARKET_USD"):
        if getattr(C, name) <= 0:
            errs.append(f"{name} must be > 0")
    if not 0 < C.KELLY_FRACTION <= 1:
        errs.append("KELLY_FRACTION must be between 0 and 1")
    return errs


# ---------------------------------------------------------------- brokers
def fee_total(price, contracts):
    """Kalshi-style fee for the whole order, rounded UP to the next cent."""
    return math.ceil(C.FEE_RATE * contracts * price * (1 - price) * 100 - 1e-9) / 100


class PaperBroker:
    """Simulated fills at the limit price. No account, no network."""
    name = "paper"

    def __init__(self, db):
        self.db = db

    def balance(self):
        exposure = sum(o["cost_total"] + o["fee_total"] for o in self.db.open_orders("paper"))
        return C.PAPER_BANKROLL_USD + self.db.realized_pnl_total("paper") - exposure

    def place(self, ticker, side, contracts, limit_price, client_order_id):
        return {"status": "filled", "order_id": "paper-" + client_order_id, "fill_count": float(contracts),
                "avg_cost": limit_price, "fee_total": fee_total(limit_price, contracts), "error": ""}


class KalshiBroker:
    """Signed requests to the Kalshi trading API (demo or live, depending on KALSHI_BASE_URL)."""

    def __init__(self):
        self.name = C.TRADING_MODE
        self.signer = auth.load_signer()
        self.base = C.KALSHI_BASE_URL
        self.bp = auth.base_path(self.base)
        self.s = requests.Session()

    def _req(self, method, path, body=None):
        headers = dict(self.signer.headers(method, self.bp + path), Accept="application/json")
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.s.request(method, self.base + path, headers=headers, json=body, timeout=20)

    def balance(self):
        r = self._req("GET", "/portfolio/balance")
        r.raise_for_status()
        d = r.json()
        if d.get("balance_dollars") not in (None, ""):
            return float(d["balance_dollars"])
        return float(d["balance"]) / 100.0               # legacy field: integer cents

    def place(self, ticker, side, contracts, limit_price, client_order_id):
        book, price = ("bid", limit_price) if side == "yes" else ("ask", 1 - limit_price)
        body = {"ticker": ticker, "client_order_id": client_order_id, "side": book, "count": f"{contracts}.00",
                "price": f"{price:.4f}", "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross", "post_only": False, "cancel_order_on_pause": True}
        try:
            r = self._req("POST", "/portfolio/events/orders", body)       # never auto-retried: a retry could double-buy
        except requests.RequestException as e:
            return {"status": "unknown", "error": f"network error, order may or may not have been placed: {e}"}
        if r.status_code in (200, 201):
            try:
                d = r.json()
                fill = float(d.get("fill_count") or 0)
            except (ValueError, TypeError):
                return {"status": "unknown", "error": f"unreadable response: {r.text[:200]}"}
            out = {"order_id": d.get("order_id", ""), "fill_count": fill, "avg_cost": None, "fee_total": 0.0, "error": ""}
            if fill > 0:
                avg = d.get("average_fill_price")
                yes_avg = float(avg) if avg not in (None, "") else price     # YES-leg price
                out["avg_cost"] = yes_avg if side == "yes" else 1 - yes_avg
                out["fee_total"] = round(float(d.get("average_fee_paid") or 0) * fill, 4)
            out["status"] = "unfilled" if fill <= 0 else ("filled" if fill >= contracts - 1e-9 else "partial")
            return out
        if r.status_code == 409 or r.status_code >= 500:
            return {"status": "unknown", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"status": "rejected", "error": f"HTTP {r.status_code}: {r.text[:300]}"}


def make_broker(db):
    mode = C.TRADING_MODE
    if mode == "paper":
        return PaperBroker(db)
    if mode in ("demo", "live"):
        return KalshiBroker()
    return None


# ---------------------------------------------------------------- sizing + guard rails
def kelly_stake(p_win, cost_per, bankroll):
    """Fractional-Kelly stake in dollars for a binary contract costing cost_per (price+fee) that pays $1 on a win."""
    if not 0 < cost_per < 1 or bankroll <= 0:
        return 0.0
    f = (p_win - cost_per) / (1 - cost_per)
    return max(0.0, f) * C.KELLY_FRACTION * bankroll


def contracts_for(stake, price):
    """Largest whole number of contracts whose price + (rounded-up) fee fits inside the stake."""
    n = int(stake / (price + scoring.fee(price)))
    while n > 0 and n * price + fee_total(price, n) > stake + 1e-9:
        n -= 1
    return max(n, 0)


def mode_ok(db):
    """(ok, reason): is any trading allowed right now?"""
    if C.TRADING_MODE == "off":
        return False, "trading is off (TRADING_MODE=off)"
    if validate_config():
        return False, "trading configuration is invalid"
    if db.get_kv("halt_trading") == "1":
        return False, "trading is HALTED (send /unhalt to re-enable)"
    return True, ""


def plan(db, broker, est, fresh, source):
    """Decide whether and how much to buy, with no side effects. Returns (plan_dict, "") or (None, reason)."""
    ok, why = mode_ok(db)
    if not ok:
        return None, why
    if est["alerted"] != 1:
        return None, "this estimate was not an alert"
    if db.order_for_estimate(est["id"]):
        return None, "an order for this alert already exists"
    if source == "manual" and time.time() - est["ts"] > C.TRADE_BUTTON_TTL_MIN * 60:
        return None, f"alert is older than {C.TRADE_BUTTON_TTL_MIN:.0f} minutes; prices are stale"
    if not fresh or not fresh["two_sided"] or fresh["status"] != "active":
        return None, "market is not actively quoted right now"

    mode, now_day = C.TRADING_MODE, util.day_start_ts()
    side = est["side"]
    best_side, edge, price, fee_unit = scoring.best_edge(est["p_adj"], fresh)
    if best_side != side:
        return None, "the edge flipped to the other side since the alert"
    if edge < C.MIN_EDGE:
        return None, f"edge is now {edge * 100:+.1f}¢, below the {C.MIN_EDGE * 100:.0f}¢ minimum"
    if price - est["price"] > C.MAX_SLIPPAGE + 1e-9:
        return None, f"price moved {est['price'] * 100:.0f}¢ → {price * 100:.0f}¢ (max slippage {C.MAX_SLIPPAGE * 100:.0f}¢)"

    if db.realized_pnl_since(now_day, mode) <= -C.MAX_DAILY_LOSS_USD:
        return None, f"daily loss limit (${C.MAX_DAILY_LOSS_USD:.0f}) reached"
    held = db.open_side(est["ticker"], mode)
    if held and held != side:
        return None, f"you already hold {held.upper()} on this market"
    open_tickers = {o["ticker"] for o in db.open_orders(mode)}
    if est["ticker"] not in open_tickers and len(open_tickers) >= C.MAX_OPEN_POSITIONS:
        return None, f"already at {C.MAX_OPEN_POSITIONS} open positions"

    cash = broker.balance()
    bankroll = C.BANKROLL_USD or cash
    p_win = est["p_adj"] if side == "yes" else 1 - est["p_adj"]
    caps = [kelly_stake(p_win, price + fee_unit, bankroll), C.MAX_ORDER_USD,
            C.MAX_PER_MARKET_USD - db.ticker_exposure(est["ticker"], mode),
            C.MAX_DAILY_USD - db.orders_spend_since(now_day, mode),
            cash - C.MIN_CASH_RESERVE_USD]
    stake = min(caps)
    n = contracts_for(stake, price)
    if n < 1:
        names = ["Kelly size", "per-order cap", "per-market cap", "daily spend cap", "cash reserve"]
        return None, f"stake too small: limited by {names[caps.index(stake)]} (${max(stake, 0):.2f})"
    fee = fee_total(price, n)
    return ({"eid": est["id"], "ticker": est["ticker"], "side": side, "contracts": n, "price": price, "fee": fee,
             "cost": n * price + fee, "p_win": p_win, "edge": edge, "cash": cash, "mode": mode}, "")


def execute(db, kal, broker, eid, source):
    """Run every check, then place the order. Returns {'ok','reason','plan','result','order_id'}."""
    est = db.get_estimate(eid)
    if est is None or broker is None:
        return {"ok": False, "reason": "unknown alert or trading is off"}
    fresh = kal.refresh(est["ticker"])
    p, why = plan(db, broker, est, fresh, source)
    if p is None:
        return {"ok": False, "reason": why}
    cid = f"ckb-{eid}-{uuid.uuid4().hex[:10]}"
    oid = db.reserve_order({"estimate_id": eid, "ts": time.time(), "mode": p["mode"], "source": source, "ticker": p["ticker"],
                            "side": p["side"], "contracts": p["contracts"], "limit_price": p["price"],
                            "client_order_id": cid, "status": "pending"})
    if oid is None:
        return {"ok": False, "reason": "an order for this alert already exists"}
    log.warning("PLACING %s order: BUY %s %s x%d @ %.2f (%s)", p["mode"].upper(), p["side"].upper(), p["ticker"],
                p["contracts"], p["price"], source)
    try:
        res = broker.place(p["ticker"], p["side"], p["contracts"], p["price"], cid)
    except Exception as e:                      # anything unexpected after the reservation: treat as unknown, stop trading
        res = {"status": "unknown", "error": f"unexpected error: {e}"}
    filled = res.get("fill_count") or 0.0
    avg = res.get("avg_cost")
    db.update_order(oid, order_id=res.get("order_id", ""), status=res["status"], error=res.get("error", ""),
                    fill_count=filled, avg_cost=avg, fee_total=res.get("fee_total", 0.0),
                    cost_total=round(filled * avg, 4) if avg is not None else 0.0)
    if res["status"] == "unknown":
        db.set_kv("halt_trading", "1")          # never keep trading while an order's state is unclear
    db.set_decision(eid, "auto-bought" if source == "auto" else "approved")
    return {"ok": res["status"] in ("filled", "partial"), "reason": res.get("error", ""), "plan": p, "result": res, "order_id": oid}


def resolve_unknown(db, oid, outcome):
    """Manual fix after you checked Kalshi yourself: outcome 'none' (nothing was bought) or 'filled' (bought in full at the limit)."""
    o = db.c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "unknown":
        return "no such unknown order"
    if outcome == "none":
        db.update_order(oid, status="failed", error=o["error"] + " | resolved by user: nothing bought")
        return f"order {oid} marked as not placed"
    if outcome == "filled":
        db.update_order(oid, status="filled", fill_count=float(o["contracts"]), avg_cost=o["limit_price"],
                        cost_total=o["contracts"] * o["limit_price"], fee_total=fee_total(o["limit_price"], o["contracts"]),
                        error=o["error"] + " | resolved by user: assumed filled at the limit price")
        return f"order {oid} marked as filled at the limit price (approximate P&L)"
    return "use: /resolve <id> none|filled"
