"""Download daily closes for every ticker in config/baskets.json and write build/data.json.

Foreign listings are converted to USD and forward-filled onto the US trading calendar.
Exits non-zero (so the scheduled job fails and the last good site stays live) if the
benchmark data is missing or too many tickers fail.
"""
import json, sys, time
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


def main():
    groups = CFG["groups"]
    foreign = CFG["foreign_listings"]
    names = sorted(set(CFG["benchmarks"]) | {t for g in groups.values() for f in g.values() for m in f.values() for t in m})
    sym = {n: foreign.get(n, n) for n in names}
    fx_syms = sorted({FX[k][0] for k in FX if any(s.endswith(k) for s in sym.values())})

    raw = download(list(sym.values()) + fx_syms)

    # Yahoo serves a live, still-moving value for the session in progress. Stored as-is
    # it becomes a "close" that never happened, so drop the current day unless the US
    # cash close (20:00 UTC) has actually passed.
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    if now < now.normalize() + pd.Timedelta(hours=20, minutes=15):
        before = len(raw)
        raw = raw[raw.index < now.normalize()]
        if len(raw) < before:
            print(f"dropped {before - len(raw)} in-progress session(s) before the US close", file=sys.stderr)

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
        px[n] = [None if pd.isna(v) else round(float(v), 4) for v in q]

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
                               "px": px, "groups": groups}, separators=(",", ":")))
    print(f"wrote {OUT} - {len(px)} series through {us[-1].date()}")


if __name__ == "__main__":
    main()
