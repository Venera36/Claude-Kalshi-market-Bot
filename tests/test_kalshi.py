import os, sys, json, time, base64, tempfile, contextlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kalshi_bot import config as C, kalshi, scoring, estimator, trader, auth, control, telegram, main as M, util
from kalshi_bot.db import DB


@contextlib.contextmanager
def cfg(**kw):
    old = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(C, k, v)


def raw_market(**kw):
    d = {"ticker": "KXTEST-26NOV01-T5", "event_ticker": "KXTEST-26NOV01", "market_type": "binary", "status": "active",
         "yes_sub_title": "Above 5", "no_sub_title": "5 or below", "rules_primary": "rules", "result": "",
         "close_time": "2026-11-03T15:00:00Z", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4400",
         "no_bid_dollars": "0.5600", "no_ask_dollars": "0.6000", "yes_bid_size_fp": "50.00", "yes_ask_size_fp": "40.00",
         "volume_fp": "5000.00", "volume_24h_fp": "300.00", "open_interest_fp": "900.00"}
    d.update(kw)
    return d


def _pem():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    return ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


# ---------------------------------------------------------------- market parsing / filters
def test_parse_and_filters():
    m = kalshi.parse_market(raw_market())
    assert abs(m["mid"] - 0.42) < 1e-9 and abs(m["spread"] - 0.04) < 1e-9 and m["two_sided"]
    lo, hi = m["close_ts"] - 3600, m["close_ts"] + 3600
    assert kalshi.structural_ok(m, lo, hi)
    assert not kalshi.structural_ok(kalshi.parse_market(raw_market(yes_bid_dollars="0.0000")), lo, hi)
    assert not kalshi.structural_ok(kalshi.parse_market(raw_market(yes_ask_dollars="0.6000")), lo, hi)
    assert not kalshi.structural_ok(kalshi.parse_market(raw_market(volume_fp="10.00")), lo, hi)
    assert not kalshi.structural_ok(kalshi.parse_market(raw_market(yes_ask_size_fp="2.00")), lo, hi)
    assert not kalshi.structural_ok(kalshi.parse_market(raw_market(status="closed")), lo, hi)
    assert not kalshi.structural_ok(kalshi.parse_market(raw_market(mve_collection_ticker="X")), lo, hi)
    assert not kalshi.structural_ok(m, lo - 99999, lo - 1)
    legacy = {k: v for k, v in raw_market().items() if not k.endswith("_dollars")}
    legacy.update(yes_bid=40, yes_ask=44)
    assert abs(kalshi.parse_market(legacy)["yes_ask"] - 0.44) < 1e-9
    assert abs(m["no_ask"] - 0.60) < 1e-9


# ---------------------------------------------------------------- scoring
def test_edge_and_scorecard():
    m = kalshi.parse_market(raw_market())
    side, edge, price, fee = scoring.best_edge(0.60, m)
    assert side == "yes" and abs(price - 0.44) < 1e-9 and abs(edge - (0.60 - 0.44 - 0.07 * 0.44 * 0.56)) < 1e-9
    side, edge, price, fee = scoring.best_edge(0.20, m)
    assert side == "no" and abs(price - 0.60) < 1e-9 and edge > 0
    assert scoring.best_edge(0.42, m)[1] < 0
    rows = [dict(ts=1, ticker="A", p_model=0.8, p_adj=0.7, mid=0.5, outcome=1, pnl=None, decision="", price=0, fee=0),
            dict(ts=2, ticker="A", p_model=0.1, p_adj=0.3, mid=0.5, outcome=1, pnl=None, decision="", price=0, fee=0),
            dict(ts=3, ticker="B", p_model=0.2, p_adj=0.3, mid=0.5, outcome=0, pnl=None, decision="", price=0, fee=0)]
    sc = scoring.scorecard(rows, [])
    assert sc["n"] == 2 and sc["brier_model"] < sc["brier_market"] and sc["skill_model"] > 0
    assert "far too small" in scoring.format_scorecard(sc)


