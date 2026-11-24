"""Crypto bot v4: honest strategy test. No real money, no wallet, stdlib only.

Usage:  python bot.py backtest   (or env MODE=backtest)
        python bot.py paper

How it stays honest:
1. Rules are chosen ONLY from Bitcoin data before 2020-01-01 (the code literally cuts
   the data there, so it cannot peek at later years).
2. The chosen rules are then frozen and run ONCE on 2020 -> today for BTC, ETH and SOL.
   SOL did not exist before 2020, so it is fully unseen.
3. Signals use only past data and trades happen at the NEXT day's open.
4. Result is compared with a matched coin flip (random long/cash, same number of trades).
"""
import itertools
import json
import os
import random
import sys
import time
import urllib.request

FEE = 0.002  # 0.2% per side
START = 1000.0
CUT = 1577836800  # 2020-01-01 UTC
DEV_BEGIN = 200  # warm-up bars before trading starts in the dev period
TARGET_VOL = 0.03
SIMS = 300


# ---------- data ----------
def fetch_history(sym):
    """Daily candles from CryptoCompare (free, no key). Returns [(ts,o,h,l,c)] oldest first."""
    today = int(time.time() // 86400 * 86400)
    out, to_ts = {}, today
    for _ in range(12):
        url = (f"https://min-api.cryptocompare.com/data/v2/histoday?fsym={sym}"
               f"&tsym=USD&limit=2000&toTs={to_ts}")
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-bot"})
        data = json.load(urllib.request.urlopen(req, timeout=30))
        rows = data["Data"]["Data"]
        good = [r for r in rows if r["close"] > 0 and r["open"] > 0 and r["high"] > 0 and r["low"] > 0]
        for r in good:
            out[r["time"]] = (r["time"], r["open"], r["high"], r["low"], r["close"])
        if not good or len(good) < 50:
            break
        to_ts = rows[0]["time"] - 86400
    candles = sorted(v for k, v in out.items() if k < today)  # drop today's open candle
    return candles


# ---------- signals (all causal: bar i uses data up to and including bar i) ----------
def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def make_signal(candles, kind):
    closes = [c[4] for c in candles]
    n = len(closes)
    if kind[0] == "sma":
        N = kind[1]
        cs, acc = [0.0], 0.0
        for v in closes:
            acc += v
            cs.append(acc)
        return [i + 1 >= N and closes[i] > (cs[i + 1] - cs[i + 1 - N]) / N for i in range(n)]
    if kind[0] == "ema":
        _, f, s, t = kind
        ef, es = ema(closes, f), ema(closes, s)
        et = ema(closes, t) if t else None
        return [i >= s and ef[i] > es[i] and (et is None or (i >= t and closes[i] > et[i])) for i in range(n)]
    if kind[0] == "brk":
        _, a, b = kind
        out, state = [], False
        for i in range(n):
            if i >= max(a, b):
                hi = max(c[2] for c in candles[i - a:i])
                lo = min(c[3] for c in candles[i - b:i])
                if not state and closes[i] > hi:
                    state = True
                elif state and closes[i] < lo:
                    state = False
            out.append(state)
        return out
    raise ValueError(kind)


def daily_vol(closes, i, n=20):
    rets = [closes[j] / closes[j - 1] - 1 for j in range(max(1, i - n + 1), i + 1)]
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5


# ---------- simulator ----------
def equity_curve(candles, want, begin, stop=None, volsize=False):
    """Signal at close of bar i-1 -> trade at open of bar i."""
    closes = [c[4] for c in candles]
    cash, coins, entry = START, 0.0, None
    stopped, trades, eq = False, 0, []
    for i in range(begin, len(candles)):
        _, o, h, l, c = candles[i]
        w = want[i - 1]
        if coins and not w:
            cash = coins * o * (1 - FEE)
            coins, entry = 0.0, None
            trades += 1
        elif not coins and w and not stopped:
            frac = min(1.0, TARGET_VOL / max(daily_vol(closes, i - 1), 1e-9)) if volsize else 1.0
            spend = cash * frac
            coins = spend * (1 - FEE) / o
            cash -= spend
            entry = o
            trades += 1
        if coins and stop and l <= entry * (1 - stop):
            px = min(o, entry * (1 - stop))
            cash += coins * px * (1 - FEE)
            coins, entry = 0.0, None
            trades += 1
            stopped = True
        if not w:
            stopped = False
        eq.append(cash + coins * c)
    return eq, trades


def max_dd(eq):
    peak, worst = START, 0.0
    for v in eq:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak)
    return worst * 100


