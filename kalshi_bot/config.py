ig · PY
import os
 
 
def _f(n, d): return float(os.getenv(n, d))
def _i(n, d): return int(os.getenv(n, d))
def _b(n, d): return os.getenv(n, str(d)).strip().lower() in ("1", "true", "yes")
 
 
def _mode(n, d, allowed):
    v = os.getenv(n, d).strip().lower()
    return v if v in allowed else d
 
 
# ---- secrets / infrastructure ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DATA_DIR = os.getenv("DATA_DIR", "./data")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2").rstrip("/")
BOT_TZ = os.getenv("BOT_TZ", "America/Chicago")
DIGEST_HOUR = _i("DIGEST_HOUR", 8)                 # local hour for the daily summary message
POLL_MINUTES = _i("POLL_MINUTES", 30)
 
# ---- the model that makes the probability estimates ----
PROVIDER = _mode("PROVIDER", "anthropic", ("anthropic", "openai"))   # which API makes the probability estimates
ANTHROPIC_MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6.1-sol")     # e.g. gpt-6-luna is cheaper, gpt-6-astra stronger and costlier
OPENAI_REASONING_EFFORT = _mode("OPENAI_REASONING_EFFORT", "low", ("minimal", "low", "medium", "high", "xhigh"))
OPENAI_SEARCH_CONTEXT = _mode("OPENAI_SEARCH_CONTEXT", "medium", ("low", "medium", "high"))   # OpenAI has no max-searches setting
OPENAI_MAX_OUTPUT_TOKENS = _i("OPENAI_MAX_OUTPUT_TOKENS", 6000)   # includes reasoning tokens
MODEL = OPENAI_MODEL if PROVIDER == "openai" else ANTHROPIC_MODEL   # the name recorded in the decision log
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
USE_WEB_SEARCH = _b("USE_WEB_SEARCH", True)         # needs web search enabled for your org in the Anthropic Console
WEB_SEARCH_TOOL = os.getenv("WEB_SEARCH_TOOL", "web_search_20250305")
MAX_SEARCHES = _i("MAX_SEARCHES", 4)
MAX_TOKENS = _i("MAX_TOKENS", 2000)
 
# ---- which markets are worth researching ----
SERIES = os.getenv("SERIES", "").strip()            # optional: comma-separated series tickers to focus on (e.g. KXCPI,KXFED)
CLOSE_MIN_HOURS = _f("CLOSE_MIN_HOURS", 6)          # skip markets closing sooner than this
CLOSE_MAX_DAYS = _f("CLOSE_MAX_DAYS", 14)           # skip markets that resolve later than this (so results arrive in time to learn from)
MAX_PAGES = _i("MAX_PAGES", 6)                      # pages of 1000 markets to scan per cycle
MIN_VOLUME = _f("MIN_VOLUME", 500)                  # total contracts traded
MIN_TOP_SIZE = _f("MIN_TOP_SIZE", 10)               # contracts available at best bid AND best ask
MAX_SPREAD = _f("MAX_SPREAD", 0.08)                 # ask - bid, in dollars
MIN_MID = _f("MIN_MID", 0.05)                       # skip near-certain markets
MAX_MID = _f("MAX_MID", 0.95)
MAX_PER_EVENT = _i("MAX_PER_EVENT", 2)              # estimates per event per 24h (brackets of one event are correlated)
REESTIMATE_HOURS = _f("REESTIMATE_HOURS", 24)
REESTIMATE_MOVE = _f("REESTIMATE_MOVE", 0.10)       # ...unless the price moved this much
MAX_ESTIMATES_PER_CYCLE = _i("MAX_ESTIMATES_PER_CYCLE", 4)
DAILY_ESTIMATE_BUDGET = _i("DAILY_ESTIMATE_BUDGET", 20)   # hard cap on model calls per day (cost control)
 
# ---- when to alert ----
MODEL_WEIGHT = _f("MODEL_WEIGHT", 0.5)              # blended p = w*model + (1-w)*market mid. 1.0 = trust the model fully
MIN_EDGE = _f("MIN_EDGE", 0.05)                     # expected profit per $1 contract AFTER fees, on the blended p
FEE_RATE = _f("FEE_RATE", 0.07)                     # Kalshi taker fee = rate * P * (1-P) per contract (check their fee schedule)
MIN_CONFIDENCE = _mode("MIN_CONFIDENCE", "medium", ("low", "medium", "high"))
MAX_ALERTS_PER_DAY = _i("MAX_ALERTS_PER_DAY", 5)    # 0 = unlimited
COOLDOWN_HOURS = _f("COOLDOWN_HOURS", 72)           # no repeat alert on the same market and side within this window
TELEGRAM_CONTROL = _b("TELEGRAM_CONTROL", True)
 
# ======================= TRADING (everything below is OFF by default) =======================
# off   = alerts only (default)         paper = simulated fills in the database, no Kalshi account needed
# demo  = real order code against Kalshi's fake-money demo environment (set KALSHI_BASE_URL to the demo API too)
# live  = REAL MONEY (also requires LIVE_TRADING_CONFIRM)
TRADING_MODE = _mode("TRADING_MODE", "off", ("off", "paper", "demo", "live"))
LIVE_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_MONEY"
LIVE_TRADING_CONFIRM = os.getenv("LIVE_TRADING_CONFIRM", "").strip()
TRADE_APPROVAL = _mode("TRADE_APPROVAL", "manual", ("manual", "auto"))   # manual = you press the Buy button
AUTO_MIN_CONFIDENCE = _mode("AUTO_MIN_CONFIDENCE", "high", ("low", "medium", "high"))   # auto mode only trades this confident...
AUTO_MIN_EDGE = _f("AUTO_MIN_EDGE", 0.08)                                              # ...and this large an edge (else asks you)
TRADE_BUTTON_TTL_MIN = _f("TRADE_BUTTON_TTL_MIN", 30)      # a Buy button stops working after this many minutes
 
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "")            # PEM text ("\n" escapes allowed)
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "")    # or the PEM base64-encoded (easiest on Railway)
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
 
# Risk limits (dollars). Positions are held to settlement: the most you can lose on an order is what you paid.
MAX_ORDER_USD = _f("MAX_ORDER_USD", 10)
MAX_DAILY_USD = _f("MAX_DAILY_USD", 25)             # total spend per local day
MAX_DAILY_LOSS_USD = _f("MAX_DAILY_LOSS_USD", 15)   # realized loss per local day that halts trading
MAX_PER_MARKET_USD = _f("MAX_PER_MARKET_USD", 10)
MAX_OPEN_POSITIONS = _i("MAX_OPEN_POSITIONS", 5)
MIN_CASH_RESERVE_USD = _f("MIN_CASH_RESERVE_USD", 5)
KELLY_FRACTION = _f("KELLY_FRACTION", 0.25)         # fraction of the full-Kelly stake
BANKROLL_USD = _f("BANKROLL_USD", 0)                # 0 = use account cash; otherwise size as if this were the bankroll
PAPER_BANKROLL_USD = _f("PAPER_BANKROLL_USD", 100)
MAX_SLIPPAGE = _f("MAX_SLIPPAGE", 0.02)             # refuse if the ask moved up more than this (dollars) since the alert
