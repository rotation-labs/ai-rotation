"""Search for a signal in the page's own universes, with a protocol meant to survive the search.

scripts/quadrants.py found that the page's quadrant shifts do not predict, and hinted at two
things that might: tiers and layers right of centre kept beating the rest, and stocks that had
lagged their basket recovered against it. Hints from one look at the data are how false
discoveries start, so this script tests a fixed list of candidates, chosen for an economic
prior or for one of those hints before any of them was run, under rules set in advance:

  candidates  relative strength over several lookbacks (with and without skipping the latest
              month), RS-Ratio and RS-Momentum, last week's and last month's move (reversal),
              distance from the 52-week high, trend against a 40-week average, low volatility,
              breadth, the parent layer's strength (for stocks), and three combinations
  universes   the page's tiers and layers against QQQ, subsectors and stocks within their
              view, all AI stocks against QQQ, ~40 sector and industry ETFs and the 11 SPDR
              sectors against SPY since 1999
  portfolios  cross-sectional: within each chart, rank-weighted long-short ($1 a side), held
              1, 4 or 13 weeks as staggered portfolios; time-series: long the dot against its
              benchmark when the signal is positive, short when negative, scored only on the
              timing (each dot's average position times its average return is removed). Trades
              at the close of the session after the signal, 15bp one-way costs.
  protocol    discovery on the AI universes to the end of 2022, with the Benjamini-Hochberg
              false-discovery rate held at 10% across every discovery test; holdout from 2023
              for whatever survives, one-sided in the discovered direction with Holm's
              correction; then the ETF universes, which no candidate was chosen from. A
              walk-forward run re-picks the best candidate every January from trailing data
              only, which prices in the search itself.

The AI baskets were picked in 2026 from names that had done well, which flatters anything
that buys strength in the AI samples - the reason for the ETF check.

Reads build/data.json (FETCH_PERIOD=10y) and downloads the ETFs' full history (SIG_ETF=0 skips
it). Writes build/backtest/signals.md and signals.csv.
"""
import os, sys, warnings
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quadrants as Q
from backtest import SECTORS, INDUSTRY, nw_t

OUT = Q.OUT
COST = Q.COST
SPLIT = "2023-01-01"         # discovery before, holdout from
FDR = 0.10
HOLDS = [1, 4, 13]
PPY = 52
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Each signal is oriented so that higher means expected to outperform under its usual prior.
CS = {   # name: (family, description)
    "strength": ("strength", "relative return from 26 to 4 weeks ago (the page's Strength)"),
    "mom12_1": ("strength", "relative return from 52 to 4 weeks ago"),
    "mom3": ("strength", "relative return over the last 13 weeks"),
    "x": ("strength", "RS-Ratio, the chart's x-axis"),
    "high52": ("strength", "relative strength against its 52-week high"),
    "trend40": ("strength", "relative strength against its 40-week average"),
    "y": ("turn", "RS-Momentum, the chart's y-axis"),
    "dx4": ("turn", "change in RS-Ratio over 4 weeks"),
    "rev1": ("reversal", "minus last week's relative return"),
    "rev4": ("reversal", "minus the last 4 weeks' relative return"),
    "lowvol": ("risk", "minus the volatility of weekly relative returns over 26 weeks"),
    "breadth": ("breadth", "share of members right of centre against the same benchmark"),
    "dbreadth": ("breadth", "change in that share over 4 weeks"),
    "parent": ("parent", "the stock's layer's Strength against QQQ"),
    "pullback": ("combo", "rank of Strength plus rank of last week's reversal"),
    "fading": ("combo", "rank of RS-Ratio minus rank of RS-Momentum (strong, momentum easing)"),
    "parent_rev": ("combo", "rank of the layer's Strength plus rank of the stock's reversal"),
}
TS = {   # time-series rules: the sign of each says long or short the dot against its benchmark
    "x": "right of centre (RS-Ratio above 100)", "y": "momentum rising (RS-Momentum above 100)",
    "strength": "Strength positive", "mom12_1": "12-1 month return positive",
    "mom3": "13-week return positive", "trend40": "above its 40-week average",
}


# ---------------------------------------------------------------- panels