def yearly(ts, eq):
    res, prev, cur_y, last = {}, START, None, START
    for t, v in zip(ts, eq):
        y = time.gmtime(t).tm_year
        if cur_y is not None and y != cur_y:
            res[cur_y] = (last / prev - 1) * 100
            prev = last
        cur_y, last = y, v
    res[cur_y] = (last / prev - 1) * 100
    return res


def run_cfg(candles, cfg, begin):
    kind, stop, vs = cfg
    want = make_signal(candles, kind)
    return equity_curve(candles, want, begin, stop, vs), want


def bh_curve(candles, begin):
    px = [c[4] for c in candles[begin:]]
    return [START * p / px[0] for p in px]


# ---------- development (pre-2020 only) ----------
def build_grid():
    kinds = [("sma", n) for n in (50, 100, 150, 200)]
    kinds += [("ema", 10, 30, None), ("ema", 10, 30, 100), ("ema", 20, 50, None),
              ("ema", 20, 50, 100), ("ema", 5, 20, None)]
    kinds += [("brk", 20, 10), ("brk", 40, 20), ("brk", 55, 20), ("brk", 100, 30)]
    return [(k, s, v) for k in kinds for s in (None, 0.10, 0.20) for v in (False, True)]


def develop(btc):
    cut = next(i for i, c in enumerate(btc) if c[0] >= CUT)
    dev = btc[:cut]  # the future is physically removed
    ts = [c[0] for c in dev[DEV_BEGIN:]]
    bh_y = yearly(ts, bh_curve(dev, DEV_BEGIN))
    bear_years = [y for y, r in bh_y.items() if r < 0 and y != min(bh_y)]
    rows = []
    for cfg in build_grid():
        (eq, tr), _ = run_cfg(dev, cfg, DEV_BEGIN)
        yy = yearly(ts, eq)
        days = len(eq)
        growth = (eq[-1] / START) ** (365 / days) - 1
        calmar = growth / max(max_dd(eq) / 100, 0.01)
        bear_ok = sum(1 for y in bear_years if yy[y] > bh_y[y]) / max(1, len(bear_years))
        rows.append((bear_ok, calmar, cfg, yy, tr))
    # must beat buy&hold in most dev bear years first, then best return per unit of drawdown
    rows.sort(key=lambda r: (r[0] >= 0.75, r[1]), reverse=True)
    return rows, bh_y, bear_years, cut


# ---------- coin flip benchmark ----------
def coin_flips(candles, begin, n_trades, volsize_frac=1.0):
    rng = random.Random(42)
    q = min(0.5, max(0.001, n_trades / max(1, len(candles) - begin)))
    rets, dds, bears = [], [], []
    ts = [c[0] for c in candles[begin:]]
    for _ in range(SIMS):
        st, want = rng.random() < 0.5, []
        for _ in candles:
            if rng.random() < q:
                st = not st
            want.append(st)
        eq, _ = equity_curve(candles, want, begin)
        rets.append((eq[-1] / START - 1) * 100)
        dds.append(max_dd(eq))
        bears.append(yearly(ts, eq))
    return rets, dds, bears


def pct_rank(sorted_vals, x):
    return 100.0 * sum(1 for v in sorted_vals if v < x) / len(sorted_vals)


