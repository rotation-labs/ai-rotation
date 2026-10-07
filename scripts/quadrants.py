"""Backtest the page's quadrant shifts on the dots it actually draws.

scripts/backtest.py tests RRG readings on individual stocks and ETFs. This script asks the
question a reader of the chart asks: when a dot on the page - a tier, a layer, a subsector, a
stock, an S&P sector - moves from one quadrant to another, what happens next? It rebuilds each
dot with the page's own arithmetic (template.html: combine(), rrg(), quad(), the membership
windows and each view's benchmark) and runs three tests:

  event study  every quadrant change, by kind, with the dot's relative return over the bars
               before it and over 1-26 weeks after it. Returns start at the close of the
               session after the signal bar, the first close someone reading that evening's
               rebuild could trade at. The edge is the dot's return against the other dots on
               the same chart, minus what the same dots earned at random times (the placebo).
  placebo      each p-value re-runs the identical calculation with every event moved by a
               common offset in time (circularly, more than one horizon away), for up to 300
               offsets. That keeps the number of events, their clustering across dots and
               dates, and the drift of the dots they land on, and breaks only the timing - the
               thing under test. Overlapping holding periods need no separate correction.
  rules        trading the quadrants: hold, each bar, the dots in a quadrant (or a pair) and
               compare with holding every dot on that chart equally, with the same timing
               placebo and 15bp one-way costs.

All three run on weekly bars (the page's default) and daily bars, over the last three years
(what the page shows) and the full download, and on the S&P sectors since 1999 as well, the
one universe not chosen with hindsight. Splits by the page's settings-agreement badge and by
the benchmark's 200-day trend say whether the shifts the page treats as firmer do better.

Reads build/data.json (run scripts/fetch.py with FETCH_PERIOD=10y for the long sample) and
downloads the sector ETFs' full history itself (QUAD_LONG=0 skips that). Writes
build/backtest/quadrants.md, quadrant_events.csv, quadrant_rules.csv and
quadrant_event_log.csv.
"""
import json, os, sys, warnings
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "build" / "data.json"
OUT = ROOT / "build" / "backtest"
COST = 0.0015            # one-way trading cost per unit of notional, 15bp, as in backtest.py
SHIFTS = 300             # placebo offsets per cell, at most
MIN_EVENTS = 20          # fewer events than this and a cell is left blank
SIG = 0.05

# The page's seven tiers and the benchmark each tier's layers default to (template.html DOMAINS)
DOMAINS = [("semis", "Core Semis", "SOXX"), ("compute", "AI Compute", "QQQ"),
           ("network", "Networking & Optics", "QQQ"), ("power", "Power & Buildout", "QQQ"),
           ("platforms", "Platforms & Software", "QQQ"), ("physical", "Physical AI", "QQQ"),
           ("bio", "Bio AI", "QQQ")]
SECTOR_ETFS = ["XLK", "XLC", "XLY", "XLF", "XLV", "XLI", "XLE", "XLB", "XLU", "XLRE", "XLP"]

L, W, G, I = 0, 1, 2, 3
QN = {L: "Leading", W: "Weakening", G: "Lagging", I: "Improving"}
# Clockwise is the textbook rotation; the others are hooks back and jumps across the centre.
MOVES = [(G, I), (I, L), (L, W), (W, G), (I, G), (W, L), (L, I), (G, W), (G, L), (L, G), (I, W), (W, I)]
GROUPS = [("Into Leading or Improving", {L, I}), ("Into Weakening or Lagging", {W, G})]
RULES = [("Leading", {L}), ("Improving", {I}), ("Weakening", {W}), ("Lagging", {G}),
         ("Leading or Improving", {L, I}), ("Leading or Weakening", {L, W}), ("All but Lagging", {L, W, I})]
FRAMES = {"W": dict(n=10, name="weekly", unit="w", horizons=[1, 4, 13, 26], before=13, ppy=52, gap=13),
          "D": dict(n=21, name="daily", unit="d", horizons=[5, 21, 63], before=63, ppy=252, gap=63)}

warnings.filterwarnings("ignore", category=RuntimeWarning)


# ---------------------------------------------------------------- the page's arithmetic

def ema(a, p):
    """template.html ema(), column-wise: restarts after a gap instead of bridging it."""
    k = 2 / (p + 1)
    out = np.full(a.shape, np.nan)
    e = np.full(a.shape[1], np.nan)
    for t in range(len(a)):
        v = a[t]
        e = np.where(np.isnan(v), np.nan, np.where(np.isnan(e), v, e + k * (v - e)))
        out[t] = e
    return out


