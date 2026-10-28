# Claude-Kalshi-Bot

Estimates a probability for Kalshi markets with Claude (blind to the market price, with web search), compares it to the market after fees, and sends **Telegram alerts** when the gap is large. It can also **place orders**, but trading is **off by default** and must be turned on in stages. Every estimate is logged and later scored against the outcome (Brier score and calibration), so you can find out whether the model has any edge before risking money.

> Not financial advice. LLM probability estimates may be no better than the market, and fees and spreads can erase small edges. Positions can lose their full cost. Check Kalshi's terms and whether trading is available in your state. Nothing here is guaranteed to profit.

## Rollout (do not skip stages)

| Stage | Settings | What it does |
|---|---|---|
| 1. Alert-only | `TRADING_MODE=off` | Alerts with Approve/Skip buttons. Run for weeks; use `/report` to see Brier/calibration. |
| 2. Paper | `TRADING_MODE=paper` | Buy button simulates fills in the database. No Kalshi account needed. |
| 3. Demo | `TRADING_MODE=demo` + demo `KALSHI_BASE_URL` + demo API keys | Real order code against Kalshi's fake-money environment. Verifies the API integration. |
| 4. Live | `TRADING_MODE=live` + `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_MONEY` | Real money. Buy then Confirm (two taps). Start with tiny `MAX_*` limits. |

`TRADE_APPROVAL=auto` lets the bot trade by itself, but only alerts with `high` confidence and edge ≥ `AUTO_MIN_EDGE`. Leave it on `manual` until you trust the results.

## Safety design
- Limit orders at the ask, immediate-or-cancel, never retried, held to settlement (max loss = cost).
- Caps: per order, per market, per day, daily realized loss, open positions, cash reserve, fractional Kelly.
- One order per estimate (database-enforced), so double taps and replays cannot double-buy.
- If an order's state is unknown (network error, 5xx, 409), trading **halts automatically** until you check Kalshi and run `/resolve <id> none|filled`.
- Demo mode refuses a production URL; live mode refuses a demo URL.
- Only `TELEGRAM_CHAT_ID` is obeyed. Buy buttons expire after `TRADE_BUTTON_TTL_MIN`.

## Telegram commands
`/status` `/report` `/balance` `/orders` `/pnl` `/pause` `/resume` `/halt` `/unhalt` `/resolve <id> none|filled`

## Deploy
1. Push this repo to GitHub.
2. Railway: New Project → Deploy from GitHub repo. Add a **Volume** mounted at `/data`.
3. Set variables from `.env.example` (at minimum `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATA_DIR=/data`).
4. Telegram: create a bot with @BotFather; get your chat id by messaging the bot and opening `https://api.telegram.org/bot<TOKEN>/getUpdates`.
5. Kalshi keys (stages 3-4): create an API key in your Kalshi account settings (demo account for demo), save the private key, then `base64 -w0 kalshi.pem` into `KALSHI_PRIVATE_KEY_B64`.

## Local
```
pip install -r requirements.txt
python -m kalshi_bot.main --list          # show markets that pass filters
python -m kalshi_bot.main --once --dry-run
python -m kalshi_bot.main --check-trading # validate config + read balance
python -m kalshi_bot.main --report
python tests/test_kalshi.py
```

## Known unverified assumptions
Tests use mocks; **nothing has been run against real Kalshi.** Verify in the demo environment before any live use: V2 order endpoint behavior, that `average_fill_price` for NO orders is the YES-leg price (the code converts to your NO cost), and balance field names.
