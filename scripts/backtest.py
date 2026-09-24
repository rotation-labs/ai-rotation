"""Test whether the RRG's readings predict relative returns, across settings, horizons and universes.

The page ranks and badges groups by RS-Ratio, RS-Momentum and quadrant crossings. A first
check on three years of the AI baskets found that the strength level (the x-axis) separated
future winners from losers by ~6% a quarter but not significantly, and that crossings and
inflections did not predict at all. One setting on one bull-market sample of names picked
in 2026 cannot settle that, so this script re-runs the question on a grid:

  signals     RS-Ratio, RS-Momentum and their inflection under every combination of
              baseline, smoothing and momentum lag; plain relative momentum with and without
              skipping the latest month; volatility-adjusted momentum; quadrant tenure
  horizons    1, 4, 13 and 26 weeks ahead
  universes   the AI stocks (last 3 years only, as first tested, and all available history),
              ~35 sector and industry ETFs since the mid-2000s, and the 11 SPDR sectors
  benchmarks  QQQ, SPY and RSP for the AI stocks; SPY and RSP for the ETFs

Each cell reports the top-minus-bottom-fifth spread in forward relative return and the rank
information coefficient (IC), both with Newey-West t-stats (overlapping holding periods
otherwise overstate significance about sqrt(H) times). A placebo - persistent random
signals run through the same machinery - gives the |t| a real signal has to beat, and the
report counts how many cells clear it against how many chance alone would produce.

Reads build/data.json from scripts/fetch.py (run it with FETCH_PERIOD=10y for the long AI
sample) and downloads the ETF universe itself. Writes build/backtest/report.md and
build/backtest/results.csv.
"""
import json, math, os, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "build" / "data.json"
OUT = ROOT / "build" / "backtest"
HORIZONS = [1, 4, 13, 26]
COST = 0.0015            # one-way trading cost per unit of notional, 15bp
PLACEBOS = 100
RNG = np.random.default_rng(7)

SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
# Industry ETFs with long histories, chosen for coverage of the economy rather than for how
# they have done since. Survivorship still applies at the fund level: closed ETFs are absent.
INDUSTRY = ["SMH", "SOXX", "IGV", "IBB", "XBI", "KRE", "KBE", "XHB", "ITB", "XME", "XOP", "OIH",
            "XRT", "GDX", "IYT", "IHI", "ITA", "TAN", "ICLN", "IYR", "KIE", "IYZ", "FDN", "PHO",
            "XSD", "XPH", "IHF", "PBJ", "XES", "IYM", "SKYY", "HACK"]


# ---------------------------------------------------------------- data

def weekly(daily):
    """Friday closes; the last row is dropped when its week has not finished."""
    w = daily.resample("W-FRI").last()
    if len(daily) and daily.index[-1] < w.index[-1]:
        w = w.iloc[:-1]
    return w


def tickers(node):
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict) and "_tickers" in node:
        return list(node["_tickers"])
    return [t for v in node.values() for t in tickers(v)]


def load_ai():
    """The AI stocks and benchmarks from fetch.py's output, masked to their membership windows."""
    d = json.loads(DATA.read_text())
    idx = pd.to_datetime(d["dates"])
    px = pd.DataFrame({k: pd.Series(v, index=idx, dtype=float) for k, v in d["px"].items()})
    stocks = sorted(set(tickers(d["groups"])) & set(px))
    # A name added to a basket on a date counts only from then; one removed counts only until then.
    for e in d.get("membership", {}).get("entries", []):
        t = e["ticker"]
        if t in px:
            if e.get("from"):
                px.loc[px.index < pd.Timestamp(e["from"]), t] = np.nan
            if e.get("until"):
                px.loc[px.index >= pd.Timestamp(e["until"]), t] = np.nan
    tiers = {k: sorted(set(tickers(v)) & set(stocks)) for k, v in d["groups"].items()}
    return weekly(px), stocks, tiers