def sma(a, n):
    return pd.DataFrame(a).rolling(n, min_periods=n).mean().to_numpy()


def rrg(rs, n, e=3, base=None, m=1):
    """template.html rrg(): RS-Ratio and RS-Momentum for a (bars x dots) frame of RS."""
    sm = rs if e <= 1 else ema(rs, e)
    rsr = 100 * sm / sma(sm, base or n)
    prev = np.full(rsr.shape, np.nan)
    prev[m:] = rsr[:-m]
    roc = 100 * rsr / prev
    return rsr, (roc if e <= 1 else ema(roc, e))


def alts(n):
    """template.html ALTS(): the four alternative settings behind the page's agreement badge."""
    return [dict(m=4), dict(e=5, base=round(n * 1.4)), dict(base=round(n * 2.6)), dict(e=1)]


def quad(x, y):
    """template.html quad(); -1 where either coordinate is missing."""
    q = np.where(x >= 100, np.where(y >= 100, L, W), np.where(y >= 100, I, G))
    return np.where(np.isnan(x) | np.isnan(y), -1, q)


def combine(cols, wts):
    """template.html combine(): a basket index that starts at 100 on the first day any member
    trades and compounds the weighted mean daily return of whichever members trade both days."""
    A = np.column_stack(cols)
    w = np.asarray(wts, float)
    out = np.full(len(A), np.nan)
    have = ~np.isnan(A).all(axis=1)
    if not have.any():
        return out
    s0 = int(np.argmax(have))
    p, q = A[:-1], A[1:]
    ok = ~np.isnan(p) & ~np.isnan(q) & (np.nan_to_num(p) > 0)
    r = np.where(ok, q / np.where(ok, p, 1) - 1, 0)
    ww = np.where(ok, w, 0)
    k = ww.sum(1)
    g = np.where(k > 0, 1 + (r * ww).sum(1) / np.where(k > 0, k, 1), 1)
    out[s0] = 100
    out[s0 + 1:] = 100 * np.cumprod(g[s0:])
    return out


def unpack(m):
    return (list(m), None) if isinstance(m, list) else (list(m["_tickers"]), m.get("_weights"))


def tree(d):
    """The page's tiers, layers and subsectors, with the benchmark each one's view uses."""
    peers = d.get("peers") or {}
    tiers, layers = [], []
    for g, label, dbench in DOMAINS:
        if g not in d["groups"]:
            continue
        tier = dict(kind="domain", label=label, leaves=[])
        for fam, node in d["groups"][g].items():
            lay = dict(kind="family", label=fam, bench=peers.get(fam, dbench), path=fam, subs=[], members=None)
            if isinstance(node, list) or "_tickers" in node:      # a layer that lists stocks directly
                lay["members"], lay["weights"] = unpack(node)
                lay["leaves"] = [lay]
            else:
                for name, m in node.items():
                    mem, wts = unpack(m)
                    lay["subs"].append(dict(kind="sub", label=name, bench=lay["bench"], path=f"{fam} / {name}",
                                            members=mem, weights=wts, leaves=None))
                    lay["subs"][-1]["leaves"] = [lay["subs"][-1]]
                lay["leaves"] = lay["subs"]
            tier["leaves"] += lay["leaves"]
            layers.append(lay)
        tiers.append(tier)
    return tiers, layers


def windows(node, entries):
    """template.html windows(): ticker -> the [from, until) windows it counts in under node."""
    out = {}
    paths = {b["path"] for b in node["leaves"]}
    for b in node["leaves"]:
        for t in b["members"]:
            e = next((x for x in entries if x["ticker"] == t and x["basket"] == b["path"]), None)
            out.setdefault(t, []).append((e.get("from"), e.get("until")) if e else (None, None))
    for e in entries:
        if e["basket"] in paths and not any(b["path"] == e["basket"] and e["ticker"] in b["members"]
                                            for b in node["leaves"]):
            out.setdefault(e["ticker"], []).append((e.get("from"), e.get("until")))
    return out


def basket(node, px, dates, entries):
    """template.html series() for a subsector, layer or tier. Weights apply only at the
    subsector's own level; layers and tiers equal-weight their distinct stocks."""
    cols, wts = [], []
    weights = (node.get("weights") or {}) if node["kind"] == "sub" else {}
    for t, ws in windows(node, entries).items():
        if t not in px:
            continue
        s = px[t]
        if not any(f is None and u is None for f, u in ws):
            keep = np.zeros(len(dates), bool)
            for f, u in ws:
                keep |= (dates >= (f or "")) & ((dates < u) if u else True)
            s = np.where(keep, s, np.nan)
        cols.append(s)
        wts.append(weights.get(t, 1))
    return combine(cols, wts) if cols else None