class Panel:
    """Weekly signals and next-session-to-next-session relative returns for one universe."""
    def __init__(self, U, members=None, parent=None):
        self.U = U
        bars = Q.bars_of(U.dates, "W")
        self.dates = U.dates[bars]
        P = np.column_stack([d[2] for d in U.dots])
        B = np.column_stack([U.px[d[3]] for d in U.dots])
        rs = 100 * P[bars] / B[bars]
        ex = bars + 1
        ok = ex < len(U.dates)
        ex = np.where(ok, ex, len(U.dates) - 1)
        Pe, Be = P[ex], B[ex]
        Pe[~ok], Be[~ok] = np.nan, np.nan
        rel = (Pe / Be)
        self.R = Q.lag(rel, -1) / rel - 1              # relative return, this execution to the next
        lr = np.log(rs)
        L = lambda k: Q.lag(lr, k)
        x, y = Q.rrg(rs, 10)
        d = np.vstack([np.full((1, lr.shape[1]), np.nan), np.diff(lr, axis=0)])
        S = dict(strength=L(4) - L(26), mom12_1=L(4) - L(52), mom3=lr - L(13), x=x, y=y,
                 high52=lr - pd.DataFrame(lr).rolling(52, min_periods=40).max().to_numpy(),
                 trend40=lr - pd.DataFrame(lr).rolling(40, min_periods=30).mean().to_numpy(),
                 dx4=x - Q.lag(x, 4), rev1=-(lr - L(1)), rev4=-(lr - L(4)),
                 lowvol=-pd.DataFrame(d).rolling(26, min_periods=20).std().to_numpy())
        if members is not None:
            S["breadth"] = members
            S["dbreadth"] = members - Q.lag(members, 4)
        if parent is not None:
            S["parent"] = parent
        charts = [d[0] for d in U.dots]
        self.starts = np.flatnonzero([i == 0 or charts[i] != charts[i - 1] for i in range(len(charts))])
        self.sizes = np.diff(np.r_[self.starts, len(charts)])
        S["pullback"] = self.rank(S["strength"]) + self.rank(S["rev1"])
        S["fading"] = self.rank(x) - self.rank(y)
        if parent is not None:
            S["parent_rev"] = self.rank(parent) + self.rank(S["rev1"])
        self.S = S

    def charts(self):
        return [slice(a, a + n) for a, n in zip(self.starts, self.sizes)]

    def rank(self, X):
        """Percentile rank within each chart, per bar."""
        out = np.full(X.shape, np.nan)
        for c in self.charts():
            out[:, c] = pd.DataFrame(X[:, c]).rank(axis=1, pct=True).to_numpy()
        return out


def breadth(U, member_lists):
    """Share of each dot's members that are right of centre against the dot's own benchmark."""
    bars = Q.bars_of(U.dates, "W")
    cache, out = {}, []
    for (chart, label, s, bench), mem in zip(U.dots, member_lists):
        cols = []
        for t in mem:
            if t not in U.px or t == bench:
                continue
            if (t, bench) not in cache:
                cache[(t, bench)] = Q.rrg((100 * U.px[t][bars] / U.px[bench][bars])[:, None], 10)[0][:, 0]
            cols.append(cache[(t, bench)])
        if not cols:
            out.append(np.full(len(bars), np.nan))
            continue
        X = np.column_stack(cols)
        ok = ~np.isnan(X)
        with np.errstate(invalid="ignore"):
            out.append(np.where(ok.sum(1) > 0, (np.where(ok, X > 100, False)).sum(1) / ok.sum(1), np.nan))
    return np.column_stack(out)


# ---------------------------------------------------------------- portfolios

def cs_returns(p, S, H):
    """Weekly return and two-way turnover of a rank-weighted long-short ($1 a side) within
    each chart, held H weeks as H staggered portfolios, averaged across charts."""
    T, N = S.shape
    W = np.zeros((T, N))
    for c in p.charts():
        r = pd.DataFrame(S[:, c]).rank(axis=1).to_numpy()
        k = (~np.isnan(r)).sum(1, keepdims=True)
        dm = np.where(np.isnan(r), 0, r - (k + 1) / 2)
        g = np.abs(dm).sum(1, keepdims=True)
        W[:, c] = np.where((k >= 2) & (g > 0), 2 * dm / np.where(g > 0, g, 1), 0)
    if H > 1:
        W = pd.DataFrame(W).rolling(H, min_periods=1).mean().to_numpy()
    R = np.nan_to_num(p.R)
    live = ~np.isnan(p.R)
    rets, turns = [], []
    for c in p.charts():
        w = W[:, c]
        on = (np.abs(w).sum(1) > 0) & live[:, c].any(axis=1)
        rets.append(np.where(on, (w * R[:, c]).sum(1), np.nan))
        turns.append(np.where(on, np.abs(np.diff(np.vstack([np.zeros((1, w.shape[1])), w]), axis=0)).sum(1), np.nan))
    return np.nanmean(np.column_stack(rets), axis=1), np.nanmean(np.column_stack(turns), axis=1)


