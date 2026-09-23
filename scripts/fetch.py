"""Download daily closes for every ticker in config/baskets.json and write build/data.json.

Foreign listings are converted to USD and forward-filled onto the US trading calendar.
Exits non-zero (so the scheduled job fails and the last good site stays live) if the
benchmark data is missing or too many tickers fail.
"""
import json, math, sys, time
from pathlib import Path
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "baskets.json").read_text())
OUT = ROOT / "build" / "data.json"
PERIOD = "3y"
MAX_MISSING_SHARE = 0.10
STALE_SESSIONS = 5   # drop a listing whose last print is more than this many US sessions old

# suffix -> (FX ticker, how to convert). "div": price / fx (fx quoted per USD); "mul": price * fx (USD per unit)
FX = {".KS": ("KRW=X", "div"), ".T": ("JPY=X", "div"), ".TW": ("TWD=X", "div"), ".TWO": ("TWD=X", "div"),
      ".SZ": ("CNY=X", "div"), ".SS": ("CNY=X", "div"), ".HK": ("HKD=X", "div"),
      ".AS": ("EURUSD=X", "mul"), ".DE": ("EURUSD=X", "mul"), ".PA": ("EURUSD=X", "mul")}


def sigfig(v, n=5):
    """Round to significant figures, not decimal places.

    Prices are only ever consumed as ratios, so absolute precision is wasted, but a flat
    2dp would quantise a sub-dollar listing badly: NANYA trades near $0.77, where half a
    cent is 0.65% - bigger than plenty of daily moves. Five significant figures caps the
    relative error at 0.005% for every series and still cuts the gzipped payload ~16%.
    """
    if v == 0:
        return 0.0
    return round(v, -int(math.floor(math.log10(abs(v)))) + (n - 1))


def download(symbols, tries=3):
    """Batch download closes, retrying missing symbols individually."""
    frame = pd.DataFrame()
    for attempt in range(tries):
        need = [s for s in symbols if s not in frame or frame[s].dropna().empty]
        if not need:
            break
        try:
            d = yf.download(need, period=PERIOD, interval="1d", progress=False, auto_adjust=True, threads=True)["Close"]
            if isinstance(d, pd.Series):
                d = d.to_frame(need[0])
            d.index = pd.to_datetime([str(i.date()) for i in d.index])
            frame = d if frame.empty else frame.combine_first(d)
        except Exception as e:  # network hiccup / rate limit
            print(f"download attempt {attempt + 1} failed: {e}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    return frame


def fill_unfinalised(frame, lookback=3):
    """Fill empty daily closes on recent US sessions from that session's intraday bars.

    Yahoo can leave the latest daily candle empty for hours after the close - observed
    still NaN at 04:00 UTC the next morning - while the session's intraday bars are
    already published, so a scheduled run shortly after the close would drop a whole
    session. The last intraday bar can miss the closing auction by a few basis points;
    every run refetches the full history, so the finalised close replaces it next time.
    Only US listings are filled: a suffix-less symbol trades on New York hours, so its
    intraday bars map to a session date unambiguously.
    """
    if frame.empty:
        return frame
    dates = frame.index[-lookback:]
    need = sorted({s for d in dates for s in frame.columns
                   if pd.isna(frame.at[d, s]) and "." not in s and "=" not in s})
    if not need:
        return frame
    try:
        h = yf.download(need, period="5d", interval="60m", progress=False, auto_adjust=True, threads=True)["Close"]
    except Exception as e:
        print(f"intraday fill skipped: {e}", file=sys.stderr)
        return frame
    if isinstance(h, pd.Series):
        h = h.to_frame(need[0])
    if h.empty:
        return frame
    local = h.index.tz_convert("America/New_York") if h.index.tz is not None else h.index
    day = pd.to_datetime([str(i.date()) for i in local])
    filled = 0
    for s in need:
        if s not in h:
            continue
        col = pd.Series(h[s].to_numpy(), index=day).dropna()
        for d in dates:
            if pd.isna(frame.at[d, s]):
                hit = col[col.index == d]
                if len(hit):
                    frame.at[d, s] = float(hit.iloc[-1])
                    filled += 1
    if filled:
        print(f"filled {filled} unfinalised daily close(s) from intraday bars", file=sys.stderr)
    return frame


def main():
    groups = CFG["groups"]
    foreign = CFG["foreign_listings"]
    names = sorted(set(CFG["benchmarks"]) | {t for g in groups.values() for f in g.values() for m in f.values() for t in m})
    sym = {n: foreign.get(n, n) for n in names}
    fx_syms = sorted({FX[k][0] for k in FX if any(s.endswith(k) for s in sym.values())})

    raw = fill_unfinalised(download(list(sym.values()) + fx_syms))

    # Yahoo serves a live, still-moving value for the session in progress. Stored as-is
    # it becomes a "close" that never happened, so drop the current New York session
    # until 16:15 local. This is anchored to New York rather than a fixed UTC hour: the
    # close is 20:00 UTC in summer but 21:00 UTC in winter.
    ny = pd.Timestamp.now(tz="America/New_York")
    today = pd.Timestamp(ny.date())
    cutoff = today if ny.time() < pd.Timestamp("16:15").time() else today + pd.Timedelta(days=1)
    before = len(raw)
    raw = raw[raw.index < cutoff]
    if len(raw) < before:
        print(f"dropped {before - len(raw)} in-progress or future session row(s)", file=sys.stderr)

    if "SPY" not in raw or raw["SPY"].dropna().empty:
        sys.exit("SPY data missing - aborting so the previous site stays up")
    us = raw["SPY"].dropna().index

    px, missing, stale = {}, [], []
    for n, s in sym.items():
        p = raw[s].dropna() if s in raw else pd.Series(dtype=float)
        if len(p) < 60:
            missing.append(n)
            continue
        # A halted, delisted or silently broken listing would otherwise be forward-filled
        # flat forever, contributing 0% every day and dragging its basket toward the mean.
        if len(us) > STALE_SESSIONS and p.index[-1] < us[-STALE_SESSIONS - 1]:
            stale.append(n)
            missing.append(n)
            continue
        suf = next((k for k in FX if s.endswith(k)), None)
        if suf:
            fx_t, how = FX[suf]
            fx = raw[fx_t].ffill().reindex(p.index).ffill()
            p = p / fx if how == "div" else p * fx
        first = p.index[0]
        q = p.reindex(p.index.union(us)).ffill().reindex(us)
        q[q.index < first] = float("nan")
        px[n] = [None if pd.isna(v) else sigfig(float(v)) for v in q]

    for b in CFG["benchmarks"]:
        if b not in px:
            sys.exit(f"benchmark {b} missing - aborting")
    if len(missing) > MAX_MISSING_SHARE * len(names):
        sys.exit(f"too many tickers missing ({len(missing)}/{len(names)}): {missing}")
    if stale:
        print(f"warning: dropped as stale (no print in {STALE_SESSIONS} sessions): {stale}", file=sys.stderr)
    if missing:
        print(f"warning: dropped {missing}", file=sys.stderr)

    for g in groups.values():
        for fam in g.values():
            for k in list(fam):
                fam[k] = [t for t in fam[k] if t not in missing]
                if not fam[k]:
                    del fam[k]

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"asof": str(us[-1].date()), "dates": [str(x.date()) for x in us],
                               "px": px, "groups": groups,
                               "names": {k: v for k, v in CFG.get("names", {}).items() if k in px}},
                              separators=(",", ":")))
    print(f"wrote {OUT} - {len(px)} series through {us[-1].date()}")


if __name__ == "__main__":
    main()
