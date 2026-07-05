# TradingAlgo

Automated intraday options-selling bot for NIFTY, built on Zerodha KiteConnect. Combines trendline support/resistance, RSI divergence, and option-chain PCR into a scored signal, then sells (or strangles) NIFTY options with defined risk controls. Includes a backtester and a paper-trading mode alongside live execution.

## How it works

Three signals are scored each scan (2 of 3 required to act):

1. **Trendline** — price at a clustered support/resistance level
2. **RSI divergence** — bearish (higher high + lower RSI) or bullish (lower low + higher RSI)
3. **Option chain PCR** — put/call ratio below 0.8 (bearish) or above 1.2 (bullish)

When PCR and RSI are both neutral and price is range-bound between visible trendlines, the bot instead looks for a **strangle** setup.

Risk controls include a stop-loss buffer beyond the trendline, a premium-decay profit target, strike rolling on OI wall shifts, a daily max-loss circuit breaker, and forced square-off before market close.

## Project structure

```
main.py               live trading entry point
run_paper_trade.py    paper trading entry point
run_backtest.py       backtest entry point
config.py             strategy, risk, and instrument parameters

auth/                 Zerodha login + access token handling
data/                 market data, option chain, S/R level storage
signals/              signal combination, position/trade monitoring
strategy/             trendline, RSI divergence, option signal logic
execution/            order placement
backtest/             backtest engine, option pricer, reporting
paper_trade/          paper trading simulator
notifications/        Telegram alerts and remote commands
scripts/              automated login (TOTP-based)
systemd/              service/timer units for server deployment
```

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your Zerodha (KiteConnect) and Telegram credentials.
3. Run once each morning to refresh the Zerodha access token (or automate via `scripts/auto_login.py`):
   ```bash
   python auth/kite_login.py
   ```

## Running

```bash
python run_backtest.py       # backtest against historical data
python run_paper_trade.py     # paper trade against live market data
python main.py                # live trading (real orders)
```

## Telegram bot

When running, the bot sends trade alerts and supports basic commands (`/status`, `/logs`, `/errors`, `/help`) via a command listener thread.

## Disclaimer

This is a personal trading tool provided as-is, with no warranty. Options trading carries substantial risk of loss. Nothing here is financial advice — use at your own risk.