def ts_returns(p, S, mask):
    """Timing return of going long the dot against its benchmark when S > 0 and short when
    S < 0, with each dot's average position times its average return taken out, so drift
    that any always-long position would earn does not count. Averaged across dots."""
    pos = np.sign(S)
    R = p.R
    ok = ~np.isnan(pos) & ~np.isnan(R) & mask[:, None]
    P = np.where(ok, pos, np.nan)
    Rr = np.where(ok, R, np.nan)
    out = (P - np.nanmean(P, axis=0)) * (Rr - np.nanmean(Rr, axis=0))
    turn = np.abs(np.diff(np.nan_to_num(np.where(ok, pos, 0)), axis=0, prepend=0)).sum(1) / np.maximum(ok.sum(1), 1)
    return np.nanmean(out, axis=1), turn


def stats(r, turn, mask, H):
    """Annualised mean, Newey-West t, Sharpe and annualised net of costs over mask."""
    m = mask & ~np.isnan(r)
    x = r[m]
    if len(x) < 52:
        return dict(n=len(x), ann=np.nan, t=np.nan, sharpe=np.nan, net=np.nan, turn=np.nan)
    tn = np.nanmean(turn[m]) * PPY
    return dict(n=len(x), ann=x.mean() * PPY, t=nw_t(x, max(H, 2)), sharpe=x.mean() / x.std() * np.sqrt(PPY),
                net=x.mean() * PPY - tn * COST, turn=tn)


def pval(t):
    from math import erf, sqrt
    return 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))) if np.isfinite(t) else np.nan


def bh(p, q):
    """Benjamini-Hochberg: which of p pass at false-discovery rate q."""
    p = np.asarray(p, float)
    ok = np.isfinite(p)
    out = np.zeros(len(p), bool)
    if not ok.any():
        return out
    idx = np.flatnonzero(ok)
    order = idx[np.argsort(p[idx])]
    m = len(order)
    passed = np.flatnonzero(p[order] <= q * np.arange(1, m + 1) / m)
    if len(passed):
        out[order[:passed.max() + 1]] = True
    return out


def walk_forward(p, names, H, start_year):
    """Each January from start_year, hold the candidate whose trailing (expanding) Sharpe was
    best, for the year. Returns the out-of-sample weekly series and the picks."""
    series = {n: cs_returns(p, p.S[n], H) for n in names}
    years = pd.to_datetime(p.dates).year
    out = np.full(len(p.dates), np.nan)
    picks = []
    for y in range(start_year, years.max() + 1):
        past, now = years < y, years == y
        best, score = None, -np.inf
        for n, (r, _) in series.items():
            x = r[past & ~np.isnan(r)]
            if len(x) >= 52 and x.std() > 0 and x.mean() / x.std() > score:
                best, score = n, x.mean() / x.std()
        if best is None:
            continue
        r, tn = series[best]
        out[now] = r[now] - COST * tn[now]
        picks.append((y, best))
    avg = np.nanmean(np.column_stack([r - COST * tn for r, tn in series.values()]), axis=1)
    return out, avg, picks


# ---------------------------------------------------------------- universes

