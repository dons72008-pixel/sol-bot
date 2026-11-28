"""Crypto bot v4.9: honest strategy test. No real money, no wallet, stdlib only.

Usage:  python bot.py backtest   (or env MODE=backtest)
        python bot.py paper

How it stays honest:
1. Rules are chosen ONLY from Bitcoin data before 2020-01-01 (the code literally cuts
   the data there, so it cannot peek at later years).
2. The chosen rules are then frozen and run ONCE on 2020 -> today for BTC, ETH and SOL.
   SOL did not exist before 2020, so it is fully unseen.
3. Signals use only past data and trades happen at the NEXT day's open.
4. Result is compared with a matched coin flip (random long/cash, same number of trades).

v4.9: candle table also shows the drop along the way (report only).

v4.8: candle table uses all history, shown before and since 2020 (report only).

v4.7: completes the hammer family (hanging man, inverted hammer), report only.

v4.6: adds the "John Wick" candle (a huge wick), report only.

v4.5: reads more candle shapes (report only; the candle filter is unchanged).

v4.4: reads candle shapes, reports what happened after each, and compares a candle filter.

v4.3: adds a "memory" version of the rule, shown side by side for comparison only.

v4.2: adds a trade journal, loss report, floating P&L and a Faber 10-month comparison.
The way the rule is chosen is unchanged.

v4.1: data now comes from Yahoo Finance (backup: Coinbase). No API key needed.
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
EARLIEST = 1388534400  # 2014-01-01 UTC


# ---------- data ----------
def _get_json(url, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; crypto-bot)"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (k + 1))
    raise last


def _yahoo(sym):
    now = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}-USD"
           f"?period1={EARLIEST}&period2={now}&interval=1d")
    r = _get_json(url)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        out.append((t // 86400 * 86400, o, h, l, c))
    return out


def _coinbase(sym):
    def iso(x):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x))
    out, end = {}, int(time.time() // 86400 * 86400)
    while end > EARLIEST:
        start = end - 299 * 86400
        url = (f"https://api.exchange.coinbase.com/products/{sym}-USD/candles"
               f"?granularity=86400&start={iso(start)}&end={iso(end)}")
        rows = _get_json(url)
        if not rows:
            break
        for t, l, h, o, c, _v in rows:
            if min(o, h, l, c) > 0:
                out[t] = (t, o, h, l, c)
        end = start - 86400
        time.sleep(0.4)
    return list(out.values())


def fetch_history(sym):
    """Daily candles [(ts,o,h,l,c)] oldest first. Yahoo first, Coinbase as backup."""
    today = int(time.time() // 86400 * 86400)
    candles, src = [], None
    for name, fn in (("Yahoo", _yahoo), ("Coinbase", _coinbase)):
        try:
            candles = fn(sym)
            if len(candles) >= 200:
                src = name
                break
        except Exception as e:  # noqa
            print(f"{sym}: {name} failed ({e})")
    if not src:
        raise RuntimeError(f"No data source worked for {sym}")
    print(f"{sym}: data from {src}")
    uniq = {c[0]: c for c in candles if c[0] < today}  # drop today's open candle
    return [uniq[k] for k in sorted(uniq)]


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


# ---------- trade journal ----------
def conditions(closes, i):
    """Market state known at the close of bar i (no future data)."""
    trend = "no-data"
    if i >= 199:
        trend = "uptrend" if closes[i] > sum(closes[i - 199:i + 1]) / 200 else "downtrend"
    chop = "no-data"
    if i >= 20:
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - 19, i + 1))
        chop = "sideways" if abs(closes[i] - closes[i - 20]) < 0.3 * path else "trending"
    vol = "wild" if i >= 1 and daily_vol(closes, i) > 0.04 else "calm"
    return {"trend": trend, "chop": chop, "vol": vol}


def _close(log, ts, px, why):
    t = log[-1]
    t.update(out=ts, sell=px, why=why, pnl=(px * (1 - FEE) ** 2 / t["buy"] - 1) * 100)


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def journal_report(sym, log, last=10):
    done = [t for t in log if "pnl" in t]
    if not done:
        print(f"{sym} journal: no closed trades")
        return
    lost = [t for t in done if t["pnl"] <= 0]
    print(f"\n{sym} journal: {len(done)} closed trades, {len(lost)} lost. Last {min(last, len(done))}:")
    for t in done[-last:]:
        print(f"  {day(t['in'])} -> {day(t['out'])} {t['pnl']:+6.1f}% exit={t['why']:9} {t['trend']}, {t['chop']}, {t['vol']}")
    print("Where it lost (lost/total, average result):")
    for key in ("trend", "chop", "vol", "why"):
        for val in sorted({t[key] for t in done}):
            g = [t for t in done if t[key] == val]
            bad = sum(1 for t in g if t["pnl"] <= 0)
            few = "  (too few to judge)" if len(g) < 10 else ""
            print(f"  {val:10} {bad}/{len(g)} lost, avg {sum(t['pnl'] for t in g) / len(g):+.1f}%{few}")


def faber_signal(candles):
    """Month end: hold if close > average of the last 10 month-end closes."""
    ends, out, state = [], [], False
    for t, _o, _h, _l, c in candles:
        if time.gmtime(t).tm_mon != time.gmtime(t + 86400).tm_mon:
            ends.append(c)
            state = len(ends) >= 10 and c > sum(ends[-10:]) / 10
        out.append(state)
    return out


# ---------- memory: learn from past mistakes ----------
MIN_SEEN = 5  # a pattern must be seen this often before it can be blocked


def memory_signal(candles, want):
    """Skip a new buy if the same market pattern lost money on average before.

    It learns from every earlier trade of the base rule (taken or skipped), and
    only from trades already closed on the day of the decision (no peeking).
    """
    past = []
    equity_curve(candles, want, 200, log=past)
    closes = [c[4] for c in candles]
    out, skip, blocked = [], False, []
    for i, w in enumerate(want):
        if w and (i == 0 or not want[i - 1]):
            now = conditions(closes, i)
            same = [t["pnl"] for t in past if "pnl" in t and t["out"] <= candles[i][0]
                    and all(t[k] == now[k] for k in now)]
            skip = len(same) >= MIN_SEEN and sum(same) / len(same) < 0
            if skip:
                blocked.append((i, now, len(same), sum(same) / len(same)))
        out.append(w and not skip)
    return out, blocked


# ---------- candle shapes ----------
BEARISH = ("shooting star", "bearish engulfing")


def candle_shapes(candles, i):
    """Shapes visible at the close of day i (uses days up to i only)."""
    _, o, h, l, c = candles[i]
    rng, body = h - l, abs(c - o)
    if rng <= 0 or i < 6:
        return []
    upper, lower = h - max(o, c), min(o, c) - l
    fell, rose = candles[i - 1][4] < candles[i - 6][4], candles[i - 1][4] > candles[i - 6][4]
    po, pc = candles[i - 1][1], candles[i - 1][4]
    found = []
    if body <= 0.1 * rng:
        found.append("doji")
    elif lower >= 2 * body and upper <= body and fell:
        found.append("hammer")
    elif upper >= 2 * body and lower <= body and rose:
        found.append("shooting star")
    if pc < po and c > o and o <= pc and c >= po and body > po - pc:
        found.append("bullish engulfing")
    if pc > po and c < o and o >= pc and c <= po and body > pc - po:
        found.append("bearish engulfing")
    # rest of the hammer family: same shapes, opposite prior move
    if body > 0.1 * rng:
        if lower >= 2 * body and upper <= body and rose:
            found.append("hanging man")
        if upper >= 2 * body and lower <= body and fell:
            found.append("inverted hammer")
    # v4.5 extra shapes. Crypto trades 24/7, so none of these require a price gap.
    if body >= 0.9 * rng:
        found.append("green marubozu" if c > o else "red marubozu")
    if 0.1 * rng < body <= 0.3 * rng and upper > body and lower > body:
        found.append("spinning top")
    pbody, pmid = abs(pc - po), (po + pc) / 2
    prng = candles[i - 1][2] - candles[i - 1][3]
    long_prev = pbody >= 0.6 * prng and pbody > 0
    if long_prev and body < pbody and max(o, c) <= max(po, pc) and min(o, c) >= min(po, pc):
        if pc < po and c > o:
            found.append("bullish harami")
        elif pc > po and c < o:
            found.append("bearish harami")
    if long_prev and pc < po and c > o and o <= pc and pmid < c < po:
        found.append("piercing line")
    if long_prev and pc > po and c < o and o >= pc and po < c < pmid:
        found.append("dark cloud cover")
    _, ao, ah, al, ac = candles[i - 2]
    abody = abs(ac - ao)
    if abody >= 0.6 * (ah - al) and abody > 0 and pbody <= 0.3 * abody:
        if ac < ao and c > o and c > (ao + ac) / 2:
            found.append("morning star")
        elif ac > ao and c < o and c < (ao + ac) / 2:
            found.append("evening star")
    three = candles[i - 2:i + 1]
    strong = all(abs(x[4] - x[1]) >= 0.6 * (x[2] - x[3]) > 0 for x in three)
    if strong and all(x[4] > x[1] for x in three) and ac < pc < c:
        found.append("three white soldiers")
    if strong and all(x[4] < x[1] for x in three) and ac > pc > c:
        found.append("three black crows")
    # John Wick: much bigger than usual, and one wick is most of the candle
    if i >= 20:
        usual = sum(x[2] - x[3] for x in candles[i - 20:i]) / 20
        if rng >= 2 * usual and lower >= 0.66 * rng:
            found.append("John Wick (low)")
        if rng >= 2 * usual and upper >= 0.66 * rng:
            found.append("John Wick (high)")
    return found


def candle_signal(candles, want):
    """Do not START a position on a day with a bearish shape; check again next day."""
    out, inside = [], False
    for i, w in enumerate(want):
        inside = w and (inside or not any(x in BEARISH for x in candle_shapes(candles, i)))
        out.append(inside)
    return out


def shape_report(sym, candles, begin, end, period, days=5):
    """Average price move over the next days after each shape, against all days."""
    seen, every = {}, []
    for i in range(begin, end - days):
        move = (candles[i + days][4] / candles[i + 1][1] - 1) * 100
        # lowest price during those days, measured from the buy price (0 = never below it)
        dip = min(0.0, (min(x[3] for x in candles[i + 1:i + 1 + days]) / candles[i + 1][1] - 1) * 100)
        every.append((move, dip))
        for name in candle_shapes(candles, i):
            seen.setdefault(name, []).append((move, dip))
    if not every:
        print(f"\n{sym} candle shapes {period}: no data")
        return
    print(f"\n{sym} candle shapes {period}: what happened by the {days}th day after (before fees)")
    print("  dip = typical drop along the way; bad dip = 1 time in 10 it was this deep or deeper")
    for name, both in [("ANY day", every)] + sorted(seen.items()):
        m = [x for x, _ in both]
        dips = sorted(d for _, d in both)  # most negative first
        up = 100 * sum(1 for v in m if v > 0) / len(m)
        few = "  (too few to judge)" if len(m) < 30 else ""
        print(f"  {name:20} seen {len(m):5}  went up {up:3.0f}%  average {sum(m) / len(m):+.1f}%"
              f"  dip {dips[len(dips) // 2]:+.1f}%  bad dip {dips[(len(dips) - 1) // 10]:+.1f}%{few}")


# ---------- simulator ----------
def equity_curve(candles, want, begin, stop=None, volsize=False, log=None):
    """Signal at close of bar i-1 -> trade at open of bar i."""
    closes = [c[4] for c in candles]
    cash, coins, entry = START, 0.0, None
    stopped, trades, eq = False, 0, []
    for i in range(begin, len(candles)):
        _, o, h, l, c = candles[i]
        w = want[i - 1]
        if coins and not w:
            if log is not None:
                _close(log, candles[i][0], o, "signal")
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
            if log is not None:
                log.append({"in": candles[i][0], "buy": o, **conditions(closes, i - 1)})
        if coins and stop and l <= entry * (1 - stop):
            px = min(o, entry * (1 - stop))
            cash += coins * px * (1 - FEE)
            if log is not None:
                _close(log, candles[i][0], px, "stop-loss")
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
    log = []
    eq, tr = equity_curve(candles, make_signal(candles, cfg[0]), begin, cfg[1], cfg[2], log)
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
    f_eq, f_tr = equity_curve(candles, faber_signal(candles), begin)
    print("\nCOMPARE (same dates, fees included)   return   max drawdown   orders")
    m_want, blocked = memory_signal(candles, make_signal(candles, cfg[0]))
    m_eq, m_tr = equity_curve(candles, m_want, begin, cfg[1], cfg[2])
    k_eq, k_tr = equity_curve(candles, candle_signal(candles, make_signal(candles, cfg[0])), begin, cfg[1], cfg[2])
    for name, curve, n in (("frozen rule", eq, tr), ("rule + memory", m_eq, m_tr),
                           ("rule + candles", k_eq, k_tr),
                           ("Faber 10-month", f_eq, f_tr),
                           ("buy & hold", bh_curve(candles, begin), 0)):
        print(f"  {name:32}{(curve[-1] / START - 1) * 100:>+8.0f}%{max_dd(curve):>13.0f}%{n:>9}")
    journal_report(sym, log)
    cut = next((i for i, x in enumerate(candles) if x[0] >= CUT), len(candles))
    shape_report(sym, candles, 6, cut, "BEFORE 2020")
    shape_report(sym, candles, cut, len(candles), "SINCE 2020")
    recent = [b for b in blocked if b[0] >= begin]
    print(f"Memory blocked {len(recent)} buys in the test years. Last 5:")
    for i, now, n, avg in recent[-5:]:
        print(f"  {day(candles[i][0])} {now['trend']}, {now['chop']}, {now['vol']}: seen {n} times, avg {avg:+.1f}%")
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
        want = make_signal(c, cfg[0])
        last = c[-1][4]
        log = []
        # replay history to see whether the paper position is open
        equity_curve(c, want, 200, cfg[1], cfg[2], log)
        print(f"{s} last close {last:.2f}: signal {'HOLD' if want[-1] else 'CASH'}")
        print(f"  with memory: {'HOLD' if memory_signal(c, want)[0][-1] else 'CASH'}")
        print(f"  with candles: {'HOLD' if candle_signal(c, want)[-1] else 'CASH'}")
        print(f"  last candle: {', '.join(candle_shapes(c, len(c) - 1)) or 'no listed shape'}")
        if log and "pnl" not in log[-1]:
            t = log[-1]
            print(f"  FLOATING: bought {day(t['in'])} at {t['buy']:.2f}, "
                  f"now {(last / t['buy'] - 1) * 100:+.1f}% (before fees)")
            if not want[-1]:
                print("  Sell signal: would sell at the next open")
        else:
            print("  No open position, nothing floating")
    print("Paper only, no real trades.")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "backtest")).lower()
    run_paper() if mode == "paper" else run_backtest()
