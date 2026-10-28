"""Telegram control channel (background thread with its own DB connection and clients).
Buttons: Approve/Skip (log only) and Buy/Confirm (places an order if trading is enabled). Commands:
/status /report /balance /orders /pnl /pause /resume /halt /unhalt /resolve. Only TELEGRAM_CHAT_ID is obeyed."""
import html
import time
import logging
import threading

from . import config as C, scoring, telegram, trader, util
from .db import DB
from .kalshi import Kalshi

log = logging.getLogger("control")


def scorecard_text(db):
    return scoring.format_scorecard(scoring.scorecard([dict(r) for r in db.settled()], [dict(r) for r in db.settled_alerts()]))


def trading_summary(db):
    mode = C.TRADING_MODE
    if mode == "off":
        return "Trading: OFF (alert-only)"
    now_day = util.day_start_ts()
    opens = db.open_orders(mode)
    return (f"Trading: {mode.upper()} · approval {C.TRADE_APPROVAL} · {'HALTED' if db.get_kv('halt_trading') == '1' else 'enabled'}\n"
            f"Spent today ${db.orders_spend_since(now_day, mode):.2f}/{C.MAX_DAILY_USD:.0f} · realized today ${db.realized_pnl_since(now_day, mode):+.2f} "
            f"· all-time ${db.realized_pnl_total(mode):+.2f} · open positions {len({o['ticker'] for o in opens})}/{C.MAX_OPEN_POSITIONS}")


def status_text(db):
    return (f"Alerts: {'PAUSED' if db.get_kv('paused') == '1' else 'active'}\n"
            f"Alerts today: {db.alerts_since(util.day_start_ts())}" + (f" / cap {C.MAX_ALERTS_PER_DAY}" if C.MAX_ALERTS_PER_DAY else "") + "\n"
            f"Estimates today: {db.get_kv('budget:' + util.today(), '0')} / budget {C.DAILY_ESTIMATE_BUDGET}\n"
            f"Model: {C.MODEL} · web search {'on' if C.USE_WEB_SEARCH else 'off'} · blend {C.MODEL_WEIGHT} · min edge {C.MIN_EDGE * 100:.0f}¢\n"
            + trading_summary(db))


def orders_text(db):
    if C.TRADING_MODE == "off":
        return "Trading is off, so there are no orders."
    rows = db.recent_orders(C.TRADING_MODE)
    if not rows:
        return "No orders yet."
    out = []
    for o in rows:
        pnl = "open" if o["pnl"] is None and o["status"] in ("filled", "partial") else (f"{o['pnl']:+.2f}" if o["pnl"] is not None else "-")
        out.append(f"#{o['id']} {o['status']:<8} {o['side'].upper()} {o['ticker'][-24:]} x{o['contracts']} @ {o['limit_price']:.2f} · {pnl}")
    return "\n".join(out)


def _send(text):
    telegram.send(text)


def _buy_tapped(db, kal, broker, eid, cb, chat, msg_id):
    """First tap on Buy. Live mode asks for a second confirmation; paper/demo go straight through."""
    if broker is None:
        telegram.answer_callback(cb["id"], "Trading is off")
        return
    if C.TRADING_MODE == "live":
        est = db.get_estimate(eid)
        p, why = trader.plan(db, broker, est, kal.refresh(est["ticker"]) if est else None, "manual")
        if p is None:
            telegram.answer_callback(cb["id"], "Not placed")
            _send("🚫 <b>Not placed:</b> " + html.escape(why))
            return
        telegram.answer_callback(cb["id"], "Confirm to place")
        telegram.send(telegram.format_plan(p), [("✔ Confirm BUY", f"y:{eid}"), ("✖ Cancel", f"x:{eid}")])
        return
    _run_order(db, kal, broker, eid, "manual", cb, chat, msg_id)


def _run_order(db, kal, broker, eid, source, cb, chat, msg_id):
    telegram.answer_callback(cb["id"], "Placing order…")
    if msg_id:
        try:
            telegram.clear_buttons(chat, msg_id)
        except Exception:
            pass
    out = trader.execute(db, kal, broker, eid, source)
    _send(telegram.format_order_result(out) if "plan" in out else "🚫 <b>Not placed:</b> " + html.escape(out["reason"]))