def build():
    d, dates, px = Q.load()
    tiers, layers = Q.tree(d)
    unis = {U.key: U for U in Q.ai_universes(d, dates, px)}
    # Members of each dot, for breadth; windows are ignored here (only SANM has one, from 2026-09).
    deep = lambda node: list(dict.fromkeys(t for b in node["leaves"] for t in b["members"]))
    mem = {"tiers": [deep(t) for t in tiers], "layers": [deep(x) for x in layers],
           "subs": [s["members"] for x in layers for s in x["subs"]]}
    # Every AI stock once, against QQQ, with its first layer's Strength as 'parent'.
    first = {}
    for i, x in enumerate(layers):
        for t in deep(x):
            first.setdefault(t, i)
    allstocks = Q.Universe("stocks_qqq", "AI stocks vs QQQ", dates, px,
                           [("All", t, px.get(t), "QQQ") for t in sorted(first) if t != "QQQ"], True)
    unis["stocks_qqq"] = allstocks
    out = {}
    for key in ["tiers", "layers", "subs", "stocks", "stocks_qqq"]:
        U = unis[key]
        keep = None
        if key in mem:   # Universe drops dots without data; keep member lists aligned with it
            labels = {(c, l) for c, l, _, _ in U.dots}
            src = {"tiers": [("Full stack", t["label"]) for t in tiers],
                   "layers": [("Full stack, layers", x["label"]) for x in layers],
                   "subs": [(x["label"], s["label"]) for x in layers for s in x["subs"]]}[key]
            keep = [m for k, m in zip(src, mem[key]) if k in labels]
        out[key] = (U, keep)
    lay = Panel(unis["layers"])
    par = None
    if len(lay.S["strength"]):
        names = [l for _, l, _, _ in unis["layers"].dots]
        par = np.column_stack([lay.S["strength"][:, names.index(layers[first[t]]["label"])]
                               if layers[first[t]]["label"] in names else np.full(len(lay.dates), np.nan)
                               for _, t, _, _ in allstocks.dots])
    panels = {}
    for key, (U, keep) in out.items():
        panels[key] = Panel(U, breadth(U, keep) if keep is not None else None, par if key == "stocks_qqq" else None)
        print(f"{U.name}: {len(U.dots)} dots, {len(panels[key].dates)} weeks", file=sys.stderr)
    if os.environ.get("SIG_ETF", "1") != "0":
        long = Q.download_max(SECTORS + INDUSTRY)
        if long:
            ld, lp = long
            etf = [s for s in SECTORS + INDUSTRY if s in lp]
            panels["etfs"] = Panel(Q.Universe("etfs", "Sector and industry ETFs vs SPY", ld, lp,
                                              [("ETFs", s, lp[s], "SPY") for s in etf], False))
            panels["spdr"] = Panel(Q.Universe("spdr", "SPDR sectors vs SPY", ld, lp,
                                              [("Sectors", s, lp[s], "SPY") for s in SECTORS if s in lp], False))
            for k in ("etfs", "spdr"):
                print(f"{panels[k].U.name}: {len(panels[k].U.dots)} dots, {len(panels[k].dates)} weeks", file=sys.stderr)
    return panels


AI = ["tiers", "layers", "subs", "stocks", "stocks_qqq"]
ETF = ["etfs", "spdr"]