def load_etfs():
    import yfinance as yf
    syms = sorted(set(SECTORS + INDUSTRY + ["SPY", "RSP", "QQQ"]))
    frame = pd.DataFrame()
    for attempt in range(3):
        need = [s for s in syms if s not in frame or frame[s].dropna().empty]
        if not need:
            break
        try:
            d = yf.download(need, period="max", interval="1d", progress=False, auto_adjust=True, threads=True)["Close"]
            if isinstance(d, pd.Series):
                d = d.to_frame(need[0])
            d.index = pd.to_datetime([str(i.date()) for i in d.index])
            frame = d if frame.empty else frame.combine_first(d)
        except Exception as e:
            print(f"ETF download attempt {attempt + 1} failed: {e}", file=sys.stderr)
    frame = frame[frame.index >= "1999-01-01"]
    missing = [s for s in syms if s not in frame or frame[s].dropna().empty]
    if missing:
        print(f"warning: no data for {missing}", file=sys.stderr)
    return weekly(frame.dropna(how="all"))


# ---------------------------------------------------------------- signals

def ema(df, n):
    return df if n <= 1 else df.ewm(span=n, adjust=False, ignore_na=True).mean().where(df.notna())


def rrg(rs, base, e, m):
    """The page's RS-Ratio and RS-Momentum (template.html rrg()), on a frame of RS columns."""
    sm = ema(rs, e)
    rsr = 100 * sm / sm.rolling(base).mean()
    roc = 100 * rsr / rsr.shift(m)
    return rsr, ema(roc, e)


def tenure(rsr, rsm):
    """+weeks in Leading, -weeks in Lagging, 0 in the other two quadrants."""
    q = pd.DataFrame(np.select([(rsr > 100) & (rsm > 100), (rsr < 100) & (rsm < 100)], [1, -1], 0),
                     index=rsr.index, columns=rsr.columns).where(rsr.notna() & rsm.notna())
    a = q.to_numpy()
    run = np.full(a.shape, np.nan)
    for i in range(len(a)):
        run[i] = np.where(np.isnan(a[i]), np.nan, 1.0)
        if i:
            same = a[i] == a[i - 1]
            run[i][same] = run[i - 1][same] + 1
    return q * pd.DataFrame(run, index=q.index, columns=q.columns)


def signals(px, bench):
    """name -> (family, params, frame). Every signal is 'higher = expected to outperform'."""
    rs = 100 * px.div(bench, axis=0)
    rel = np.log(rs).diff()
    out = {}
    for base in [5, 10, 20, 40]:
        for e in [1, 3, 5]:
            for m in [1, 4]:
                rsr, rsm = rrg(rs, base, e, m)
                if m == 1:
                    out[f"rs_ratio b{base} e{e}"] = ("RS-Ratio (x-axis)", dict(base=base, ema=e), rsr)
                    out[f"inflection b{base} e{e}"] = ("Inflection (change in y)", dict(base=base, ema=e), rsm.diff())
                out[f"rs_mom b{base} e{e} m{m}"] = ("RS-Momentum (y-axis)", dict(base=base, ema=e, lag=m), rsm)
                if (base, e, m) == (10, 3, 1):
                    out["tenure b10 e3"] = ("Quadrant tenure", dict(base=base, ema=e), tenure(rsr, rsm))
    for L in [4, 12, 26, 52]:
        for S in [0, 4]:
            if S < L:
                out[f"mom {L}w skip{S}"] = ("Relative return", dict(lookback=L, skip=S), rs.shift(S) / rs.shift(L) - 1)
    vol = rel.rolling(26, min_periods=20).std()
    for L in [12, 26]:
        for S in [0, 4]:
            out[f"voladj {L}w skip{S}"] = ("Vol-adjusted return", dict(lookback=L, skip=S),
                                          (np.log(rs.shift(S) / rs.shift(L))) / vol)
    return out


DEFAULTS = ["rs_ratio b10 e3", "mom 12w skip0", "mom 12w skip4", "mom 26w skip4", "mom 52w skip4",
            "voladj 26w skip4", "rs_mom b10 e3 m1", "inflection b10 e3", "tenure b10 e3"]


# ---------------------------------------------------------------- statistics

def nw_t(x, lag):
    """Newey-West t-stat of the mean of a series with overlapping observations."""
    x = pd.Series(x).dropna().to_numpy()
    n = len(x)
    if n < 8:
        return np.nan
    u = x - x.mean()
    s = u @ u / n
    for k in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - k / (lag + 1)) * (u[k:] @ u[:-k]) / n
    return x.mean() / math.sqrt(s / n) if s > 0 else np.nan


