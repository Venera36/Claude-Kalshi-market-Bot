import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
-- Every model estimate ever made, whether or not it produced an alert. This is the decision log.
CREATE TABLE IF NOT EXISTS estimates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, event_ticker TEXT, title TEXT, close_time TEXT, close_ts REAL,
  model TEXT, p_model REAL, p_adj REAL, confidence TEXT, resolution_risk TEXT, rationale TEXT, uncertainties TEXT,
  yes_bid REAL, yes_ask REAL, no_ask REAL, mid REAL, spread REAL, volume REAL, open_interest REAL,
  side TEXT, price REAL, fee REAL, edge REAL, status TEXT, alerted INTEGER DEFAULT 0, decision TEXT DEFAULT '',
  in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0, searches INTEGER DEFAULT 0,
  settle_status TEXT DEFAULT '', outcome INTEGER, settled_ts REAL, pnl REAL);
CREATE INDEX IF NOT EXISTS ix_est_ticker ON estimates(ticker, ts);
CREATE INDEX IF NOT EXISTS ix_est_ts ON estimates(ts);
-- Every order the bot has attempted (paper, demo or live). One order per estimate, enforced by UNIQUE.
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id INTEGER UNIQUE, ts REAL, mode TEXT, source TEXT,
  ticker TEXT, side TEXT, contracts INTEGER, limit_price REAL, client_order_id TEXT, order_id TEXT DEFAULT '',
  status TEXT, error TEXT DEFAULT '', fill_count REAL DEFAULT 0, avg_cost REAL, fee_total REAL DEFAULT 0,
  cost_total REAL DEFAULT 0, settled_ts REAL, pnl REAL);