# ---------------------------------------------------------------- driver

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    panels = build()
    rows = []
    for key, p in panels.items():
        disc = p.dates < SPLIT
        hold = ~disc
        full = np.ones(len(p.dates), bool)
        for name in CS:
            if name not in p.S:
                continue
            for H in HOLDS:
                r, tn = cs_returns(p, p.S[name], H)
                for sample, m in [("discovery", disc), ("holdout", hold), ("full", full)]:
                    rows.append(dict(universe=key, kind="cs", signal=name, hold=H, sample=sample, **stats(r, tn, m, H)))
        if key in ("tiers", "layers", "etfs", "spdr"):
            for name in TS:
                S = p.S[name] - (100 if name in ("x", "y") else 0)
                for sample, m in [("discovery", disc), ("holdout", hold), ("full", full)]:
                    r, tn = ts_returns(p, S, m)
                    rows.append(dict(universe=key, kind="ts", signal=name, hold=1, sample=sample, **stats(r, tn, m, 1)))
    res = pd.DataFrame(rows)
    res["p"] = res.t.map(pval)

    # Discovery: AI universes before 2023, every test at once.
    D = res[(res.universe.isin(AI)) & (res["sample"] == "discovery")].copy()
    D["found"] = bh(D.p.to_numpy(), FDR)
    found = D[D.found]
    # Holdout: same universe, signal, hold and kind, one-sided in the discovered direction, Holm.
    H = res[res["sample"] == "holdout"].set_index(["universe", "kind", "signal", "hold"])
    checks = []
    for _, r in found.iterrows():
        k = (r.universe, r.kind, r.signal, r.hold)
        h = H.loc[k] if k in H.index else None
        t1 = np.sign(r.t) * h.t if h is not None else np.nan
        checks.append(dict(universe=r.universe, kind=r.kind, signal=r.signal, hold=r.hold, d_ann=r.ann, d_t=r.t,
                           h_ann=h.ann if h is not None else np.nan, h_t=h.t if h is not None else np.nan,
                           h_net=h.net if h is not None else np.nan, p1=(pval(t1) / 2 if t1 > 0 else 1 - pval(t1) / 2)
                           if np.isfinite(t1) else np.nan))
    C = pd.DataFrame(checks)
    if len(C):
        order = np.argsort(C.p1.fillna(1).to_numpy())
        m = len(C)
        holm = np.zeros(m, bool)
        for j, i in enumerate(order):
            if C.p1.iloc[i] <= 0.05 / (m - j):
                holm[i] = True
            else:
                break
        C["holds"] = holm

    # Walk-forward selection over the cross-sectional candidates, 4-week hold.
    wf = []
    for key, p in panels.items():
        names = [n for n in CS if n in p.S]
        start = int(p.dates[0][:4]) + 3
        r, avg, picks = walk_forward(p, names, 4, start)
        m = ~np.isnan(r)
        for lab, x in [("walk-forward pick", r), ("average candidate", avg)]:
            s = stats(x, np.zeros(len(x)), m, 4)
            wf.append(dict(universe=key, series=lab, start=start, picks=", ".join(f"{y} {n}" for y, n in picks)
                           if lab == "walk-forward pick" else "", **s))
    WF = pd.DataFrame(wf)
    res.to_csv(OUT / "signals.csv", index=False)
    (OUT / "signals.md").write_text(report(res, D, C, WF, panels))
    print((OUT / "signals.md").read_text())


# ---------------------------------------------------------------- report

def pct(x):
    return "–" if pd.isna(x) else f"{100 * x:+.1f}%"


def tt(x):
    return "–" if pd.isna(x) else f"{x:+.1f}"