def evaluate(sig, fwd, H, min_n):
    """Per-date quintile spread, rank IC and top/bottom membership for one signal."""
    ok = sig.notna() & fwd.notna()
    n = ok.sum(axis=1)
    keep = n >= min_n
    s, f = sig.where(ok)[keep], fwd.where(ok)[keep]
    pr = s.rank(axis=1, pct=True)
    top, bot = pr > 0.8, pr <= 0.2
    spread = f.where(top).mean(axis=1) - f.where(bot).mean(axis=1)
    rs, rf = s.rank(axis=1), f.rank(axis=1)
    rs, rf = rs.sub(rs.mean(axis=1), axis=0), rf.sub(rf.mean(axis=1), axis=0)
    ic = (rs * rf).sum(axis=1) / np.sqrt((rs ** 2).sum(axis=1) * (rf ** 2).sum(axis=1))
    return spread, ic, top, bot


def turnover(mask, H):
    """Average share of a leg replaced at each H-week rebalance."""
    m = mask.iloc[::H]
    prev, cur = m.shift(1, fill_value=False).astype(bool), m.astype(bool)
    new = (cur & ~prev).sum(axis=1)
    size = cur.sum(axis=1).replace(0, np.nan)
    return (new / size).iloc[1:].mean()


def placebo_threshold(template, fwd, H, min_n):
    """95th percentile of |NW t| for persistent random signals (random walks) on the same panel."""
    ts = []
    shape = template.shape
    for _ in range(PLACEBOS):
        walk = pd.DataFrame(RNG.standard_normal(shape).cumsum(axis=0), index=template.index,
                            columns=template.columns).where(template.notna())
        sp, _, _, _ = evaluate(walk, fwd, H, min_n)
        ts.append(abs(nw_t(sp, H)))
    return float(np.nanpercentile(ts, 95))


# ---------------------------------------------------------------- driver

def run_context(name, px, members, bench_name, regime_bench, min_n=10):
    """Evaluate every signal at every horizon for one universe and benchmark."""
    P = px[members].copy()
    b = px[bench_name]
    P = P[b.notna()]
    b = b[b.notna()]
    sigs = signals(P, b)
    lr = np.log(P.div(b, axis=0))
    rb = px[regime_bench].reindex(P.index)
    up = rb > rb.rolling(40).mean()
    rows, series = [], {}
    for H in HORIZONS:
        fwd = lr.shift(-H) - lr
        thr = placebo_threshold(sigs["rs_ratio b10 e3"][2].reindex(fwd.index), fwd, H, min_n)
        for sname, (fam, params, sig) in sigs.items():
            sp, ic, top, bot = evaluate(sig.reindex(fwd.index), fwd, H, min_n)
            sp = sp.dropna()
            if len(sp) < 2 * H + 8:
                continue
            yr = sp.groupby(sp.index.year).mean()
            yr = yr[sp.groupby(sp.index.year).size() >= 13]
            ft, fb = turnover(top, H), turnover(bot, H)
            isup = up.reindex(sp.index).fillna(False).astype(bool)
            u, d = sp[isup], sp[~isup]
            rows.append(dict(context=name, bench=bench_name, horizon=H, signal=sname, family=fam,
                             **{f"p_{k}": v for k, v in params.items()},
                             start=str(sp.index[0].date()), end=str(sp.index[-1].date()), dates=len(sp),
                             spread=sp.mean(), t=nw_t(sp, H), ic=ic.mean(), ic_t=nw_t(ic, H),
                             placebo_t95=thr, yrs=len(yr), yrs_pos=int((yr > 0).sum()),
                             spread_up=u.mean(), t_up=nw_t(u, H), spread_down=d.mean(), t_down=nw_t(d, H),
                             weeks_down=len(d), turn_top=ft, turn_bot=fb,
                             net=sp.mean() - 2 * COST * ((ft or 0) + (fb or 0))))
            if sname in DEFAULTS:
                series[(sname, H)] = sp
    print(f"{name} vs {bench_name}: {len(members)} names, {len(rows)} cells", file=sys.stderr)
    return rows, series, sigs, lr


