import argparse
import html
import json
import logging
import sys
import time
import datetime as dt

from . import config as C, control, estimator, scoring, telegram, trader, util
from .db import DB
from .kalshi import Kalshi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
RANK = {"low": 0, "medium": 1, "high": 2}


def _push(msg, dry, buttons=None):
    if dry:
        print("\n" + msg + ("\n[buttons: " + " | ".join(t for t, _ in buttons) + "]" if buttons else "") + "\n")
    else:
        telegram.send(msg, buttons)


def select(db, cands):
    """Which candidate markets deserve a (paid) model call this cycle."""
    now = time.time()
    ev_counts = {}
    chosen = []
    for m in sorted(cands, key=lambda x: (-x["volume24h"], -x["volume"])):
        if len(chosen) >= C.MAX_ESTIMATES_PER_CYCLE:
            break
        last = db.last_estimate(m["ticker"])
        if last and now - last["ts"] < C.REESTIMATE_HOURS * 3600 and abs(m["mid"] - last["mid"]) < C.REESTIMATE_MOVE:
            continue
        ev = m["event_ticker"]
        if ev not in ev_counts:
            ev_counts[ev] = db.count_event_since(ev, now - 86400)
        if ev_counts[ev] >= C.MAX_PER_EVENT:
            continue
        ev_counts[ev] += 1
        chosen.append(m)
    return chosen


def decide(db, est, edge, side, m):
    if edge < C.MIN_EDGE:
        return "no_edge"
    if RANK[est["confidence"]] < RANK[C.MIN_CONFIDENCE]:
        return "low_confidence"
    if est["resolution_risk"] == "high":
        return "resolution_risk"
    prev = db.last_alert(m["ticker"])
    if prev and time.time() - prev["ts"] < C.COOLDOWN_HOURS * 3600 and prev["side"] == side:
        return "cooldown"
    if C.MAX_ALERTS_PER_DAY and db.alerts_since(util.day_start_ts()) >= C.MAX_ALERTS_PER_DAY:
        return "capped"
    return "alert"


def alert_buttons(eid):
    if C.TRADING_MODE == "off":
        return [("✅ Approve", f"d:a:{eid}"), ("⏭ Skip", f"d:s:{eid}")]
    return [("💰 Buy", f"t:{eid}"), ("⏭ Skip", f"d:s:{eid}")]


def wants_auto(est, edge):
    return (C.TRADING_MODE != "off" and C.TRADE_APPROVAL == "auto" and RANK[est["confidence"]] >= RANK[C.AUTO_MIN_CONFIDENCE]
            and edge >= C.AUTO_MIN_EDGE)


def handle_market(db, kal, broker, m, dry):
    question = kal.question(m)
    est = estimator.estimate(question, m)                 # blind: the model never sees the price
    if est is None:
        return
    fresh = kal.refresh(m["ticker"]) or m                 # the research took a while; use the price as of now
    if not fresh["two_sided"]:
        log.info("%s: book went one-sided during research; skipped", m["ticker"])
        return
    adj = C.MODEL_WEIGHT * est["p"] + (1 - C.MODEL_WEIGHT) * fresh["mid"]
    side, edge, price, fee = scoring.best_edge(adj, fresh)
    status = decide(db, est, edge, side, fresh)
    row = {
        "ts": time.time(), "ticker": m["ticker"], "event_ticker": m["event_ticker"], "title": question,
        "close_time": fresh["close_time"], "close_ts": fresh["close_ts"], "model": C.MODEL,
        "p_model": est["p"], "p_adj": adj, "confidence": est["confidence"], "resolution_risk": est["resolution_risk"],
        "rationale": est["rationale"], "uncertainties": json.dumps(est["uncertainties"]),
        "yes_bid": fresh["yes_bid"], "yes_ask": fresh["yes_ask"], "no_ask": fresh["no_ask"], "mid": fresh["mid"],
        "spread": fresh["spread"], "volume": fresh["volume"], "open_interest": fresh["open_interest"],
        "side": side, "price": price, "fee": fee, "edge": edge, "status": status, "alerted": int(status == "alert"),
        "in_tok": est["in_tok"], "out_tok": est["out_tok"], "searches": est["searches"],
    }
    eid = db.add_estimate(row)
    log.info("%s model=%.0f%% mid=%.0f%% edge=%+.1f¢ conf=%s -> %s", m["ticker"], est["p"] * 100, fresh["mid"] * 100,
             edge * 100, est["confidence"], status)
    if status != "alert":
        return
    auto = broker is not None and not dry and wants_auto(est, edge)
    a = dict(row, uncertainties=est["uncertainties"])
    _push(telegram.format_alert(a, fresh, question) + ("\n🤖 <b>Auto-trade is on: attempting this order now.</b>" if auto else ""),
          dry, None if auto else alert_buttons(eid))
    if auto:
        out = trader.execute(db, kal, broker, eid, "auto")
        _push(telegram.format_order_result(out) if "plan" in out else "🚫 <b>Auto order not placed:</b> " + html.escape(out["reason"]), dry)