def report(res, D, C, WF, panels):
    L = []
    w = L.append
    names = {k: p.U.name for k, p in panels.items()}
    span = {k: f"{p.dates[0]} to {p.dates[-1]}" for k, p in panels.items()}
    w("# Signal search\n")
    w("A fixed list of candidate signals, tested on the page's universes with a discovery period, a holdout and an "
      "independent check, so that what survives has survived the search. Returns are long-short within each chart "
      "($1 a side, rank-weighted) or, for timing rules, long or short each dot against its benchmark with drift "
      "removed; trades at the close of the session after the signal; annualised; t-stats are Newey-West. "
      f"Discovery runs to {SPLIT} and controls the false-discovery rate at {FDR:.0%} across "
      f"{len(D)} tests; holdout runs from {SPLIT}.\n")
    w("Universes: " + "; ".join(f"{names[k]} ({len(p.U.dots)} dots, {span[k]})" for k, p in panels.items()) + ".\n")

    w("## 1. What survived\n")
    if not len(C):
        w(f"Nothing passed discovery at a {FDR:.0%} false-discovery rate.\n")
    else:
        w(f"{len(C)} tests passed discovery; {int(C.holds.sum())} of them held up in the holdout (one-sided, Holm-"
          "corrected at 5%).\n")
        w("| Universe | Kind | Signal | Hold | Discovery ann. (t) | Holdout ann. (t) | Holdout net | Holds |")
        w("|---|---|---|---|---|---|---|---|")
        for _, r in C.sort_values(["holds", "d_t"], ascending=[False, False]).iterrows():
            w(f"| {names[r.universe]} | {'long-short' if r.kind == 'cs' else 'timing'} | {r.signal} | {r.hold}w | "
              f"{pct(r.d_ann)} ({tt(r.d_t)}) | {pct(r.h_ann)} ({tt(r.h_t)}) | {pct(r.h_net)} | {'yes' if r.holds else 'no'} |")
        w("")

    w("## 2. The independent check: ETFs (full history, before and from 2023)\n")
    w("The same candidates on universes none of them was chosen from. A signal worth trusting in the AI baskets "
      "should at least point the same way here.\n")
    E = res[res.universe.isin(ETF) & (res.kind == "cs") & (res.hold == 4)]
    T = res[res.universe.isin(ETF) & (res.kind == "ts")]
    for key in [k for k in ETF if k in panels]:
        w(f"### {names[key]}\n")
        w("| Signal | Kind | Full ann. (t) | Net | Before 2023 (t) | From 2023 (t) |\n|---|---|---|---|---|---|")
        for kind, G in [("long-short 4w", E[E.universe == key]), ("timing", T[T.universe == key])]:
            for s in dict.fromkeys(G.signal):
                g = G[G.signal == s].set_index("sample")
                f, a, b = g.loc["full"], g.loc["discovery"], g.loc["holdout"]
                w(f"| {s} | {kind} | {pct(f.ann)} ({tt(f.t)}) | {pct(f.net)} | {pct(a.ann)} ({tt(a.t)}) | {pct(b.ann)} ({tt(b.t)}) |")
        w("")

    w("## 3. Every candidate in the AI universes (4-week hold for long-short)\n")
    w("Discovery to 2022 and holdout from 2023, annualised (t). Bold: passed discovery.\n")
    found = set(map(tuple, D[D.found][["universe", "kind", "signal", "hold"]].to_numpy()))
    for key in [k for k in AI if k in panels]:
        G = res[(res.universe == key) & (((res.kind == "cs") & (res.hold == 4)) | (res.kind == "ts"))]
        w(f"### {names[key]}\n")
        w("| Signal | Kind | Discovery | Holdout | Full net | Turnover/yr |\n|---|---|---|---|---|---|")
        for (kind, s), g in G.groupby(["kind", "signal"], sort=False):
            g = g.set_index("sample")
            a, b, f = g.loc["discovery"], g.loc["holdout"], g.loc["full"]
            cell = f"{pct(a.ann)} ({tt(a.t)})"
            if (key, kind, s, 4 if kind == "cs" else 1) in found:
                cell = f"**{cell}**"
            w(f"| {s} | {'long-short' if kind == 'cs' else 'timing'} | {cell} | {pct(b.ann)} ({tt(b.t)}) | "
              f"{pct(f.net)} | {f.turn:.0f}× |")
        w("")

    w("## 4. Holding period (long-short, full sample)\n")
    w("Annualised return net of costs, with the t-stat of the gross return in brackets: a short hold can carry a "
      "real gross edge that costs then erase.\n")
    G = res[(res.kind == "cs") & (res["sample"] == "full")]
    w("| Universe | Signal | " + " | ".join(f"{h}w" for h in HOLDS) + " |\n|---|---|" + "---|" * len(HOLDS))
    for key in panels:
        for s in [x for x in ["strength", "x", "rev1", "pullback", "parent", "parent_rev", "lowvol"] if x in set(G[G.universe == key].signal)]:
            g = G[(G.universe == key) & (G.signal == s)].set_index("hold")
            w(f"| {names[key]} | {s} | " + " | ".join(f"{pct(g.loc[h].net)} ({tt(g.loc[h].t)})" for h in HOLDS) + " |")
    w("")

    w("## 5. Walk-forward: what picking the best candidate each January would have earned\n")
    w("Each January the candidate with the best trailing Sharpe (all data up to then) is held for the year, 4-week "
      "hold, net of costs. This prices in the search: if it does no better than the average candidate, picking the "
      "past winner is not a signal.\n")
    w("| Universe | From | Picked each year | Ann. net (t) | Sharpe | Average candidate ann. net (t) |\n|---|---|---|---|---|---|")
    for key in panels:
        g = WF[WF.universe == key].set_index("series")
        if "walk-forward pick" not in g.index:
            continue
        a, b = g.loc["walk-forward pick"], g.loc["average candidate"]
        w(f"| {names[key]} | {a.start} | {a.picks} | {pct(a.ann)} ({tt(a.t)}) | {a.sharpe:+.2f} | {pct(b.ann)} ({tt(b.t)}) |")
    w("")

    w("## 6. Candidates\n")
    for k, (fam, desc) in CS.items():
        w(f"- **{k}** ({fam}): {desc}.")
    w("\nTiming rules use the sign of: " + "; ".join(f"**{k}** ({v})" for k, v in TS.items()) + ".\n")
    return "\n".join(L)


if __name__ == "__main__":
    main()