# ---------------------------------------------------------------- universes

class Universe:
    """Dots drawn against a benchmark, grouped by the chart they share. Each dot is
    (chart, label, daily series, benchmark); a chart's dots are contiguous."""
    def __init__(self, key, name, dates, px, dots, hindsight):
        self.key, self.name, self.dates, self.px, self.hindsight = key, name, dates, px, hindsight
        self.dots = [x for x in dots if x[2] is not None and np.isfinite(x[2]).sum() > 60 and x[3] in px]


def ai_universes(d, dates, px):
    entries = [e for e in (d.get("membership") or {}).get("entries", []) if e.get("ticker") and e.get("basket")]
    tiers, layers = tree(d)
    b = lambda n: basket(n, px, dates, entries)
    stocks = []
    for lay in layers:
        for node, chart in ([(lay, lay["label"])] if lay["members"] else
                            [(s, f"{lay['label']} / {s['label']}") for s in lay["subs"]]):
            stocks += [(chart, t, px.get(t), node["bench"]) for t in node["members"] if t != node["bench"]]
    return [
        Universe("tiers", "Tiers vs QQQ", dates, px, [("Full stack", t["label"], b(t), "QQQ") for t in tiers], True),
        Universe("layers", "Layers vs QQQ", dates, px, [("Full stack, layers", x["label"], b(x), "QQQ") for x in layers], True),
        Universe("subs", "Subsectors vs their layer's peer", dates, px,
                 [(x["label"], s["label"], b(s), s["bench"]) for x in layers for s in x["subs"]], True),
        Universe("stocks", "Stocks vs their basket's peer", dates, px, stocks, True),
    ]


def sector_universe(key, name, dates, px):
    return Universe(key, name, dates, px, [("S&P 500 sectors", t, px.get(t), "SPY") for t in SECTOR_ETFS], False)


def load():
    d = json.loads(DATA.read_text())
    dates = np.array(d["dates"])
    px = {k: np.array([np.nan if v is None else v for v in s], float) for k, s in d["px"].items()}
    if d.get("live"):              # today's bar is still moving: drop it
        dates = dates[:-1]
        px = {k: v[:-1] for k, v in px.items()}
    return d, dates, px