def transitions(sigs, lr, H, name):
    """Forward return (vs the universe's cross-sectional mean) after each quadrant move."""
    _, _, rsr = sigs["rs_ratio b10 e3"]
    _, _, rsm = sigs["rs_mom b10 e3 m1"]
    Q = {(1, 1): "Leading", (1, 0): "Weakening", (0, 0): "Lagging", (0, 1): "Improving"}
    q = pd.DataFrame(np.select([(rsr > 100) & (rsm > 100), (rsr > 100) & (rsm <= 100),
                                (rsr <= 100) & (rsm <= 100)], ["Leading", "Weakening", "Lagging"], "Improving"),
                     index=rsr.index, columns=rsr.columns).where(rsr.notna() & rsm.notna())
    prev = q.shift(1)
    fwd = lr.shift(-H) - lr
    dm = fwd.sub(fwd.mean(axis=1), axis=0)
    out = []
    for a in Q.values():
        for c in Q.values():
            ev = (prev == a) & (q == c) & dm.notna()
            per = dm.where(ev).mean(axis=1).dropna()
            if ev.values.sum() >= 30:
                out.append(dict(context=name, horizon=H, move=f"{a} → {c}", events=int(ev.values.sum()),
                                excess=per.mean(), t=nw_t(per, H)))
    return out


def pct(x, d=1):
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:+.{d}f}%"


def num(x, d=1):
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{d}f}"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ai, stocks, tiers = load_ai()
    etf = load_etfs()
    ai_first, ai_last = ai.index[0], ai.index[-1]
    cut3 = ai_last - pd.DateOffset(years=3)

    contexts = []   # (name, px, members, bench, regime bench, min_n)
    ai3 = ai[ai.index >= cut3]
    for bn in ["QQQ", "SPY", "RSP"]:
        contexts.append(("AI stocks, 3y only", ai3, stocks, bn, "QQQ", 10))
        contexts.append(("AI stocks, full history", ai, stocks, bn, "QQQ", 10))
    ind = [s for s in SECTORS + INDUSTRY if s in etf]
    sec = [s for s in SECTORS if s in etf]
    for bn in ["SPY", "RSP"]:
        contexts.append(("Sector + industry ETFs", etf, ind, bn, "SPY", 10))
    contexts.append(("11 SPDR sectors", etf, sec, "SPY", "SPY", 8))

    rows, series, trans = [], {}, []
    for name, px, mem, bn, rb, mn in contexts:
        r, s, sigs, lr = run_context(name, px, mem, bn, rb, mn)
        rows += r
        for k, v in s.items():
            series[(name, bn) + k] = v
        if bn in ("QQQ", "SPY") and name != "11 SPDR sectors":
            for H in [4, 13]:
                trans += transitions(sigs, lr, H, name)

    # Tier by tier: does the edge come from one cluster?
    tier_rows = []
    for tier, mem in tiers.items():
        if len(mem) < 15:
            continue
        sigs = signals(ai[mem], ai["QQQ"])
        lr = np.log(ai[mem].div(ai["QQQ"], axis=0))
        for H in [4, 13]:
            fwd = lr.shift(-H) - lr
            for sname in ["rs_ratio b10 e3", "mom 12w skip4", "rs_mom b10 e3 m1", "inflection b10 e3"]:
                sp, ic, _, _ = evaluate(sigs[sname][2], fwd, H, 10)
                tier_rows.append(dict(tier=tier, names=len(mem), horizon=H, signal=sname,
                                      spread=sp.mean(), t=nw_t(sp, H), ic=ic.mean(), ic_t=nw_t(ic, H)))

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "results.csv", index=False)
    pd.DataFrame(trans).to_csv(OUT / "transitions.csv", index=False)
    pd.DataFrame(tier_rows).to_csv(OUT / "tiers.csv", index=False)
    (OUT / "report.md").write_text(report(res, series, pd.DataFrame(trans), pd.DataFrame(tier_rows),
                                          ai_first, ai_last, len(stocks), ind, etf))
    print((OUT / "report.md").read_text())


# ---------------------------------------------------------------- report

