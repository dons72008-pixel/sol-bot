"""SOL EMA-crossover bot. Modes: backtest, paper. No real money, no wallet."""
import json
import os
import sys
import urllib.request

FAST, SLOW = 9, 21
FEE = 0.0026  # 0.26% per trade, roughly an exchange fee
START_CASH = 1000.0
STATE_FILE = "paper_state.json"


def fetch_closes(interval_minutes):
    url = f"https://api.kraken.com/0/public/OHLC?pair=SOLUSD&interval={interval_minutes}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    if data.get("error"):
        raise RuntimeError(data["error"])
    rows = next(v for k, v in data["result"].items() if k != "last")
    rows = rows[:-1]  # drop the still-forming candle
    return [float(row[4]) for row in rows]


def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def backtest():
    closes = fetch_closes(60)  # hourly, about 30 days
    fast, slow = ema(closes, FAST), ema(closes, SLOW)
    cash, sol, trades = START_CASH, 0.0, 0
    for i in range(SLOW, len(closes)):
        price = closes[i]
        if fast[i] > slow[i] and sol == 0:
            sol = cash * (1 - FEE) / price
            cash = 0.0
            trades += 1
        elif fast[i] < slow[i] and sol > 0:
            cash = sol * price * (1 - FEE)
            sol = 0.0
            trades += 1
    final = cash + sol * closes[-1]
    hold = START_CASH / closes[SLOW] * closes[-1]
    print(f"Candles: {len(closes)} (hourly)")
    print(f"Trades: {trades}")
    print(f"Bot final value: ${final:.2f} ({(final / START_CASH - 1) * 100:+.1f}%)")
    print(f"Buy & hold value: ${hold:.2f} ({(hold / START_CASH - 1) * 100:+.1f}%)")


def paper():
    closes = fetch_closes(15)
    fast, slow = ema(closes, FAST), ema(closes, SLOW)
    price = closes[-1]
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {"cash": START_CASH, "sol": 0.0, "trades": 0}

    action = "HOLD"
    if fast[-1] > slow[-1] and state["sol"] == 0:
        state["sol"] = state["cash"] * (1 - FEE) / price
        state["cash"] = 0.0
        state["trades"] += 1
        action = "BUY"
    elif fast[-1] < slow[-1] and state["sol"] > 0:
        state["cash"] = state["sol"] * price * (1 - FEE)
        state["sol"] = 0.0
        state["trades"] += 1
        action = "SELL"

    value = state["cash"] + state["sol"] * price
    print(f"SOL price: ${price:.2f} | fast EMA {fast[-1]:.2f} | slow EMA {slow[-1]:.2f}")
    print(f"Decision: {action}")
    print(f"Cash ${state['cash']:.2f} | SOL {state['sol']:.4f} | Trades {state['trades']}")
    print(f"Paper portfolio value: ${value:.2f} ({(value / START_CASH - 1) * 100:+.1f}%)")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "paper"
    backtest() if mode == "backtest" else paper()
