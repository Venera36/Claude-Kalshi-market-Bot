import html
import time
import requests
from . import config as C, scoring


def _c(x):
    return f"{x * 100:.0f}¢"


def _link(event_ticker):
    series = (event_ticker or "").split("-")[0].lower()
    return f"https://kalshi.com/markets/{series}" if series else "https://kalshi.com"


def format_alert(est, m, question):
    side = est["side"].upper()
    days = (m["close_ts"] - time.time()) / 86400 if m.get("close_ts") else 0
    lines = [f"🎯 <b>KALSHI ALERT — BUY {side}</b> (alert-only, no orders placed)",
             html.escape(question[:300]), "",
             f"<code>{html.escape(m['ticker'])}</code> · closes in {days:.1f} days ({html.escape(m['close_time'][:16])} UTC)",
             f"Pay (ask): <b>{_c(est['price'])}</b> for {side} · est. fee {_c(est['fee'])}",
             f"Model says <b>{est['p_model']:.0%}</b> YES (blended {est['p_adj']:.0%}) · market mid {m['mid']:.0%}",
             f"Edge after fees: <b>{est['edge'] * 100:+.1f}¢</b> per $1 contract",
             f"Confidence: {html.escape(est['confidence'])} · resolution risk: {html.escape(est['resolution_risk'])}",
             "Why: " + html.escape(est["rationale"] or "n/a")]
    if est["uncertainties"]:
        lines.append("Uncertain: " + html.escape("; ".join(est["uncertainties"])))
    lines += [f"Liquidity: vol {m['volume']:,.0f} · OI {m['open_interest']:,.0f} · spread {_c(m['spread'])}",
              f"<a href=\"{_link(m['event_ticker'])}\">Open on Kalshi</a>",
              "<i>Model estimates can be wrong and the market is often right. Not financial advice.</i>"]
    return "\n".join(lines)


def format_settlement(row, outcome_yes):
    won = row["pnl"] is not None and row["pnl"] > 0
    return (f"{'✅' if won else '❌'} <b>Settled {'YES' if outcome_yes else 'NO'}</b> — {html.escape(row['title'][:200])}\n"
            f"Alert was BUY {row['side'].upper()} at {_c(row['price'])} (model said {row['p_model']:.0%} YES, market {row['mid']:.0%}).\n"
            f"Paper result: <b>{row['pnl'] * 100:+.1f}¢</b> per contract after fees. "
            f"You {html.escape(row['decision'] or 'did not respond')}.")


def _api(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/{method}", timeout=kw.pop("timeout", 20), **kw)
    r.raise_for_status()
    return r.json()


def send(text, buttons=None):
    payload = {"chat_id": C.TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
    return _api("sendMessage", json=payload)


def answer_callback(callback_id, text):
    _api("answerCallbackQuery", json={"callback_query_id": callback_id, "text": text})


def clear_buttons(chat_id, message_id):
    _api("editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})


def get_updates(offset, timeout=30):
    r = _api("getUpdates", json={"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
             timeout=timeout + 10)
    return r.get("result", [])


# ------------------------------------------------------------------ trading messages
def _usd(x):
    return f"${x:,.2f}"


def format_plan(p):
    s = p["side"].upper()
    return (f"🧾 <b>Confirm {p['mode'].upper()} order</b>\n"
            f"BUY {s} · <code>{html.escape(p['ticker'])}</code>\n"
            f"{p['contracts']} contracts @ up to {_c(p['price'])} + est. fee {_usd(p['fee'])} = <b>{_usd(p['cost'])}</b> total\n"
            f"If you are wrong you lose that amount. If right you receive {_usd(p['contracts'])}.\n"
            f"Edge now {p['edge'] * 100:+.1f}¢/contract · cash {_usd(p['cash'])}")


def format_order_result(out):
    p, r = out["plan"], out["result"]
    tag = p["mode"].upper() + (" (simulated)" if p["mode"] == "paper" else " (fake money)" if p["mode"] == "demo" else " (REAL MONEY)")
    head = {"filled": "✅ <b>FILLED</b>", "partial": "🟡 <b>PARTIALLY FILLED</b>", "unfilled": "⚪ <b>NOT FILLED</b> (price moved; nothing bought)",
            "rejected": "❌ <b>REJECTED</b>", "unknown": "🚨 <b>ORDER STATE UNKNOWN</b>"}.get(r["status"], r["status"])
    lines = [f"{head} — {tag}", f"BUY {p['side'].upper()} <code>{html.escape(p['ticker'])}</code>"]
    if r["status"] in ("filled", "partial"):
        lines.append(f"{r['fill_count']:g} of {p['contracts']} contracts @ avg {_c(r['avg_cost'])}, fees {_usd(r['fee_total'])}, "
                     f"total <b>{_usd(r['fill_count'] * r['avg_cost'] + r['fee_total'])}</b>. Held to settlement.")
    if r.get("error"):
        lines.append("Detail: " + html.escape(r["error"][:300]))
    if r["status"] == "unknown":
        lines.append("<b>Trading has been HALTED.</b> Check your Kalshi account for this order, then use "
                     f"/resolve {out['order_id']} none (nothing bought) or /resolve {out['order_id']} filled, then /unhalt.")
    return "\n".join(lines)


def format_order_settlement(o, outcome_yes, title):
    won = o["pnl"] > 0
    return (f"{'✅' if won else '❌'} <b>{o['mode'].upper()} position settled {'YES' if outcome_yes else 'NO'}</b> — {html.escape(title[:160])}\n"
            f"You held {o['side'].upper()} ×{o['fill_count']:g} (cost {_usd(o['cost_total'] + o['fee_total'])}). "
            f"Result: <b>{'+' if o['pnl'] >= 0 else '-'}{_usd(abs(o['pnl']))}</b>.")