def run_cycle(db, kal, broker, dry):
    if db.get_kv("paused") == "1":
        log.info("paused (send /resume in Telegram)")
        return
    cands = kal.candidates()
    for m in select(db, cands):
        if db.bump(f"budget:{util.today()}") > C.DAILY_ESTIMATE_BUDGET:
            log.info("daily estimate budget (%d) reached", C.DAILY_ESTIMATE_BUDGET)
            break
        handle_market(db, kal, broker, m, dry)


def settle_order(db, r, outcome, dry):
    o = db.order_for_estimate(r["id"])
    if not o or o["pnl"] is not None or o["status"] not in ("filled", "partial"):
        return False
    win = (outcome == 1) == (o["side"] == "yes")
    pnl = (o["fill_count"] if win else 0.0) - o["cost_total"] - o["fee_total"]
    db.update_order(o["id"], pnl=round(pnl, 4), settled_ts=time.time())
    _push(telegram.format_order_settlement(dict(db.c.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()),
                                           outcome == 1, r["title"]), dry)
    return True


def settle(db, kal, dry):
    """Record outcomes for markets that have closed; score estimates and settle any orders placed on them."""
    now = time.time()
    rows = db.unsettled(now)
    if not rows:
        return 0
    got = kal.markets_by_tickers(sorted({r["ticker"] for r in rows}))
    n = 0
    for r in rows:
        mk = got.get(r["ticker"])
        if mk is None:
            if now - r["close_ts"] > 30 * 86400:
                db.mark(r["id"], "unknown")                # not retrievable any more; stop checking
            continue
        if mk["status"] not in ("determined", "finalized"):
            continue
        if mk["result"] not in ("yes", "no"):
            o = db.order_for_estimate(r["id"])             # voided / scalar: treat any held position as refunded
            if o and o["pnl"] is None and o["status"] in ("filled", "partial"):
                db.update_order(o["id"], pnl=0.0, settled_ts=now, error=(o["error"] + " | market voided; assumed refunded").strip(" |"))
            db.mark(r["id"], "void")
            continue
        outcome = 1 if mk["result"] == "yes" else 0
        pnl = None
        if r["alerted"]:
            win = (outcome == 1) == (r["side"] == "yes")
            pnl = (1.0 if win else 0.0) - r["price"] - r["fee"]
        db.settle(r["id"], outcome, pnl)
        n += 1
        if settle_order(db, r, outcome, dry):
            continue
        if r["alerted"]:
            _push(telegram.format_settlement(dict(db.get_estimate(r["id"])), outcome == 1), dry)
    return n


def digest(db, dry):
    """Once a day, at DIGEST_HOUR local time: what the bot did, what it cost, how it is scoring."""
    now = util.now_local()
    if now.hour < C.DIGEST_HOUR or db.get_kv("last_digest") == now.date().isoformat():
        return
    db.set_kv("last_digest", now.date().isoformat())
    rows = db.estimates_since(time.time() - 86400)
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    pending = db.c.execute("SELECT COUNT(*) FROM estimates WHERE alerted=1 AND settle_status=''").fetchone()[0]
    msg = (f"🗞 <b>Daily summary</b> ({now.date().isoformat()})\n"
           f"Last 24h: {len(rows)} estimates, {by.get('alert', 0)} alerts. Skips: "
           + (", ".join(f"{k} {v}" for k, v in sorted(by.items()) if k != "alert") or "none") + "\n"
           f"Tokens: {sum(r['in_tok'] for r in rows):,} in / {sum(r['out_tok'] for r in rows):,} out · "
           f"{sum(r['searches'] for r in rows)} searches\n"
           f"Alerts awaiting settlement: {pending}\n"
           + html.escape(control.trading_summary(db)) + "\n\n" + html.escape(control.scorecard_text(db)))
    _push(msg, dry)