"""
ORDER_COLS = {"order_id", "status", "error", "fill_count", "avg_cost", "fee_total", "cost_total", "settled_ts", "pnl"}
ACTIVE = "('pending','filled','partial','unknown')"
EST_COLS = ("ts ticker event_ticker title close_time close_ts model p_model p_adj confidence resolution_risk rationale "
            "uncertainties yes_bid yes_ask no_ask mid spread volume open_interest side price fee edge status alerted "
            "in_tok out_tok searches").split()


class DB:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.c = sqlite3.connect(os.path.join(data_dir, "kalshi_bot.db"), timeout=30)
        self.c.row_factory = sqlite3.Row
        try:
            self.c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.c.executescript(SCHEMA)

    # ---- key/value ----
    def get_kv(self, k, default=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_kv(self, k, v):
        with self.c:
            self.c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def bump(self, k):
        n = int(self.get_kv(k, "0") or 0) + 1
        self.set_kv(k, n)
        return n

    # ---- estimates ----
    def add_estimate(self, row):
        with self.c:
            cur = self.c.execute(f"INSERT INTO estimates({','.join(EST_COLS)}) VALUES({','.join('?' * len(EST_COLS))})",
                                 [row[k] for k in EST_COLS])
            return cur.lastrowid

    def count_estimates_since(self, ts):
        return self.c.execute("SELECT COUNT(*) FROM estimates WHERE ts>=?", (ts,)).fetchone()[0]

    def last_estimate(self, ticker):
        return self.c.execute("SELECT * FROM estimates WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()

    def count_event_since(self, event_ticker, ts):
        return self.c.execute("SELECT COUNT(*) FROM estimates WHERE event_ticker=? AND ts>=?", (event_ticker, ts)).fetchone()[0]

    def sibling_estimates(self, event_ticker, ticker, since_ts):
        """Most recent estimate for each OTHER market of the same event since since_ts."""
        return self.c.execute(
            "SELECT e.* FROM estimates e JOIN (SELECT ticker, MAX(ts) mts FROM estimates WHERE event_ticker=? AND ticker!=? AND ts>=? "
            "GROUP BY ticker) x ON e.ticker=x.ticker AND e.ts=x.mts", (event_ticker, ticker, since_ts)).fetchall()

    def last_alert(self, ticker):
        return self.c.execute("SELECT * FROM estimates WHERE ticker=? AND alerted=1 ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()

    def alerts_since(self, ts):
        return self.c.execute("SELECT COUNT(*) FROM estimates WHERE alerted=1 AND ts>=?", (ts,)).fetchone()[0]

    def estimates_since(self, ts):
        return self.c.execute("SELECT * FROM estimates WHERE ts>=? ORDER BY ts", (ts,)).fetchall()

    def get_estimate(self, eid):
        return self.c.execute("SELECT * FROM estimates WHERE id=?", (eid,)).fetchone()

    def set_decision(self, eid, decision):
        with self.c:
            self.c.execute("UPDATE estimates SET decision=? WHERE id=?", (decision, eid))

    # ---- settlement ----
    def unsettled(self, now_ts):
        return self.c.execute("SELECT * FROM estimates WHERE outcome IS NULL AND settle_status='' AND close_ts<?", (now_ts,)).fetchall()

    def settle(self, eid, outcome, pnl):
        with self.c:
            self.c.execute("UPDATE estimates SET outcome=?, settle_status='settled', settled_ts=?, pnl=? WHERE id=?",
                           (outcome, time.time(), pnl, eid))

    def mark(self, eid, status):
        with self.c:
            self.c.execute("UPDATE estimates SET settle_status=?, settled_ts=? WHERE id=?", (status, time.time(), eid))

    def settled(self):
        return self.c.execute("SELECT * FROM estimates WHERE settle_status='settled'").fetchall()

    def settled_alerts(self):
        return self.c.execute("SELECT * FROM estimates WHERE settle_status='settled' AND alerted=1").fetchall()

    # ---- orders ----
    def reserve_order(self, row):
        """Insert a 'pending' order BEFORE talking to the broker. Returns its id, or None if this estimate already has one."""
        cols = "estimate_id ts mode source ticker side contracts limit_price client_order_id status".split()
        try:
            with self.c:
                cur = self.c.execute(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                     [row[k] for k in cols])
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def update_order(self, oid, **fields):
        bad = set(fields) - ORDER_COLS
        if bad:
            raise ValueError(f"unknown columns {bad}")
        with self.c:
            self.c.execute(f"UPDATE orders SET {','.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), oid))

    def order_for_estimate(self, eid):
        return self.c.execute("SELECT * FROM orders WHERE estimate_id=?", (eid,)).fetchone()

    def orders_spend_since(self, ts, mode):
        """Dollars committed today: filled cost + fees, plus the full planned cost of pending/unknown orders (conservative)."""
        r = self.c.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('filled','partial') THEN cost_total+fee_total "
            "ELSE contracts*limit_price END),0) FROM orders WHERE ts>=? AND mode=? AND status IN ('pending','filled','partial','unknown')",
            (ts, mode)).fetchone()
        return r[0]

    def open_orders(self, mode):
        return self.c.execute(f"SELECT * FROM orders WHERE mode=? AND pnl IS NULL AND status IN {ACTIVE}", (mode,)).fetchall()

    def ticker_exposure(self, ticker, mode):
        return sum((o["cost_total"] + o["fee_total"]) if o["status"] in ("filled", "partial") else o["contracts"] * o["limit_price"]
                   for o in self.open_orders(mode) if o["ticker"] == ticker)

    def open_side(self, ticker, mode):
        sides = {o["side"] for o in self.open_orders(mode) if o["ticker"] == ticker}
        return next(iter(sides)) if sides else None

    def realized_pnl_since(self, ts, mode):
        return self.c.execute("SELECT COALESCE(SUM(pnl),0) FROM orders WHERE mode=? AND settled_ts>=?", (mode, ts)).fetchone()[0]

    def realized_pnl_total(self, mode):
        return self.c.execute("SELECT COALESCE(SUM(pnl),0) FROM orders WHERE mode=?", (mode,)).fetchone()[0]

    def recent_orders(self, mode, n=8):
        return self.c.execute("SELECT * FROM orders WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, n)).fetchall()

    def all_orders(self):
        return self.c.execute("SELECT * FROM orders ORDER BY ts").fetchall()