def test_estimator_parsing():
    assert estimator.extract_json('thinking... {"a": 1} final {"probability": 0.3, "confidence": "high"}')["probability"] == 0.3
    assert estimator.extract_json("no json") is None
    n = estimator.normalize({"probability": 62, "confidence": "HIGH", "resolution_risk": "weird", "rationale": "x", "key_uncertainties": "one"})
    assert n["p"] == 0.62 and n["confidence"] == "high" and n["resolution_risk"] == "medium" and n["uncertainties"] == ["one"]
    assert estimator.normalize({"probability": -1}) is None and estimator.normalize({"probability": 0.999})["p"] == 0.99


# ---------------------------------------------------------------- auth / signing
def test_signing_rsa_and_ed25519():
    from cryptography.hazmat.primitives.asymmetric import rsa, padding, ed25519
    from cryptography.hazmat.primitives import hashes, serialization
    pem = b""
    for kind in ("rsa", "ed"):
        key = rsa.generate_private_key(65537, 2048) if kind == "rsa" else ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        h = auth.Signer("key-id", pem).headers("post", "/trade-api/v2/portfolio/events/orders?x=1")
        assert h["KALSHI-ACCESS-KEY"] == "key-id" and len(h["KALSHI-ACCESS-TIMESTAMP"]) == 13
        msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "POST/trade-api/v2/portfolio/events/orders").encode()
        sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
        if kind == "rsa":
            key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        else:
            key.public_key().verify(sig, msg)
    assert auth.base_path("https://external-api.demo.kalshi.co/trade-api/v2/") == "/trade-api/v2"
    with cfg(KALSHI_PRIVATE_KEY_B64=base64.b64encode(pem).decode(), KALSHI_PRIVATE_KEY="", KALSHI_PRIVATE_KEY_PATH=""):
        assert auth.load_pem() == pem


def test_private_key_any_paste_format():
    """Railway's one-line field breaks PEM keys (MalformedFraming). Every common way of pasting must work."""
    import textwrap
    from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
    from cryptography.hazmat.primitives import serialization
    for key in (rsa.generate_private_key(65537, 2048), ed25519.Ed25519PrivateKey.generate()):
        for fmt in (serialization.PrivateFormat.PKCS8, serialization.PrivateFormat.TraditionalOpenSSL):
            if isinstance(key, ed25519.Ed25519PrivateKey) and fmt != serialization.PrivateFormat.PKCS8:
                continue
            pem = key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())
            der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            t = pem.decode()
            b64 = base64.b64encode(pem).decode()
            pastes = {
                "KALSHI_PRIVATE_KEY": [t, t.replace("\n", " "), t.replace("\n", "\\n"), '"' + t + '"',
                                       t.replace("\n", "\r\n"), "  " + t.replace("\n", "  ") + "  "],
                "KALSHI_PRIVATE_KEY_B64": [b64, "\n".join(textwrap.wrap(b64, 76)), t, t.replace("\n", " "),
                                           base64.b64encode(der).decode(), b64.rstrip("=")],
            }
            for var, values in pastes.items():
                for v in values:
                    other = "KALSHI_PRIVATE_KEY" if var.endswith("B64") else "KALSHI_PRIVATE_KEY_B64"
                    with cfg(**{var: v, other: "", "KALSHI_PRIVATE_KEY_PATH": "", "KALSHI_API_KEY_ID": " 'kid' "}):
                        sg = auth.load_signer()
                        assert sg.key_id == "kid"
                        assert sg.key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()) == der, (var, v[:40])
    for bad in ("hello world", base64.b64encode(b"not a key").decode()):
        with cfg(KALSHI_PRIVATE_KEY_B64=bad, KALSHI_PRIVATE_KEY="", KALSHI_PRIVATE_KEY_PATH=""):
            try:
                auth.load_signer()
                raise AssertionError("bad key accepted")
            except ValueError as e:
                assert "KALSHI_PRIVATE_KEY_B64" in str(e) and bad[:5] not in str(e)      # never echo the key