def report(res, series, trans, tiers, ai_first, ai_last, n_ai, ind, etf):
    L = []
    w = L.append
    w("# RRG signal backtest\n")
    w(f"AI stocks: {n_ai} names, weekly, {ai_first.date()} to {ai_last.date()} (names chosen in 2026, "
      f"so the long sample is survivorship-biased). ETFs: {len(ind)} sector and industry funds from "
      f"{etf.index[0].date()} to {etf.index[-1].date()}.\n")
    w("Spread = mean forward relative log return, top fifth minus bottom fifth, per holding period. "
      "t = Newey-West t-stat (lag = horizon). IC = mean weekly rank correlation of signal with forward return. "
      "Placebo t95 = the |t| that 95% of persistent random signals stay below on the same panel.\n")

    main = res[res.bench.isin(["QQQ", "SPY"]) | ((res.context == "11 SPDR sectors") & (res.bench == "SPY"))]
    main = main[~((main.context.str.startswith("AI")) & (main.bench == "SPY"))]

    w("## 1. Headline signals at the page's default settings\n")
    for ctx, g in main.groupby("context", sort=False):
        bn = g.bench.iloc[0]
        w(f"### {ctx} (vs {bn}, {g.start.min()} to {g.end.max()})\n")
        hdr = "| Signal | " + " | ".join(f"{H}w spread (t)" for H in HORIZONS) + " | 13w IC (t) | 13w years + |"
        w(hdr)
        w("|" + "---|" * (len(HORIZONS) + 3))
        thr = " | ".join(f"{g[g.horizon == H].placebo_t95.iloc[0]:.1f}" if len(g[g.horizon == H]) else "–" for H in HORIZONS)
        for s in DEFAULTS:
            cells = []
            for H in HORIZONS:
                r = g[(g.signal == s) & (g.horizon == H)]
                cells.append(f"{pct(r.spread.iloc[0])} ({num(r.t.iloc[0])})" if len(r) else "–")
            r13 = g[(g.signal == s) & (g.horizon == 13)]
            ic = f"{r13.ic.iloc[0]:+.3f} ({num(r13.ic_t.iloc[0])})" if len(r13) else "–"
            yp = f"{r13.yrs_pos.iloc[0]}/{r13.yrs.iloc[0]}" if len(r13) else "–"
            w(f"| {s} | " + " | ".join(cells) + f" | {ic} | {yp} |")
        w(f"| *placebo t95* | {thr} | | |\n")

    w("## 2. Robustness across the settings grid\n")
    w("Share of settings with a positive spread, median t, and the range of t, per signal family. "
      "A real effect should be positive across most of its family, not in one cell.\n")
    w("| Universe | Family | Horizon | Settings | Positive | Median t | t range | Cells beyond placebo t95 |")
    w("|---|---|---|---|---|---|---|---|")
    for (ctx, fam, H), g in main.groupby(["context", "family", "horizon"], sort=False):
        beyond = int((g.t.abs() > g.placebo_t95).sum())
        w(f"| {ctx} | {fam} | {H}w | {len(g)} | {(g.spread > 0).mean():.0%} | {g.t.median():+.1f} | "
          f"{g.t.min():+.1f} to {g.t.max():+.1f} | {beyond} |")
    w("")

    w("## 3. RS-Ratio: baseline × smoothing (13-week spread, t)\n")
    for ctx, g in main[(main.family == "RS-Ratio (x-axis)") & (main.horizon == 13)].groupby("context", sort=False):
        w(f"**{ctx}**\n")
        w("| Baseline \\ EMA | 1 | 3 | 5 |\n|---|---|---|---|")
        for base in [5, 10, 20, 40]:
            cells = []
            for e in [1, 3, 5]:
                r = g[(g.p_base == base) & (g.p_ema == e)]
                cells.append(f"{pct(r.spread.iloc[0])} ({num(r.t.iloc[0])})" if len(r) else "–")
            w(f"| {base}w | " + " | ".join(cells) + " |")
        w("")

    w("## 4. Relative return: lookback × skip (spread, t)\n")
    for ctx, g in main[main.family == "Relative return"].groupby("context", sort=False):
        w(f"**{ctx}**\n")
        w("| Lookback | Skip | " + " | ".join(f"{H}w" for H in HORIZONS) + " |")
        w("|---|---|" + "---|" * len(HORIZONS))
        for (Lb, S), gg in g.groupby(["p_lookback", "p_skip"]):
            cells = []
            for H in HORIZONS:
                r = gg[gg.horizon == H]
                cells.append(f"{pct(r.spread.iloc[0])} ({num(r.t.iloc[0])})" if len(r) else "–")
            w(f"| {int(Lb)}w | {int(S)}w | " + " | ".join(cells) + " |")
        w("")

    w("## 5. Benchmark sensitivity (13-week spread, t)\n")
    sub = res[(res.horizon == 13) & res.signal.isin(DEFAULTS[:6])]
    benches = list(dict.fromkeys(sub.bench))
    w("| Universe | Signal | " + " | ".join(benches) + " |")
    w("|---|---|" + "---|" * len(benches))
    for (ctx, s), g in sub.groupby(["context", "signal"], sort=False):
        cells = []
        for bn in benches:
            r = g[g.bench == bn]
            cells.append(f"{pct(r.spread.iloc[0])} ({num(r.t.iloc[0])})" if len(r) else "")
        w(f"| {ctx} | {s} | " + " | ".join(cells) + " |")
    w("")

    w("## 6. Year by year (13-week spread, default settings)\n")
    for ctx in main.context.unique():
        bn = main[main.context == ctx].bench.iloc[0]
        cols = [s for s in ["rs_ratio b10 e3", "mom 12w skip4", "mom 52w skip4", "rs_mom b10 e3 m1"]
                if (ctx, bn, s, 13) in series]
        if not cols:
            continue
        yrs = sorted({y for s in cols for y in series[(ctx, bn, s, 13)].index.year})
        w(f"**{ctx}**\n")
        w("| Year | " + " | ".join(cols) + " |")
        w("|---|" + "---|" * len(cols))
        for y in yrs:
            cells = []
            for s in cols:
                sp = series[(ctx, bn, s, 13)]
                v = sp[sp.index.year == y]
                cells.append(pct(v.mean()) if len(v) >= 13 else "–")
            w(f"| {y} | " + " | ".join(cells) + " |")
        w("")

    w("## 7. Market regime (13-week spread; benchmark above or below its 40-week average)\n")
    w("| Universe | Signal | Uptrend | Downtrend | Downtrend weeks |\n|---|---|---|---|---|")
    for _, r in main[(main.horizon == 13) & main.signal.isin(DEFAULTS[:7])].iterrows():
        w(f"| {r.context} | {r.signal} | {pct(r.spread_up)} ({num(r.t_up)}) | "
          f"{pct(r.spread_down)} ({num(r.t_down)}) | {r.weeks_down} |")
    w("")

    w(f"## 8. Turnover and costs ({COST * 1e4:.0f}bp one-way, rebalanced every holding period)\n")
    w("| Universe | Signal | Horizon | Leg turnover | Gross | Net |\n|---|---|---|---|---|---|")
    for _, r in main[main.horizon.isin([4, 13]) & main.signal.isin(DEFAULTS[:6])].iterrows():
        w(f"| {r.context} | {r.signal} | {r.horizon}w | {(r.turn_top + r.turn_bot) / 2:.0%} | "
          f"{pct(r.spread)} | {pct(r.net)} |")
    w("")

    if len(trans):
        w("## 9. Quadrant moves (forward return vs the universe average)\n")
        w("Events cluster in time and overlap, so these t-stats are optimistic; judge them against the "
          "placebo t95 in section 1 (typically 2 to 3), not against 2.\n")
        w("| Universe | Move | Events | 4w excess (t) | 13w excess (t) |\n|---|---|---|---|---|")
        for (ctx, mv), g in trans.groupby(["context", "move"], sort=False):
            a, b = g[g.horizon == 4], g[g.horizon == 13]
            w(f"| {ctx} | {mv} | {a.events.iloc[0] if len(a) else '–'} | "
              f"{pct(a.excess.iloc[0]) if len(a) else '–'} ({num(a.t.iloc[0]) if len(a) else '–'}) | "
              f"{pct(b.excess.iloc[0]) if len(b) else '–'} ({num(b.t.iloc[0]) if len(b) else '–'}) |")
        w("")

    if len(tiers):
        w("## 10. Within each AI tier (vs QQQ, full history)\n")
        w("| Tier | Names | Signal | 4w spread (t) | 13w spread (t) | 13w IC |\n|---|---|---|---|---|---|")
        for (tier, s), g in tiers.groupby(["tier", "signal"], sort=False):
            a, b = g[g.horizon == 4].iloc[0], g[g.horizon == 13].iloc[0]
            w(f"| {tier} | {a.names} | {s} | {pct(a.spread)} ({num(a.t)}) | {pct(b.spread)} ({num(b.t)}) | {b.ic:+.3f} |")
        w("")

    w("## 11. Multiple testing\n")
    n = len(res)
    beyond = int((res.t.abs() > res.placebo_t95).sum())
    w(f"{n} cells in total. {beyond} exceed their placebo t95; chance alone would put about {round(0.05 * n)} "
      f"there. {int((res.t > 2).sum())} have t > 2 and {int((res.t < -2).sum())} have t < -2. "
      "Neighbouring cells share data, so they are far from independent: a cluster of significant cells in one "
      "family is one finding, not many.\n")
    return "\n".join(L)


if __name__ == "__main__":
    main()