def download_max(syms, since="1999-01-01"):
    """Daily closes for syms over Yahoo's full history from `since`, on SPY's calendar and
    forward-filled as fetch.py does; None if SPY can't be had."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    syms = list(dict.fromkeys(list(syms) + ["SPY"]))
    frame = pd.DataFrame()
    for attempt in range(3):
        need = [s for s in syms if s not in frame or frame[s].dropna().empty]
        if not need:
            break
        try:
            x = yf.download(need, period="max", interval="1d", progress=False, auto_adjust=True, threads=True)["Close"]
            if isinstance(x, pd.Series):
                x = x.to_frame(need[0])
            x.index = pd.to_datetime([str(i.date()) for i in x.index])
            frame = x if frame.empty else frame.combine_first(x)
        except Exception as e:
            print(f"download attempt {attempt + 1} failed: {e}", file=sys.stderr)
    if "SPY" not in frame or frame["SPY"].dropna().empty:
        print("warning: no long history; skipping it", file=sys.stderr)
        return None
    frame = frame[(frame.index >= since) & frame["SPY"].notna()].ffill()
    today = pd.Timestamp.now(tz="America/New_York")
    if frame.index[-1].date() == today.date() and today.time() < pd.Timestamp("16:15").time():
        frame = frame.iloc[:-1]    # an unfinished session
    dates = np.array([str(i.date()) for i in frame.index])
    return dates, {s: frame[s].to_numpy(float) for s in frame}


def load_long_sectors():
    """The SPDR sectors and SPY since 1999, straight from Yahoo; None if that fails."""
    if os.environ.get("QUAD_LONG", "1") == "0":
        return None
    return download_max(SECTOR_ETFS)


# ---------------------------------------------------------------- panels

def bars_of(dates, frame):
    """template.html buildWeekIdx() for weekly bars (the last session of each week), dropping
    a final week that has not finished; every session for daily bars."""
    if frame == "D":
        return np.arange(len(dates))
    d = pd.to_datetime(dates)
    wd = np.asarray(d.weekday)
    gap = np.asarray((d[1:] - d[:-1]).days) > 6
    idx = np.flatnonzero(np.r_[(wd[1:] < wd[:-1]) | gap, True])
    return idx[:-1] if wd[-1] != 4 else idx


def lag(a, k):
    """a shifted down k rows (a value from k bars earlier); k < 0 looks ahead."""
    out = np.full(a.shape, np.nan)
    if k > 0:
        out[k:] = a[:-k]
    elif k < 0:
        out[:k] = a[-k:]
    else:
        out[:] = a
    return out


class Panel:
    """Everything the tests need for one universe on one bar frequency."""
    def __init__(self, U, frame):
        F = FRAMES[frame]
        n = F["n"]
        self.U, self.frame, self.F = U, frame, F
        bars = bars_of(U.dates, frame)
        self.bars, self.dates = bars, U.dates[bars]
        P = np.column_stack([x[2] for x in U.dots])
        B = np.column_stack([U.px[x[3]] for x in U.dots])
        rs = 100 * P[bars] / B[bars]
        rsr, rsm = rrg(rs, n)
        self.q = quad(rsr, rsm)
        self.agree = np.ones(self.q.shape)
        self.evals = np.ones(self.q.shape)
        for o in alts(n):
            qo = quad(*rrg(rs, n, **o))
            self.evals += qo >= 0
            self.agree += (qo >= 0) & (qo == self.q)
        self.qprev = np.vstack([np.full((1, self.q.shape[1]), -1), self.q[:-1]])
        self.changed = (self.q >= 0) & (self.qprev >= 0) & (self.q != self.qprev)
        # Execution: the close of the session after the signal bar.
        ex = bars + 1
        ok = ex < len(U.dates)
        ex = np.where(ok, ex, len(U.dates) - 1)
        Pe, Be = P[ex], B[ex]
        Pe[~ok], Be[~ok] = np.nan, np.nan
        self.lr = np.log(Pe / Be)                 # log RS at execution
        self.lr_bar = np.log(rs)                  # log RS at the signal bar
        self.R = lag(Pe, -1) / Pe - 1             # dot return, this execution to the next
        self.Rrel = (1 + self.R) / (1 + lag(Be, -1) / Be - 1) - 1
        avg = pd.DataFrame(B).rolling(200, min_periods=200).mean().to_numpy()[bars]
        self.up = np.where(np.isnan(avg), np.nan, (B[bars] > avg).astype(float))
        charts = [x[0] for x in U.dots]
        self.starts = np.flatnonzero([i == 0 or charts[i] != charts[i - 1] for i in range(len(charts))])
        self.sizes = np.diff(np.r_[self.starts, len(charts)])

    def gsum(self, A):
        """Per-chart sums along the dot axis, repeated back onto each chart's dots."""
        return np.repeat(np.add.reduceat(A, self.starts, axis=1), self.sizes, axis=1)

    def siblings(self, X):
        """Each dot's value minus the mean of the other dots on its chart at the same bar."""
        ok = ~np.isnan(X)
        x0 = np.where(ok, X, 0)
        s, c = self.gsum(x0), self.gsum(ok.astype(float))
        others = (s - x0) / (c - ok)
        return np.where(ok & (c - ok > 0), X - others, np.nan)

    def fwd(self, h):
        return lag(self.lr, -h) - self.lr

    def before(self, k):
        return self.siblings(self.lr_bar - lag(self.lr_bar, k))


def events(p, kind):
    """Boolean (bars x dots) frame of one kind of shift."""
    if isinstance(kind, tuple):
        a, b = kind
        return p.changed & (p.qprev == a) & (p.q == b)
    return p.changed & np.isin(p.q, list(kind))


def label(kind):
    return f"{QN[kind[0]]} → {QN[kind[1]]}" if isinstance(kind, tuple) else next(n for n, k in GROUPS if k == kind)


# ---------------------------------------------------------------- statistics

def offsets(T, gap):
    lo, hi = gap + 1, T - gap - 1
    if hi < lo:
        return np.array([], int)
    return np.unique(np.linspace(lo, hi, min(SHIFTS, hi - lo + 1)).round().astype(int))


def date_means(E, X, shifts):
    """Mean over event dates of the mean outcome of that date's events, for the real timing
    (first value) and with every event moved forward by each offset, wrapping at the end."""
    u, i = np.nonzero(E)
    s = np.r_[0, shifts].astype(int)
    if not len(u):
        return np.full(len(s), np.nan)
    starts = np.flatnonzero(np.r_[True, u[1:] != u[:-1]])
    out = np.empty(len(s))
    for c in range(0, len(s), 64):
        ss = s[c:c + 64]
        v = X[(u[:, None] + ss[None, :]) % len(E), i[:, None]]
        ok = ~np.isnan(v)
        m = np.add.reduceat(np.where(ok, v, 0), starts, axis=0) / np.add.reduceat(ok.astype(float), starts, axis=0)
        out[c:c + 64] = np.nanmean(m, axis=0)
    return out