# ---------- reports ----------
def test_asset(sym, candles, cfg, begin):
    (eq, tr), _ = run_cfg(candles, cfg, begin)
    ts = [c[0] for c in candles[begin:]]
    yy, bh = yearly(ts, eq), yearly(ts, bh_curve(candles, begin))
    rets, dds, bears = coin_flips(candles, begin, tr)
    tot = (eq[-1] / START - 1) * 100
    bh_tot = (bh_curve(candles, begin)[-1] / START - 1) * 100
    pr = pct_rank(sorted(rets), tot)
    pd = 100.0 - pct_rank(sorted(dds), max_dd(eq))  # higher = shallower drawdown than coin flips
    print(f"\n=== {sym} test {time.strftime('%Y-%m', time.gmtime(ts[0]))} -> {time.strftime('%Y-%m', time.gmtime(ts[-1]))} ===")
    print(f"{'year':6}{'strategy':>10}{'buy&hold':>10}{'coinflip med':>14}")
    for y in sorted(yy):
        med = sorted(b[y] for b in bears)[len(bears) // 2]
        tag = "  <- bear year" if bh[y] < 0 else ""
        print(f"{y:<6}{yy[y]:>+9.0f}%{bh[y]:>+9.0f}%{med:>+13.0f}%{tag}")
    print(f"TOTAL strategy {tot:+.0f}% | buy&hold {bh_tot:+.0f}% | coin-flip median {sorted(rets)[SIMS//2]:+.0f}%")
    print(f"Max drawdown: strategy {max_dd(eq):.0f}% | coin-flip median {sorted(dds)[SIMS//2]:.0f}%")
    print(f"Beats {pr:.0f}% of coin flips on return, {pd:.0f}% on drawdown, {tr} trades")
    bear_y = [y for y in yy if bh[y] < 0]
    wins = 0
    for y in bear_y:
        beat = pct_rank(sorted(b[y] for b in bears), yy[y])
        wins += beat >= 50
        print(f"  bear {y}: strategy {yy[y]:+.0f}% vs buy&hold {bh[y]:+.0f}%, beats {beat:.0f}% of coin flips")
    ok = pr >= 95
    print("VERDICT:", "PASS (>=95th percentile of coin flips)" if ok else "NOT proven better than a coin flip")
    return ok


def label(cfg):
    kind, stop, vs = cfg
    return f"{kind} stop={stop or '-'} vol={'Y' if vs else 'N'}"


def run_backtest():
    data = {s: fetch_history(s) for s in ("BTC", "ETH", "SOL")}
    for s, c in data.items():
        print(f"{s}: {len(c)} daily candles from {time.strftime('%Y-%m-%d', time.gmtime(c[0][0]))}")
    rows, bh_y, bear_years, cut = develop(data["BTC"])
    print(f"\nDEV (BTC, before 2020). Buy&hold by year: " + " ".join(f"{y}:{r:+.0f}%" for y, r in sorted(bh_y.items())))
    print(f"Dev bear years: {bear_years}")
    print("Top 5 dev settings (bear-year win rate, Calmar):")
    for bo, cal, cfg, _, tr in rows[:5]:
        print(f"  {label(cfg):45} bear_win={bo:.0%} calmar={cal:.2f} trades={tr}")
    chosen = rows[0][2]
    print(f"\nFROZEN RULE: {label(chosen)}  (no more changes after this line)")
    results = []
    for s in ("BTC", "ETH", "SOL"):
        c = data[s]
        begin = max(100, next((i for i, x in enumerate(c) if x[0] >= CUT), 100))
        results.append(test_asset(s, c, chosen, begin))
    print(f"\nPassed {sum(results)} of {len(results)} assets.")
    print("Tested once on unseen years. Do not re-tune on these results; that would break the test.")


def run_paper():
    btc = fetch_history("BTC")
    rows, *_ = develop(btc)
    cfg = rows[0][2]
    print("Rule (chosen on pre-2020 BTC):", label(cfg))
    for s in ("BTC", "SOL"):
        c = fetch_history(s) if s != "BTC" else btc
        w = make_signal(c, cfg[0])[-1]
        print(f"{s} last close {c[-1][4]:.2f}: {'HOLD' if w else 'CASH'}")
    print("Paper only, no real trades.")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "backtest")).lower()
    run_paper() if mode == "paper" else run_backtest()