def handle_update(db, upd, kal=None, broker=None):
    cb = upd.get("callback_query")
    if cb:
        msg = cb.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        if chat != C.TELEGRAM_CHAT_ID:
            return
        mid = msg.get("message_id")
        parts = (cb.get("data") or "").split(":")
        if len(parts) == 3 and parts[0] == "d" and parts[1] in ("a", "s") and parts[2].isdigit():
            decision = "approved" if parts[1] == "a" else "skipped"
            db.set_decision(int(parts[2]), decision)
            telegram.answer_callback(cb["id"], f"Logged: {decision}")
            if mid:
                telegram.clear_buttons(chat, mid)
        elif len(parts) == 2 and parts[0] == "t" and parts[1].isdigit():
            _buy_tapped(db, kal, broker, int(parts[1]), cb, chat, mid)
        elif len(parts) == 2 and parts[0] == "y" and parts[1].isdigit():
            _run_order(db, kal, broker, int(parts[1]), "manual", cb, chat, mid)
        elif len(parts) == 2 and parts[0] == "x" and parts[1].isdigit():
            telegram.answer_callback(cb["id"], "Cancelled")
            if mid:
                telegram.clear_buttons(chat, mid)
        else:
            telegram.answer_callback(cb["id"], "Unknown action")
        return

    m = upd.get("message") or {}
    if str((m.get("chat") or {}).get("id", "")) != C.TELEGRAM_CHAT_ID:
        return
    words = (m.get("text") or "").strip().lower().split()
    cmd = words[0].split("@")[0] if words else ""
    if cmd == "/pause":
        db.set_kv("paused", "1")
        _send("⏸ New alerts and model calls paused. Settlement tracking continues. /resume to re-enable. (Use /halt to stop order placement.)")
    elif cmd == "/resume":
        db.set_kv("paused", "0")
        _send("▶️ Alerts resumed.")
    elif cmd == "/halt":
        db.set_kv("halt_trading", "1")
        _send("🛑 <b>Trading HALTED.</b> No orders will be placed, including from old buttons. /unhalt to re-enable.")
    elif cmd == "/unhalt":
        unknown = db.c.execute("SELECT COUNT(*) FROM orders WHERE status='unknown'").fetchone()[0]
        if unknown:
            _send(f"Cannot re-enable: {unknown} order(s) have an unknown state. Check Kalshi, then /resolve <id> none|filled.")
        else:
            db.set_kv("halt_trading", "0")
            _send("✅ Trading re-enabled." if C.TRADING_MODE != "off" else "Trading mode is off in the configuration.")
    elif cmd == "/status":
        _send(html.escape(status_text(db)))
    elif cmd == "/report":
        _send(html.escape(scorecard_text(db)))
    elif cmd == "/orders":
        _send("<pre>" + html.escape(orders_text(db)) + "</pre>")
    elif cmd == "/pnl":
        _send(html.escape(trading_summary(db)))
    elif cmd == "/balance":
        try:
            _send(f"{C.TRADING_MODE.upper()} balance: ${broker.balance():,.2f}" if broker else "Trading is off.")
        except Exception as e:
            _send("Could not read balance: " + html.escape(str(e)[:200]))
    elif cmd == "/resolve":
        if len(words) == 3 and words[1].isdigit():
            _send(html.escape(trader.resolve_unknown(db, int(words[1]), words[2])))
        else:
            _send("Usage: /resolve <order id> none|filled")


def _run():
    db = DB(C.DATA_DIR)
    kal = Kalshi()
    try:
        broker = trader.make_broker(db)
    except Exception as e:
        log.error("control thread could not create a broker: %s", e)
        broker = None
    while True:
        try:
            offset = int(db.get_kv("tg_offset", "0") or 0)
            for upd in telegram.get_updates(offset, 30):
                db.set_kv("tg_offset", upd["update_id"] + 1)      # advance first: a crash must never replay a Buy tap
                try:
                    handle_update(db, upd, kal, broker)
                except Exception:
                    log.exception("update handling failed")
        except Exception as e:
            log.warning("Telegram polling error: %s", e)
            time.sleep(15)


def start():
    threading.Thread(target=_run, name="telegram-control", daemon=True).start()
    log.info("Telegram control started")