def test_estimate_budget_counts_only_real_estimates():
    import tempfile, time as _t
    from kalshi_bot import main as M, util
    from kalshi_bot.db import DB
    db = DB(tempfile.mkdtemp())
    db.set_kv(f"budget:{util.today()}", 47)                     # old inflated counter is ignored
    for _ in range(20):                                           # 20 real estimates already made today
        db.c.execute("INSERT INTO estimates(ts, ticker) VALUES(?, 'X')", (_t.time(),))
    db.c.commit()
    with cfg(DAILY_ESTIMATE_BUDGET=40):
        took = sum(M.budget_take(db) for _ in range(25))
        assert took == 20                                         # 40 - 20 already used
        for _ in range(10):
            assert M.budget_take(db) is False                     # skipped scans don't inflate the count
        assert int(db.get_kv(f"est_budget:{util.today()}")) == 40
    with cfg(DAILY_ESTIMATE_BUDGET=45):
        assert sum(M.budget_take(db) for _ in range(10)) == 5     # raising the budget mid-day now works


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, json.dumps(body)
    def json(self): return self._b
    def raise_for_status(self): assert self.status_code < 400


def _kbroker(handler):
    with cfg(KALSHI_API_KEY_ID="kid", KALSHI_PRIVATE_KEY=_pem(), KALSHI_PRIVATE_KEY_B64="", TRADING_MODE="demo",
             KALSHI_BASE_URL="https://external-api.demo.kalshi.co/trade-api/v2"):
        b = trader.KalshiBroker()
    b.s = type("S", (), {"request": staticmethod(handler)})
    return b


