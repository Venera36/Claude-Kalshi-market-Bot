"""Read-only Kalshi market-data client. GET requests to public endpoints only: no API key, no orders, no account access."""
import time
import logging
import datetime as dt
import requests

from . import config as C

log = logging.getLogger("kalshi")


class KalshiError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _price(raw, key):
    """Dollar price from the *_dollars field; falls back to the legacy integer-cents field."""
    v = _num(raw.get(key + "_dollars"))
    if v is None:
        c = _num(raw.get(key))
        v = c / 100 if c is not None else None
    return v


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def parse_market(raw):
    yb, ya = _price(raw, "yes_bid"), _price(raw, "yes_ask")
    nb, na = _price(raw, "no_bid"), _price(raw, "no_ask")
    two_sided = bool(yb and yb > 0 and ya and 0 < ya < 1 and ya >= yb)
    if two_sided:                      # YES and NO books mirror each other; derive if a side is missing
        na = na if na and 0 < na < 1 else 1 - yb
        nb = nb if nb and nb > 0 else 1 - ya
    return {
        "ticker": raw.get("ticker", ""), "event_ticker": raw.get("event_ticker", ""),
        "market_type": raw.get("market_type", ""), "status": raw.get("status", ""), "result": raw.get("result", "") or "",
        "yes_sub_title": raw.get("yes_sub_title", "") or "", "no_sub_title": raw.get("no_sub_title", "") or "",
        "rules_primary": raw.get("rules_primary", "") or "", "rules_secondary": raw.get("rules_secondary", "") or "",
        "close_time": raw.get("close_time", ""), "close_ts": _ts(raw.get("close_time", "")),
        "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na, "last": _price(raw, "last_price"),
        "bid_size": _num(raw.get("yes_bid_size_fp")), "ask_size": _num(raw.get("yes_ask_size_fp")),
        "volume": _num(raw.get("volume_fp")) or 0.0, "volume24h": _num(raw.get("volume_24h_fp")) or 0.0,
        "open_interest": _num(raw.get("open_interest_fp")) or 0.0,
        "two_sided": two_sided,
        "mid": (yb + ya) / 2 if two_sided else None, "spread": (ya - yb) if two_sided else None,
        "is_mve": bool(raw.get("mve_collection_ticker")) or str(raw.get("ticker", "")).startswith("KXMVE"),
        "title": ((raw.get("title") or "") if isinstance(raw.get("title"), str) else ""),
    }


def excluded(m, prefixes=None):
    """True if the market's ticker or event ticker starts with any EXCLUDE_SERIES prefix (case-insensitive)."""
    raw = C.EXCLUDE_SERIES if prefixes is None else prefixes
    pre = tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return bool(pre) and (m["ticker"].upper().startswith(pre) or m["event_ticker"].upper().startswith(pre))


def structural_ok(m, lo_ts, hi_ts):
    """Cheap filters applied before any model call."""
    if m["market_type"] != "binary" or m["status"] != "active" or m["is_mve"] or not m["two_sided"]:
        return False
    if not m["close_ts"] or not (lo_ts <= m["close_ts"] <= hi_ts):
        return False
    if not (C.MIN_MID <= m["mid"] <= C.MAX_MID) or m["spread"] > C.MAX_SPREAD:
        return False
    if m["volume"] < C.MIN_VOLUME:
        return False
    sizes = [s for s in (m["bid_size"], m["ask_size"]) if s is not None]
    return not sizes or min(sizes) >= C.MIN_TOP_SIZE