def report(db):
    print(control.scorecard_text(db))
    rows = db.c.execute("SELECT status, COUNT(*) n FROM estimates GROUP BY status").fetchall()
    print("\nEstimates by status:", ", ".join(f"{r['status']}={r['n']}" for r in rows) or "none yet")
    orders = db.all_orders()
    if orders:
        print("\nOrders:")
        for mode in sorted({o["mode"] for o in orders}):
            g = [o for o in orders if o["mode"] == mode]
            done = [o for o in g if o["pnl"] is not None]
            print(f"  [{mode}] {len(g)} orders, {len(done)} settled, realized P&L ${sum(o['pnl'] for o in done):+.2f}, "
                  f"wins {sum(1 for o in done if o['pnl'] > 0)}/{len(done)}")


def main():
    ap = argparse.ArgumentParser(description="Claude-Kalshi-Bot: Kalshi probability screener with optional, guarded trading")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Telegram; never trades")
    ap.add_argument("--list", action="store_true", help="print the markets that pass the filters (no model calls) and exit")
    ap.add_argument("--report", action="store_true", help="print the scorecard and exit")
    ap.add_argument("--check-trading", action="store_true", help="validate trading configuration and credentials, print the balance, exit")
    a = ap.parse_args()

    if a.report:
        return report(DB(C.DATA_DIR))
    if a.list:
        for m in sorted(Kalshi().candidates(), key=lambda x: -x["volume"])[:40]:
            print(f"{m['ticker']:40} mid {m['mid']:.2f} spread {m['spread']:.2f} vol {m['volume']:>9,.0f}  {m['yes_sub_title'][:50]}")
        return
    if a.check_trading:
        errs = trader.validate_config()
        print(f"TRADING_MODE={C.TRADING_MODE}  base={C.KALSHI_BASE_URL}")
        for e in errs:
            print("PROBLEM:", e)
        if not errs and C.TRADING_MODE != "off":
            print(f"OK. Balance: ${trader.make_broker(DB(C.DATA_DIR)).balance():,.2f}")
        return
    if not C.ANTHROPIC_API_KEY:
        sys.exit("Set ANTHROPIC_API_KEY (console.anthropic.com).")
    if not a.dry_run and not (C.TELEGRAM_BOT_TOKEN and C.TELEGRAM_CHAT_ID):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or use --dry-run).")
    errs = trader.validate_config()
    if errs:
        sys.exit("Trading configuration problem(s):\n - " + "\n - ".join(errs))

    db, kal = DB(C.DATA_DIR), Kalshi()
    broker = None if a.dry_run else trader.make_broker(db)
    bal = ""
    if broker is not None:
        try:
            bal = f"{broker.balance():,.2f}"
        except Exception as e:
            sys.exit(f"Could not read the {C.TRADING_MODE} account balance (check keys and KALSHI_BASE_URL): {e}")
    live = not a.dry_run and not a.once
    if live and C.TELEGRAM_CONTROL:
        control.start()
    if live:
        if C.TRADING_MODE == "off":
            trading = "alert-only, no orders"
        else:
            trading = (f"<b>TRADING {C.TRADING_MODE.upper()}</b>" + (" — REAL MONEY" if C.TRADING_MODE == "live" else "") +
                       f" · approval {C.TRADE_APPROVAL} · balance ${bal} · per order ≤${C.MAX_ORDER_USD:.0f}, per day ≤${C.MAX_DAILY_USD:.0f}, "
                       f"daily loss stop ${C.MAX_DAILY_LOSS_USD:.0f}. /halt stops all orders.")
        telegram.send(f"🟢 <b>Claude-Kalshi-Bot started</b> — {trading}\nModel {html.escape(C.MODEL)}, "
                      f"web search {'on' if C.USE_WEB_SEARCH else 'off'}, ≤{C.DAILY_ESTIMATE_BUDGET} estimates/day.\n"
                      "Commands: /status /report /orders /pnl /balance /pause /resume /halt /unhalt")
    fails = 0
    while True:
        try:
            run_cycle(db, kal, broker, a.dry_run)
            fails = 0
        except Exception as e:
            fails += 1
            log.exception("cycle failed (%d in a row)", fails)
            if live and fails >= 3 and time.time() - float(db.get_kv("last_error_alert", "0") or 0) > 6 * 3600:
                db.set_kv("last_error_alert", time.time())
                telegram.send(f"⚠️ Claude-Kalshi-Bot: {fails} cycles in a row have failed. Last error: {html.escape(str(e)[:300])}")
        try:
            settle(db, kal, a.dry_run)
            if live:
                digest(db, a.dry_run)
        except Exception:
            log.exception("settlement/digest failed")
        if a.once:
            break
        time.sleep(C.POLL_MINUTES * 60)


if __name__ == "__main__":
    main()