def placebo(vals):
    """(observed, placebo mean, two-sided p) from date_means() output."""
    obs, pl = vals[0], vals[1:]
    pl = pl[~np.isnan(pl)]
    if np.isnan(obs) or len(pl) < 20:
        return obs, np.nan, np.nan
    c = pl.mean()
    return obs, c, (1 + (np.abs(pl - c) >= abs(obs - c) - 1e-12).sum()) / (1 + len(pl))


def study(p, r0, kinds, sample):
    """Event-study rows for one panel over rows r0: (one row per kind x horizon)."""
    F, rows = p.F, []
    T = len(p.q) - r0
    sl = slice(r0, None)
    sib = {h: p.siblings(p.fwd(h))[sl] for h in F["horizons"]}
    raw = {h: p.fwd(h)[sl] for h in F["horizons"]}
    pre = p.before(F["before"])[sl]
    splits = [("all", None), ("firm", (p.evals - p.agree) <= 1), ("fragile", (p.evals - p.agree) >= 2),
              ("uptrend", p.up == 1), ("downtrend", p.up == 0)]
    for kind in kinds:
        E0 = events(p, kind)
        for split, mask in splits:
            E = (E0 if mask is None else E0 & mask)[sl]
            n = int(E.sum())
            if split != "all" and n < MIN_EVENTS:
                continue
            for h in F["horizons"]:
                if split != "all" and h != F["horizons"][-2]:
                    continue            # splits are reported at the second-longest horizon only
                X = sib[h]
                obs, base, pval = placebo(date_means(E, X, offsets(T, h)))
                hits = X[E & ~np.isnan(X)]
                rows.append(dict(universe=p.U.key, name=p.U.name, frame=p.frame, sample=sample, split=split,
                                 move=label(kind), horizon=h, events=n,
                                 dates=int(E.any(axis=1).sum()),
                                 before=date_means(E, pre, [])[0], raw=date_means(E, raw[h], [])[0],
                                 after=obs, baseline=base, edge=obs - base, p=pval,
                                 hit=(hits > 0).mean() if len(hits) else np.nan))
    return rows


def rule_series(p, H, sl):
    """Per-bar active return of holding H's dots against every dot on the same chart, the
    average of that across charts, and the bookkeeping that goes with it."""
    R, Rrel = p.R[sl], p.Rrel[sl]
    ok = ~np.isnan(R)
    h = H & ok
    na, nh = p.gsum(ok.astype(float)), p.gsum(h.astype(float))
    r0, x0 = np.where(ok, R, 0), np.where(ok, Rrel, 0)
    ew = p.gsum(r0) / na
    held = nh > 0
    pr = np.where(held, p.gsum(np.where(h, r0, 0)) / np.where(held, nh, 1), ew)
    rel = np.where(held, p.gsum(np.where(h, x0, 0)) / np.where(held, nh, 1), p.gsum(x0) / na)
    first = p.starts
    act = np.nanmean((pr - ew)[:, first], axis=1)
    # A rule only differs from the chart when it holds some of its dots but not all of them.
    apart = held[:, first] & (nh[:, first] < na[:, first])
    return act, np.nanmean(rel[:, first], axis=1), apart, nh[:, first], np.where(held, h / np.where(held, nh, 1), ok / na)


def rules(p, r0, sample):
    F, rows = p.F, []
    sl = slice(r0, None)
    T = len(p.q) - r0
    ppy = F["ppy"]
    for name, qs in RULES:
        H = np.isin(p.q, list(qs))
        act, rel, apart, nh, w = rule_series(p, H[sl], sl)
        live = ~np.isnan(act)
        if live.sum() < 2 * F["gap"]:
            continue
        pl = []
        for s in offsets(T, F["gap"]):
            a, *_ = rule_series(p, np.roll(H[sl], s, axis=0), sl)
            pl.append(np.nanmean(a))
        pl = np.array(pl)
        obs, c = np.nanmean(act), np.nanmean(pl) if len(pl) else np.nan
        pval = (1 + (np.abs(pl - c) >= abs(obs - c) - 1e-12).sum()) / (1 + len(pl)) if len(pl) >= 20 else np.nan
        dw = np.abs(np.nan_to_num(w) - lag(np.nan_to_num(w), 1))
        dw[0] = 0
        turn = np.nanmean(np.add.reduceat(dw, p.starts, axis=1), axis=1)[live].mean() * ppy
        wealth = np.cumprod(1 + act[live])
        dd = (wealth / np.maximum.accumulate(wealth) - 1).min()
        a, on = act[live], apart[live].any(axis=1)
        rows.append(dict(universe=p.U.key, name=p.U.name, frame=p.frame, sample=sample, rule=name,
                         bars=int(live.sum()), apart=apart[live].mean(), held=np.nanmean(np.where(nh > 0, nh, np.nan)[live]),
                         active=obs * ppy, baseline=c * ppy, edge=(obs - c) * ppy, p=pval,
                         hit=(a[on] > 0).mean() if on.any() else np.nan,
                         vs_bench=np.nanmean(rel[live]) * ppy, turnover=turn, cost=turn * COST,
                         net=(obs - (turn / ppy) * COST) * ppy, drawdown=dd))
    return rows