class Kalshi:
    def __init__(self, base_url=None):
        self.base = (base_url or C.KALSHI_BASE_URL).rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Claude-Kalshi-Bot/1.0 (read-only market data)", "Accept": "application/json"})
        self._events = {}
        self.truncated = False

    def _get(self, path, params=None):
        for attempt in range(4):
            time.sleep(0.25)
            try:
                r = self.s.get(self.base + path, params=params, timeout=30)
            except requests.RequestException as e:
                log.warning("request error %s: %s", path, e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = int(r.headers.get("Retry-After", 0))
                except ValueError:
                    wait = 0
                log.warning("HTTP %s for %s; backing off", r.status_code, path)
                time.sleep(min(max(wait, 5 * (attempt + 1)), 60))
                continue
            raise KalshiError(r.status_code, r.text[:200])
        raise KalshiError(0, f"{path} unreachable after retries")

    def _pages(self, params, drop=()):
        """All pages of GET /markets (bounded by MAX_PAGES). If Kalshi rejects an optional filter, retry without it."""
        out, cursor, params = [], None, dict(params)
        for page in range(C.MAX_PAGES):
            q = dict(params, **({"cursor": cursor} if cursor else {}))
            try:
                data = self._get("/markets", q)
            except KalshiError as e:
                if e.status == 400 and page == 0 and any(k in params for k in drop):
                    for k in drop:
                        params.pop(k, None)
                    log.warning("Kalshi rejected %s (%s); retrying without it", drop, e)
                    data = self._get("/markets", dict(params))
                else:
                    raise
            out += data.get("markets", [])
            cursor = data.get("cursor")
            if not cursor:
                break
        else:
            if cursor:   # hit the page cap with more pages left: the scan is incomplete, and silently so unless we say it
                self.truncated = True
                log.warning("market scan truncated at MAX_PAGES=%d (%d markets); more remain. Raise MAX_PAGES "
                            "or narrow CLOSE_MAX_DAYS / SERIES", C.MAX_PAGES, len(out))
        return out

    def candidates(self):
        now = int(time.time())
        lo, hi = now + int(C.CLOSE_MIN_HOURS * 3600), now + int(C.CLOSE_MAX_DAYS * 86400)
        raws, self.truncated = [], False
        if C.SERIES:
            for s in (x.strip() for x in C.SERIES.split(",") if x.strip()):
                raws += self._pages({"series_ticker": s, "status": "open", "limit": 1000})
        else:   # close-time filters can't be combined with status=open, so status is filtered locally
            raws = self._pages({"min_close_ts": lo, "max_close_ts": hi, "mve_filter": "exclude", "limit": 1000},
                               drop=("mve_filter",))
        parsed = [parse_market(r) for r in raws]
        out = [m for m in parsed if structural_ok(m, lo, hi)]
        n_ok = len(out)
        out = [m for m in out if not excluded(m)]
        log.info("scanned %d markets%s, %d pass the structural filters%s", len(parsed),
                 " (TRUNCATED)" if self.truncated else "", len(out),
                 f" ({n_ok - len(out)} more excluded by EXCLUDE_SERIES)" if n_ok != len(out) else "")
        return out

    def markets_by_tickers(self, tickers):
        """{ticker: parsed market} for the given tickers (used to refresh prices and to read settlement results)."""
        out = {}
        tickers = list(tickers)
        for i in range(0, len(tickers), 50):
            data = self._get("/markets", {"tickers": ",".join(tickers[i:i + 50]), "limit": 100})
            for r in data.get("markets", []):
                m = parse_market(r)
                out[m["ticker"]] = m
        return out

    def refresh(self, ticker):
        try:
            return self.markets_by_tickers([ticker]).get(ticker)
        except KalshiError as e:
            log.warning("refresh failed for %s: %s", ticker, e)
            return None

    def question(self, m):
        """Human-readable question: event title + the YES side. (Market 'title' is deprecated by Kalshi.)"""
        ev = m["event_ticker"]
        if ev not in self._events:
            try:
                data = self._get(f"/events/{ev}")
                self._events[ev] = (data.get("event") or {}).get("title", "") or ""
            except KalshiError as e:
                log.warning("event lookup failed for %s: %s", ev, e)
                self._events[ev] = ""
        base = self._events[ev] or m["title"] or m["ticker"]
        return f"{base} — YES means: {m['yes_sub_title']}" if m["yes_sub_title"] else base