def test_kalshi_broker_orders():
    seen = {}
    def ok(method, url, headers=None, json=None, timeout=None):
        seen.update(method=method, url=url, body=json, headers=headers)
        return FakeResp(201, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00", "average_fill_price": "0.4300", "average_fee_paid": "0.0170"})
    b = _kbroker(ok)
    r = b.place("T", "yes", 10, 0.44, "cid")
    assert seen["url"].endswith("/trade-api/v2/portfolio/events/orders") and seen["method"] == "POST"
    assert seen["body"]["side"] == "bid" and seen["body"]["price"] == "0.4400" and seen["body"]["count"] == "10.00"
    assert seen["body"]["time_in_force"] == "immediate_or_cancel" and seen["body"]["client_order_id"] == "cid"
    assert "KALSHI-ACCESS-SIGNATURE" in seen["headers"]
    assert r["status"] == "filled" and abs(r["avg_cost"] - 0.43) < 1e-9 and abs(r["fee_total"] - 0.17) < 1e-9
    r = b.place("T", "no", 10, 0.60, "cid2")
    assert seen["body"]["side"] == "ask" and seen["body"]["price"] == "0.4000"
    assert abs(r["avg_cost"] - 0.57) < 1e-9
    assert _kbroker(lambda *a, **k: FakeResp(201, {"order_id": "o", "fill_count": "4.00", "average_fill_price": "0.44", "average_fee_paid": "0.02"})).place("T", "yes", 10, 0.44, "c")["status"] == "partial"
    assert _kbroker(lambda *a, **k: FakeResp(201, {"order_id": "o", "fill_count": "0.00"})).place("T", "yes", 10, 0.44, "c")["status"] == "unfilled"
    assert _kbroker(lambda *a, **k: FakeResp(400, {"message": "bad"})).place("T", "yes", 1, 0.4, "c")["status"] == "rejected"
    assert _kbroker(lambda *a, **k: FakeResp(429, {})).place("T", "yes", 1, 0.4, "c")["status"] == "rejected"
    assert _kbroker(lambda *a, **k: FakeResp(500, {})).place("T", "yes", 1, 0.4, "c")["status"] == "unknown"
    assert _kbroker(lambda *a, **k: FakeResp(409, {})).place("T", "yes", 1, 0.4, "c")["status"] == "unknown"
    import requests
    def boom(*a, **k): raise requests.ConnectionError("timeout")
    assert _kbroker(boom).place("T", "yes", 1, 0.4, "c")["status"] == "unknown"
    calls = []
    def count(*a, **k):
        calls.append(1); return FakeResp(500, {})
    _kbroker(count).place("T", "yes", 1, 0.4, "c")
    assert len(calls) == 1
    assert _kbroker(lambda m, u, **k: FakeResp(200, {"balance": 12345, "portfolio_value": 0})).balance() == 123.45
    assert _kbroker(lambda m, u, **k: FakeResp(200, {"balance": 1, "balance_dollars": "99.5000"})).balance() == 99.5


# ---------------------------------------------------------------- config validation
def test_validate_config():
    demo, prod = "https://external-api.demo.kalshi.co/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2"
    with cfg(TRADING_MODE="off"):
        assert trader.validate_config() == []
    with cfg(TRADING_MODE="paper", KALSHI_BASE_URL=prod):
        assert trader.validate_config() == []
    with cfg(TRADING_MODE="demo", KALSHI_BASE_URL=prod):
        assert any("REAL exchange" in e for e in trader.validate_config())
    with cfg(TRADING_MODE="live", KALSHI_BASE_URL=prod, LIVE_TRADING_CONFIRM=""):
        assert any("LIVE_TRADING_CONFIRM" in e for e in trader.validate_config())
    with cfg(TRADING_MODE="live", KALSHI_BASE_URL=demo, LIVE_TRADING_CONFIRM=C.LIVE_PHRASE):
        assert any("demo environment" in e for e in trader.validate_config())
    with cfg(TRADING_MODE="live", KALSHI_BASE_URL=prod, LIVE_TRADING_CONFIRM=C.LIVE_PHRASE, KALSHI_API_KEY_ID="", KALSHI_PRIVATE_KEY="",
             KALSHI_PRIVATE_KEY_B64="", KALSHI_PRIVATE_KEY_PATH=""):
        assert any("credentials" in e for e in trader.validate_config())
    with cfg(TRADING_MODE="paper", MAX_ORDER_USD=0):
        assert any("MAX_ORDER_USD" in e for e in trader.validate_config())


# ---------------------------------------------------------------- sizing + plan + execute
def test_sizing_math():
    assert trader.fee_total(0.5, 100) == 1.75 and trader.fee_total(0.5, 1) == 0.02
    assert trader.kelly_stake(0.5, 0.5, 100) == 0.0 and trader.kelly_stake(0.4, 0.5, 100) == 0.0
    with cfg(KELLY_FRACTION=0.25):
        assert abs(trader.kelly_stake(0.60, 0.50, 100) - 0.25 * 0.2 * 100) < 1e-9
    n = trader.contracts_for(10.0, 0.44)
    assert n * 0.44 + trader.fee_total(0.44, n) <= 10.0 and (n + 1) * 0.44 + trader.fee_total(0.44, n + 1) > 10.0
    assert trader.contracts_for(0.30, 0.44) == 0


class FakeKal:
    def __init__(self, m): self.m = m
    def refresh(self, t): return self.m
    def question(self, m): return "Will it?"


class FakeBroker:
    name = "fake"
    def __init__(self, cash=100.0, result=None):
        self.cash, self.result, self.calls = cash, result, []
    def balance(self): return self.cash
    def place(self, ticker, side, contracts, limit, cid):
        self.calls.append((ticker, side, contracts, limit, cid))
        return self.result or {"status": "filled", "order_id": "x1", "fill_count": float(contracts), "avg_cost": limit,
                               "fee_total": trader.fee_total(limit, contracts), "error": ""}


def _est(db, p_adj=0.65, side="yes", ticker="KXTEST-26NOV01-T5", price=0.44, alerted=1, ts=None):
    m = kalshi.parse_market(raw_market(ticker=ticker))
    row = dict(ts=ts or time.time(), ticker=ticker, event_ticker="KXTEST-26NOV01", title="Q", close_time=m["close_time"], close_ts=m["close_ts"],
               model="m", p_model=0.7, p_adj=p_adj, confidence="high", resolution_risk="low", rationale="r", uncertainties="[]",
               yes_bid=0.40, yes_ask=0.44, no_ask=0.60, mid=0.42, spread=0.04, volume=5000, open_interest=900, side=side, price=price,
               fee=0.017, edge=0.16, status="alert", alerted=alerted, in_tok=1, out_tok=1, searches=0)
    return db.add_estimate(row)


@contextlib.contextmanager
def trading(**kw):
    base = dict(TRADING_MODE="paper", KALSHI_BASE_URL="https://external-api.kalshi.com/trade-api/v2", MAX_ORDER_USD=10, MAX_DAILY_USD=25,
                MAX_PER_MARKET_USD=10, MAX_OPEN_POSITIONS=5, MIN_CASH_RESERVE_USD=5, MAX_DAILY_LOSS_USD=15, MIN_EDGE=0.05,
                MAX_SLIPPAGE=0.02, KELLY_FRACTION=0.25, BANKROLL_USD=0.0, TRADE_BUTTON_TTL_MIN=30)
    base.update(kw)
    with cfg(**base):
        yield


def test_execute_happy_path_and_idempotency():
    db = DB(tempfile.mkdtemp()); eid = _est(db); kal = FakeKal(kalshi.parse_market(raw_market())); br = FakeBroker()
    with trading():
        out = trader.execute(db, kal, br, eid, "manual")
        assert out["ok"] and out["plan"]["contracts"] >= 1 and out["plan"]["cost"] <= 10.0
        assert br.calls[0][1] == "yes" and br.calls[0][3] == 0.44
        o = db.order_for_estimate(eid)
        assert o["status"] == "filled" and o["mode"] == "paper" and o["cost_total"] > 0 and db.get_estimate(eid)["decision"] == "approved"
        again = trader.execute(db, kal, br, eid, "manual")
        assert not again["ok"] and "already exists" in again["reason"] and len(br.calls) == 1


def test_guards():
    kal = FakeKal(kalshi.parse_market(raw_market()))
    def attempt(prep=None, est_kw=None, market=None, cash=100.0, **cfg_kw):
        db = DB(tempfile.mkdtemp()); eid = _est(db, **(est_kw or {})); br = FakeBroker(cash=cash)
        if prep: prep(db)
        with trading(**cfg_kw):
            return trader.execute(db, FakeKal(market) if market else kal, br, eid, "manual"), br, db
    out, br, _ = attempt(TRADING_MODE="off"); assert not out["ok"] and not br.calls and "off" in out["reason"]
    out, br, _ = attempt(prep=lambda d: d.set_kv("halt_trading", "1")); assert "HALTED" in out["reason"] and not br.calls
    out, br, _ = attempt(est_kw=dict(ts=time.time() - 3600)); assert "stale" in out["reason"] and not br.calls
    out, br, _ = attempt(est_kw=dict(alerted=0)); assert "not an alert" in out["reason"]
    out, br, _ = attempt(market=kalshi.parse_market(raw_market(yes_ask_dollars="0.5000", no_bid_dollars="0.5000", no_ask_dollars="0.6000")))
    assert not br.calls and ("slippage" in out["reason"] or "edge" in out["reason"])
    out, br, _ = attempt(est_kw=dict(p_adj=0.43)); assert not br.calls and "edge" in out["reason"]
    out, br, _ = attempt(market=kalshi.parse_market(raw_market(status="closed"))); assert "not actively quoted" in out["reason"]
    out, br, _ = attempt(cash=5.2); assert "cash reserve" in out["reason"] and not br.calls
    out, br, _ = attempt(BANKROLL_USD=1.0); assert "Kelly" in out["reason"] and not br.calls
    out, br, _ = attempt(MAX_DAILY_USD=0.30); assert "daily spend" in out["reason"]
    def lost(db):
        db.reserve_order(dict(estimate_id=999, ts=time.time(), mode="paper", source="m", ticker="Z", side="yes", contracts=1, limit_price=0.5, client_order_id="c", status="pending"))
        oid = db.order_for_estimate(999)["id"]
        db.update_order(oid, status="filled", fill_count=1, avg_cost=0.5, cost_total=0.5, fee_total=0.02, pnl=-16.0, settled_ts=time.time())
    out, br, _ = attempt(prep=lost); assert "daily loss limit" in out["reason"] and not br.calls
    def holds_no(db):
        db.reserve_order(dict(estimate_id=998, ts=time.time(), mode="paper", source="m", ticker="KXTEST-26NOV01-T5", side="no", contracts=3, limit_price=0.6, client_order_id="c9", status="pending"))
    out, br, _ = attempt(prep=holds_no); assert "already hold NO" in out["reason"] and not br.calls
    def full(db):
        for i in range(2):
            db.reserve_order(dict(estimate_id=900 + i, ts=time.time(), mode="paper", source="m", ticker=f"O{i}", side="yes", contracts=1, limit_price=0.5, client_order_id=f"c{i}", status="pending"))
    out, br, _ = attempt(prep=full, MAX_OPEN_POSITIONS=2); assert "open positions" in out["reason"] and not br.calls
    def exposed(db):
        db.reserve_order(dict(estimate_id=997, ts=time.time(), mode="paper", source="m", ticker="KXTEST-26NOV01-T5", side="yes", contracts=20, limit_price=0.5, client_order_id="cx", status="pending"))
    out, br, _ = attempt(prep=exposed); assert "per-market" in out["reason"] and not br.calls
    out, br, db = attempt(); assert out["ok"]


def test_unknown_order_halts_and_resolves():
    db = DB(tempfile.mkdtemp()); eid = _est(db); kal = FakeKal(kalshi.parse_market(raw_market()))
    br = FakeBroker(result={"status": "unknown", "error": "network error"})
    with trading():
        out = trader.execute(db, kal, br, eid, "manual")
        assert not out["ok"] and db.get_kv("halt_trading") == "1" and db.order_for_estimate(eid)["status"] == "unknown"
        assert "ORDER STATE UNKNOWN" in telegram.format_order_result(out) and "/resolve" in telegram.format_order_result(out)
        assert db.orders_spend_since(0, "paper") > 0
        eid2 = _est(db, ticker="OTHER-1")
        assert "HALTED" in trader.execute(db, kal, FakeBroker(), eid2, "manual")["reason"]
        oid = db.order_for_estimate(eid)["id"]
        assert "not placed" in trader.resolve_unknown(db, oid, "none") and db.order_for_estimate(eid)["status"] == "failed"
        assert trader.resolve_unknown(db, oid, "none") == "no such unknown order"


def test_rejected_and_unfilled_do_not_count():
    db = DB(tempfile.mkdtemp()); eid = _est(db); kal = FakeKal(kalshi.parse_market(raw_market()))
    with trading():
        out = trader.execute(db, kal, FakeBroker(result={"status": "unfilled", "fill_count": 0.0, "error": ""}), eid, "manual")
        assert not out["ok"] and db.orders_spend_since(0, "paper") == 0 and db.get_kv("halt_trading") is None
        assert "NOT FILLED" in telegram.format_order_result(out)
        assert not db.open_orders("paper")


def test_paper_broker_and_settlement():
    db = DB(tempfile.mkdtemp()); eid = _est(db); kal = FakeKal(kalshi.parse_market(raw_market()))
    with trading(PAPER_BANKROLL_USD=100):
        pb = trader.PaperBroker(db)
        assert pb.balance() == 100
        out = trader.execute(db, kal, pb, eid, "auto")
        assert out["ok"] and pb.balance() < 100 and db.get_estimate(eid)["decision"] == "auto-bought"
        o = db.order_for_estimate(eid); cost = o["cost_total"] + o["fee_total"]
        sent = []
        saved = telegram.send; telegram.send = lambda t, b=None: sent.append(t)
        try:
            r = dict(db.get_estimate(eid))
            assert M.settle_order(db, r, 1, False)
            o = db.order_for_estimate(eid)
            assert abs(o["pnl"] - (o["fill_count"] - cost)) < 1e-6 and o["pnl"] > 0
            assert "settled YES" in sent[0] and not M.settle_order(db, r, 1, False)
            assert abs(pb.balance() - (100 + o["pnl"])) < 1e-6
            eid2 = _est(db, ticker="LOSER-1"); trader.execute(db, kal, pb, eid2, "manual")
            M.settle_order(db, dict(db.get_estimate(eid2)), 0, False)
            o2 = db.order_for_estimate(eid2)
            assert o2["pnl"] < 0 and abs(o2["pnl"] + o2["cost_total"] + o2["fee_total"]) < 1e-6
        finally:
            telegram.send = saved
        assert "PAPER" in control.trading_summary(db)


def test_no_side_pnl():
    db = DB(tempfile.mkdtemp()); eid = _est(db, p_adj=0.2, side="no", price=0.60); kal = FakeKal(kalshi.parse_market(raw_market()))
    with trading():
        out = trader.execute(db, kal, FakeBroker(), eid, "manual"); assert out["ok"] and out["plan"]["side"] == "no"
        saved = telegram.send; telegram.send = lambda *a, **k: None
        try:
            M.settle_order(db, dict(db.get_estimate(eid)), 0, False)
        finally:
            telegram.send = saved
        assert db.order_for_estimate(eid)["pnl"] > 0


def test_auto_mode_gating_and_buttons():
    est = dict(confidence="high")
    with cfg(TRADING_MODE="paper", TRADE_APPROVAL="auto", AUTO_MIN_CONFIDENCE="high", AUTO_MIN_EDGE=0.08):
        assert M.wants_auto(est, 0.09) and not M.wants_auto(est, 0.06) and not M.wants_auto(dict(confidence="medium"), 0.2)
    with cfg(TRADING_MODE="paper", TRADE_APPROVAL="manual"):
        assert not M.wants_auto(est, 0.5)
    with cfg(TRADING_MODE="off", TRADE_APPROVAL="auto"):
        assert not M.wants_auto(est, 0.5) and M.alert_buttons(7)[0][1] == "d:a:7"
    with cfg(TRADING_MODE="paper"):
        assert M.alert_buttons(7)[0] == ("💰 Buy", "t:7")


# ---------------------------------------------------------------- Telegram control flow
def test_control_buy_flow():
    sent, acks, cleared = [], [], []
    saved = (telegram.send, telegram.answer_callback, telegram.clear_buttons)
    telegram.send = lambda t, b=None: sent.append((t, b)); telegram.answer_callback = lambda i, t: acks.append(t)
    telegram.clear_buttons = lambda c, m: cleared.append(m)
    try:
        db = DB(tempfile.mkdtemp()); eid = _est(db); kal = FakeKal(kalshi.parse_market(raw_market())); br = FakeBroker()
        cb = lambda data, chat="42": {"update_id": 1, "callback_query": {"id": "x", "data": data, "message": {"message_id": 5, "chat": {"id": int(chat)}}}}
        with trading(TELEGRAM_CHAT_ID="42"):
            control.handle_update(db, cb(f"t:{eid}", chat="999"), kal, br)
            assert not br.calls and not acks
            control.handle_update(db, cb(f"t:{eid}"), kal, br)
            assert len(br.calls) == 1 and "FILLED" in sent[-1][0]
            control.handle_update(db, cb(f"t:{eid}"), kal, br)
            assert len(br.calls) == 1 and "already exists" in sent[-1][0]
        db = DB(tempfile.mkdtemp()); eid = _est(db); br = FakeBroker(); sent.clear()
        with trading(TELEGRAM_CHAT_ID="42", TRADING_MODE="live", LIVE_TRADING_CONFIRM=C.LIVE_PHRASE, KALSHI_API_KEY_ID="k", KALSHI_PRIVATE_KEY=_pem()):
            control.handle_update(db, cb(f"t:{eid}"), kal, br)
            assert not br.calls and "Confirm LIVE order" in sent[-1][0] and sent[-1][1][0][1] == f"y:{eid}"
            control.handle_update(db, cb(f"x:{eid}"), kal, br); assert not br.calls and "Cancelled" in acks[-1]
            control.handle_update(db, cb(f"y:{eid}"), kal, br)
            assert len(br.calls) == 1 and db.order_for_estimate(eid)["mode"] == "live"
        db = DB(tempfile.mkdtemp()); eid = _est(db); br = FakeBroker(); sent.clear()
        msg = lambda t, chat=42: {"update_id": 2, "message": {"text": t, "chat": {"id": chat}}}
        with trading(TELEGRAM_CHAT_ID="42"):
            control.handle_update(db, msg("/halt"), kal, br); assert db.get_kv("halt_trading") == "1"
            control.handle_update(db, cb(f"t:{eid}"), kal, br); assert not br.calls and "HALTED" in sent[-1][0]
            control.handle_update(db, msg("/unhalt"), kal, br); assert db.get_kv("halt_trading") == "0"
            control.handle_update(db, msg("/halt", chat=7), kal, br); assert db.get_kv("halt_trading") == "0"
            control.handle_update(db, msg("/balance"), kal, br); assert "$100.00" in sent[-1][0]
            control.handle_update(db, msg("/orders"), kal, br); control.handle_update(db, msg("/pnl"), kal, br); control.handle_update(db, msg("/status"), kal, br)
            db.reserve_order(dict(estimate_id=77, ts=time.time(), mode="paper", source="m", ticker="U", side="yes", contracts=1, limit_price=0.5, client_order_id="u", status="unknown"))
            db.set_kv("halt_trading", "1"); control.handle_update(db, msg("/unhalt"), kal, br)
            assert "unknown state" in sent[-1][0] and db.get_kv("halt_trading") == "1"
            oid = db.order_for_estimate(77)["id"]
            control.handle_update(db, msg(f"/resolve {oid} none"), kal, br); control.handle_update(db, msg("/unhalt"), kal, br)
            assert db.get_kv("halt_trading") == "0"
    finally:
        telegram.send, telegram.answer_callback, telegram.clear_buttons = saved


# ---------------------------------------------------------------- full cycle with everything mocked
def test_full_cycle_alert_then_settle():
    m = kalshi.parse_market(raw_market())
    class K(FakeKal):
        def candidates(self): return [m]
        def markets_by_tickers(self, ts):
            return {t: kalshi.parse_market(raw_market(status="finalized", result="yes")) for t in ts}
    kal = K(m)
    sent = []
    saved = (telegram.send, estimator.estimate)
    telegram.send = lambda t, b=None: sent.append((t, b))
    estimator.estimate = lambda q, mk: dict(p=0.80, confidence="high", resolution_risk="low", rationale="because", uncertainties=["u"], in_tok=10, out_tok=5, searches=1)
    try:
        db = DB(tempfile.mkdtemp())
        with trading(TRADING_MODE="off"), cfg(MIN_EDGE=0.05, MODEL_WEIGHT=1.0, MIN_VOLUME=100, CLOSE_MIN_HOURS=0):
            M.run_cycle(db, kal, None, False)
            assert len(sent) == 1 and "BUY YES" in sent[0][0] and sent[0][1][0][1].startswith("d:a:")
        db2 = DB(tempfile.mkdtemp()); sent.clear()
        with trading(TRADING_MODE="paper", TRADE_APPROVAL="auto", AUTO_MIN_EDGE=0.08), cfg(MODEL_WEIGHT=1.0, MIN_VOLUME=100):
            M.run_cycle(db2, kal, trader.PaperBroker(db2), False)
            assert "Auto-trade is on" in sent[0][0] and "FILLED" in sent[1][0]
            db2.c.execute("UPDATE estimates SET close_ts=1"); db2.c.commit()
            assert M.settle(db2, kal, False) == 1
            assert db2.order_for_estimate(1)["pnl"] > 0 and any("settled YES" in s[0] for s in sent)
    finally:
        telegram.send, estimator.estimate = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