def event_log(p):
    """One row per shift on the full sample, for anyone who wants to look at the events themselves."""
    F = p.F
    t, i = np.nonzero(p.changed)
    pre = p.before(F["before"])
    out = dict(universe=p.U.key, chart=[p.U.dots[j][0] for j in i], dot=[p.U.dots[j][1] for j in i],
               bench=[p.U.dots[j][3] for j in i], date=p.dates[t],
               move=[f"{QN[a]} → {QN[b]}" for a, b in zip(p.qprev[t, i], p.q[t, i])],
               settings_agreeing=p.agree[t, i].astype(int), settings_evaluated=p.evals[t, i].astype(int),
               bench_above_200d=p.up[t, i], **{f"before_{F['before']}{F['unit']}_vs_siblings": pre[t, i]})
    for h in F["horizons"]:
        out[f"fwd_{h}{F['unit']}_vs_bench"] = p.fwd(h)[t, i]
        out[f"fwd_{h}{F['unit']}_vs_siblings"] = p.siblings(p.fwd(h))[t, i]
    return pd.DataFrame(out)


# ---------------------------------------------------------------- driver

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    d, dates, px = load()
    unis = ai_universes(d, dates, px) + [sector_universe("sectors", "S&P sectors vs SPY", dates, px)]
    long = load_long_sectors()
    if long:
        unis.append(sector_universe("sectors_long", "S&P sectors vs SPY", *long))   # its sample is "since 1999"
    kinds = MOVES + [k for _, k in GROUPS]
    ev, ru, logs, spans = [], [], [], {}
    for U in unis:
        if len(U.dots) < 2:
            print(f"skipping {U.name}: {len(U.dots)} dots", file=sys.stderr)
            continue
        # Daily bars only where the page's default views draw them and a daily panel is cheap:
        # stocks and subsectors on daily bars are mostly whipsaw.
        for frame in (["W", "D"] if U.key in ("tiers", "layers", "sectors", "sectors_long") else ["W"]):
            p = Panel(U, frame)
            last = pd.Timestamp(str(p.dates[-1]))
            samples = [("full", 0)]
            if U.key != "sectors_long":
                samples.insert(0, ("3y", int(np.searchsorted(p.dates, str((last - pd.DateOffset(years=3)).date())))))
            else:
                samples = [("since 1999", 0)]
            for sample, r0 in samples:
                ev += study(p, r0, kinds, sample)
                ru += rules(p, r0, sample)
                spans[(U.key, frame, sample)] = (p.dates[r0], p.dates[-1], len(U.dots))
            if frame == "W":
                logs.append(event_log(p))
            print(f"{U.name}, {FRAMES[frame]['name']}: {len(U.dots)} dots, {int(p.changed.sum())} shifts", file=sys.stderr)
    ev, ru = pd.DataFrame(ev), pd.DataFrame(ru)
    ev.to_csv(OUT / "quadrant_events.csv", index=False)
    ru.to_csv(OUT / "quadrant_rules.csv", index=False)
    pd.concat(logs).round(5).to_csv(OUT / "quadrant_event_log.csv", index=False)
    (OUT / "quadrants.md").write_text(report(ev, ru, spans, unis))
    print((OUT / "quadrants.md").read_text())


# ---------------------------------------------------------------- report

def pct(x, d=1):
    return "–" if x is None or pd.isna(x) else f"{100 * x:+.{d}f}%"


def pv(x):
    return "–" if pd.isna(x) else ("<0.01" if x < 0.01 else f"{x:.2f}")


def cell(r):
    """Edge with its p-value, bold when the placebo puts it beyond SIG."""
    if r is None or pd.isna(r["edge"]):
        return "–"
    s = f"{pct(r['edge'])} ({pv(r['p'])})"
    return f"**{s}**" if r["p"] < SIG else s


def report(ev, ru, spans, unis):
    L = []
    w = L.append
    names = {U.key: U.name for U in unis}
    order = [U.key for U in unis]
    w("# Quadrant shifts backtest\n")
    w("What happened after a dot on the page changed quadrant, for the dots the page draws: the seven tiers and the "
      "layers against QQQ, each layer's subsectors and each basket's stocks against that view's peer benchmark, "
      "and the S&P sectors against SPY. Every dot is rebuilt with the page's own arithmetic (equal-weight baskets, "
      "membership windows, EMA(3) RS, 10-week or 21-day baseline, one-bar momentum).\n")
    w("**How to read it.** *Before* is the dot's return against the other dots on its chart over the bars leading up "
      "to the shift. *Edge* is its return against those siblings over the horizon after the shift, starting from the "
      "close of the next session, minus what the same dots earned when every shift was moved to a random other time "
      "(the placebo); the p-value in brackets is the share of those random timings that did at least as well or as "
      "badly. Bold marks p < 0.05. Hit is the share of shifts after which the dot beat its siblings. The AI baskets "
      "were chosen in 2026 from names that had done well, so their long sample flatters anything that rides "
      "strength; the placebo removes each dot's own drift but not the choice of dots. The sectors since 1999 are the "
      "one sample with no hindsight in it.\n")

    # verdict
    w("## Verdict\n")
    allc = ev[(ev.split == "all")]
    for frame in ["W", "D"]:
        c = allc[(allc.frame == frame) & (allc.events >= MIN_EVENTS) & allc.p.notna()]
        if not len(c):
            continue
        hits = c[c.p < SIG]
        w(f"- **{FRAMES[frame]['name'].capitalize()} bars:** {len(c)} cells (kind of shift × horizon × universe × sample) "
          f"with at least {MIN_EVENTS} events; {len(hits)} beat the placebo at p < {SIG}, against about "
          f"{SIG * len(c):.0f} expected by chance alone"
          + (f" ({int((hits.edge > 0).sum())} positive, {int((hits.edge < 0).sum())} negative)." if len(hits) else "."))
    strong = allc[(allc.p < 0.01) & (allc.events >= MIN_EVENTS)].sort_values("p")
    if len(strong):
        w(f"- Cells at p < 0.01 (expect about {0.01 * len(allc[allc.p.notna()]):.0f} by chance): "
          + "; ".join(f"{r['name']} ({FRAMES[r['frame']]['name']}, {r['sample']}) {r['move']} "
                      f"{r['horizon']}{FRAMES[r['frame']]['unit']}: {pct(r['edge'])}" for _, r in strong.head(12).iterrows())
          + ("; …" if len(strong) > 12 else "") + ".")
    rw = ru[(ru.frame == "W")]
    if len(rw):
        best = rw[rw.p < SIG]
        w(f"- **Trading the quadrants (weekly):** {len(rw)} rule × universe × sample combinations; {len(best)} beat the "
          f"placebo at p < {SIG} (about {SIG * len(rw):.0f} expected by chance)"
          + (": " + "; ".join(f"{r['name']} ({r['sample']}) hold {r['rule']} {pct(r['edge'])}/yr" for _, r in best.iterrows())
             if len(best) else "") + ".")
    w("")

    def span(k, f, s):
        a, b, n = spans[(k, f, s)]
        return f"{n} dots, {a} to {b}"

    # 1 and 2: event studies
    for sec, frame in [(1, "W"), (2, "D")]:
        F = FRAMES[frame]
        sub = allc[allc.frame == frame]
        if not len(sub):
            continue
        u = F["unit"]
        w(f"## {sec}. What followed each kind of shift ({F['name']} bars)\n")
        for k in order:
            for sample in ["3y", "full", "since 1999"]:
                g = sub[(sub.universe == k) & (sub["sample"] == sample)]
                if not len(g):
                    continue
                bn = "QQQ" if k in ("tiers", "layers") else "SPY" if k.startswith("sectors") else "peer"
                w(f"### {names[k]}, {sample} ({span(k, frame, sample)})\n")
                w(f"| Shift | Events | Before {F['before']}{u} | After {F['horizons'][1]}{u} vs {bn} | "
                  + " | ".join(f"Edge {h}{u} (p)" for h in F["horizons"]) + f" | Hit {F['horizons'][-2]}{u} |")
                w("|---|---|---|---|" + "---|" * len(F["horizons"]) + "---|")
                for mv in dict.fromkeys(g.move):
                    gg = g[g.move == mv]
                    n = gg.events.iloc[0]
                    if n < MIN_EVENTS:
                        continue
                    by = {r.horizon: r for _, r in gg.iterrows()}
                    w(f"| {mv} | {n} | {pct(gg.before.iloc[0])} | {pct(by[F['horizons'][1]]['raw'])} | "
                      + " | ".join(cell(by.get(h)) for h in F["horizons"])
                      + f" | {by[F['horizons'][-2]]['hit']:.0%} |")
                w("")

    # 3 and 4: splits
    hz = FRAMES["W"]["horizons"][-2]
    for sec, (title, a, b, note) in enumerate([
            ("Firm vs fragile shifts (the page's agreement badge)", "firm", "fragile",
             "Firm: the new quadrant holds under at least four of the page's five settings, so no badge. Fragile: two or "
             "more of them disagree, which is when the page shows a badge such as 3/5."),
            ("Benchmark trend", "uptrend", "downtrend",
             "Whether the view's benchmark closed above its 200-day average on the signal bar.")], start=3):
        sp = ev[(ev.frame == "W") & ev.split.isin([a, b])]
        if not len(sp):
            continue
        w(f"## {sec}. {title} (weekly, {hz}-week edge)\n")
        w(note + "\n")
        w(f"| Universe | Sample | Shift | {a.capitalize()} events | {a.capitalize()} edge (p) | {b.capitalize()} events | {b.capitalize()} edge (p) |")
        w("|---|---|---|---|---|---|---|")
        for k in order:
            for sample in ["3y", "full", "since 1999"]:
                g = sp[(sp.universe == k) & (sp["sample"] == sample)]
                for mv in dict.fromkeys(g.move):
                    x, y = g[(g.move == mv) & (g.split == a)], g[(g.move == mv) & (g.split == b)]
                    if not len(x) or not len(y):
                        continue
                    w(f"| {names[k]} | {sample} | {mv} | {x.events.iloc[0]} | {cell(x.iloc[0])} | {y.events.iloc[0]} | {cell(y.iloc[0])} |")
        w("")

    # 5: rules
    for frame in ["W", "D"]:
        F = FRAMES[frame]
        g0 = ru[ru.frame == frame]
        if not len(g0):
            continue
        per = "week" if frame == "W" else "session"
        w(f"## {5 if frame == 'W' else 6}. Trading the quadrants ({F['name']} bars)\n")
        w(f"Each {per}, hold equal weights of the dots in the named quadrants, bought at the close of the session after "
          f"the signal; when none qualify, hold every dot on the chart. *Apart* is the share of {per}s the holding "
          f"differed from the whole chart (some dots held, not all). *Active* is the annual return against holding "
          f"every dot on the chart equally (averaged across charts where a universe has several), *Edge* the same "
          f"minus its placebo average, with p. *Ahead* is the share of the {per}s apart that the rule beat the chart. "
          f"*vs bench* is against the chart's benchmark and carries the dots' own drift. Costs are "
          f"{COST * 1e4:.0f}bp one-way on the weights traded.\n")
        for k in order:
            for sample in ["3y", "full", "since 1999"]:
                g = g0[(g0.universe == k) & (g0["sample"] == sample)]
                if not len(g):
                    continue
                w(f"### {names[k]}, {sample}\n")
                w(f"| Hold | Apart | Dots held | Active/yr | Edge/yr (p) | Net of costs/yr | vs bench/yr | Ahead | Worst drawdown vs chart | Turnover/yr |")
                w("|---|---|---|---|---|---|---|---|---|---|")
                for _, r in g.iterrows():
                    e = f"{pct(r.edge)} ({pv(r.p)})"
                    w(f"| {r.rule} | {r.apart:.0%} | {r.held:.1f} | {pct(r.active)} | {f'**{e}**' if r.p < SIG else e} | "
                      f"{pct(r.net)} | {pct(r.vs_bench)} | {r.hit:.0%} | {pct(r.drawdown)} | {r.turnover:.0f}× |")
                w("")

    w("## 7. Multiple testing\n")
    c = allc[allc.p.notna()]
    w(f"{len(c)} event-study cells and {len(ru)} rule tests. At p < {SIG}, chance alone would pass about "
      f"{SIG * len(c):.0f} and {SIG * len(ru):.0f} of them. Cells share events (a 4-week and a 13-week edge "
      "follow the same shifts; the 3-year sample sits inside the full one; Leading and Leading or Improving overlap), "
      "so a run of bold cells in one row is one finding, not several. A result worth acting on would show up across "
      "horizons, in both samples, and in the sectors since 1999.\n")
    return "\n".join(L)


if __name__ == "__main__":
    main()
