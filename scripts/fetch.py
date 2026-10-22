"""Download daily closes for every ticker in config/baskets.json and write build/data.json.

Foreign listings are converted to USD and forward-filled onto the US trading calendar.
Exits non-zero (so the scheduled job fails and the last good site stays live) if the
benchmark data is missing or too many tickers fail.
"""
import json, math, os, sys, time
from pathlib import Path
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "baskets.json").read_text())
OUT = ROOT / "build" / "data.json"
PERIOD = os.environ.get("FETCH_PERIOD", "3y")   # the backtest asks for longer
MAX_MISSING_SHARE = 0.10
STALE_SESSIONS = 5   # drop a listing whose last print is more than this many US sessions old

# suffix -> (FX ticker, how to convert). "div": price / fx (fx quoted per USD); "mul": price * fx (USD per unit);
# "pence": price / 100 * fx, for venues that quote in minor units
FX = {".KS": ("KRW=X", "div"), ".T": ("JPY=X", "div"), ".TW": ("TWD=X", "div"), ".TWO": ("TWD=X", "div"),
      ".SZ": ("CNY=X", "div"), ".SS": ("CNY=X", "div"), ".HK": ("HKD=X", "div"), ".KQ": ("KRW=X", "div"), ".SW": ("CHF=X", "div"),
      # London listings are quoted in pence (GBp), not pounds: ONT.L at 172.20 is GBP 1.72
      ".L": ("GBPUSD=X", "pence"),
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


EXCHANGE_TZ = {".T": "Asia/Tokyo", ".KS": "Asia/Seoul", ".KQ": "Asia/Seoul", ".TW": "Asia/Taipei",
               ".TWO": "Asia/Taipei", ".HK": "Asia/Hong_Kong", ".SS": "Asia/Shanghai", ".SZ": "Asia/Shanghai",
               ".AS": "Europe/Amsterdam", ".DE": "Europe/Berlin", ".PA": "Europe/Paris",
               ".L": "Europe/London", ".SW": "Europe/Zurich"}


def tickers(node):
    """Every ticker under a config node. A layer maps subsectors to lists, or - when it
    has no natural sub-grouping - is itself a list of tickers. Either can instead be
    {"_tickers": [...], "_weights": {...}} where members are not equal-weighted."""
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict) and "_tickers" in node:
        return list(node["_tickers"])
    return [t for v in node.values() for t in tickers(v)]


def prune(node, drop):
    """Remove dropped tickers at any depth and delete whatever ends up empty."""
    if isinstance(node, list):
        return [t for t in node if t not in drop]
    if isinstance(node, dict) and "_tickers" in node:
        node["_tickers"] = [t for t in node["_tickers"] if t not in drop]
        node["_weights"] = {k: w for k, w in node.get("_weights", {}).items() if k not in drop}
        return node if node["_tickers"] else None
    for k in list(node):
        node[k] = prune(node[k], drop)
        if not node[k]:
            del node[k]
    return node


def exchange_tz(symbol):
    """Local time zone of a listing's exchange; suffix-less symbols trade in New York."""
    suf = max((k for k in EXCHANGE_TZ if symbol.endswith(k)), key=len, default=None)
    return EXCHANGE_TZ[suf] if suf else "America/New_York"


PROXY = {}   # symbol -> dates whose close came from an intraday bar, not the official close


def fill_unfinalised(frame, lookback=3):
    """Fill empty daily closes on recent US sessions from that session's intraday bars.

    Yahoo can leave the latest daily candle empty for hours after the close - observed
    still NaN at 04:00 UTC the next morning - while the session's intraday bars are
    already published, so a scheduled run shortly after the close would drop a whole
    session. The last intraday bar can miss the closing auction by a few basis points;
    every run refetches the full history, so the finalised close replaces it next time.
    The gap is not US-only: Paris, Frankfurt and London closes were equally blank, so every
    equity listing is filled, with its intraday bars dated in its own exchange's time zone.
    Tokyo's morning is still the previous evening in New York, so a single New York clock
    would put Asian bars on the wrong session. FX pairs trade round the clock and are skipped.
    """
    if frame.empty:
        return frame
    dates = frame.index[-lookback:]
    need = sorted({s for d in dates for s in frame.columns
                   if pd.isna(frame.at[d, s]) and "=" not in s})
    if not need:
        return frame
    # One big intraday batch silently came back empty for ~17 of 200+ symbols that
    # certainly traded, so fetch in chunks and retry whatever is still missing.
    got, pending = {}, list(need)
    for attempt in range(3):
        for i in range(0, len(pending), 40):
            chunk = pending[i:i + 40]
            try:
                h = yf.download(chunk, period="5d", interval="60m", progress=False, auto_adjust=True, threads=True)["Close"]
            except Exception as e:
                print(f"intraday chunk failed: {e}", file=sys.stderr)
                continue
            if isinstance(h, pd.Series):
                h = h.to_frame(chunk[0])
            for s in chunk:
                if s in h and h[s].notna().any():
                    col = h[s].dropna()
                    local = col.index.tz_convert(exchange_tz(s)) if col.index.tz is not None else col.index
                    got[s] = pd.Series(col.to_numpy(), index=pd.to_datetime([str(i.date()) for i in local]))
        pending = [s for s in pending if s not in got]
        if not pending:
            break
        time.sleep(3 * (attempt + 1))
    filled = 0
    for s, col in got.items():
        for d in dates:
            if pd.isna(frame.at[d, s]):
                hit = col[col.index == d]
                if len(hit):
                    frame.at[d, s] = float(hit.iloc[-1])
                    PROXY.setdefault(s, set()).add(d)
                    filled += 1
    if filled:
        print(f"filled {filled} unfinalised daily close(s) from intraday bars", file=sys.stderr)
    if pending:
        print(f"warning: no intraday bars for {pending}; their last close carries forward", file=sys.stderr)
    return frame


# Yahoo reports SPY's sector mix in Morningstar's sector names; each maps to one SPDR sector fund.
SECTOR_FUND = {"technology": "XLK", "communication_services": "XLC", "consumer_cyclical": "XLY",
               "financial_services": "XLF", "healthcare": "XLV", "industrials": "XLI", "energy": "XLE",
               "basic_materials": "XLB", "utilities": "XLU", "realestate": "XLRE", "consumer_defensive": "XLP"}


def fund_weights(fallback):
    """SPY's sector weights and top holdings, for the Drivers panel's contribution maths.

    Refreshed from Yahoo on every build. That endpoint is less dependable than prices, so on
    any failure or implausible answer the copy in config/baskets.json is used instead; the page
    drifts either one by price from its as-of date, so a stale copy degrades slowly rather than
    breaking. Never fatal: the panel is an extra, not a reason to keep the previous site up.
    """
    try:
        fd = yf.Ticker("SPY").funds_data
        sectors = {SECTOR_FUND[k]: round(float(v), 4) for k, v in fd.sector_weightings.items() if k in SECTOR_FUND}
        top = {}
        for s, v in fd.top_holdings["Holding Percent"].items():
            s = "GOOGL" if s == "GOOG" else s.replace(".", "-")   # one Alphabet; BRK.B is BRK-B on Yahoo
            top[s] = round(top.get(s, 0) + float(v), 4)
        if len(sectors) != len(SECTOR_FUND) or not 0.95 < sum(sectors.values()) < 1.05 or not top:
            raise ValueError(f"implausible weights: {sectors} {top}")
        return {"asof": None, "source": "Yahoo Finance (Morningstar sectors) for SPY", "sectors": sectors, "top": top}
    except Exception as e:
        print(f"warning: SPY weights unavailable ({e}); using the copy dated {fallback.get('asof')}", file=sys.stderr)
        return dict(fallback)


def main():
    groups = CFG["groups"]
    # Further ecosystems (Bio x AI) share the tree shape of groups and every rule that goes with it.
    stacks = {k: v for k, v in CFG.get("stacks", {}).items() if not k.startswith("_")}
    foreign = CFG["foreign_listings"]
    changes = {k: v for k, v in CFG.get("membership_changes", {}).items() if not k.startswith("_")}
    historical = [e["ticker"] for e in changes.get("entries", [])]   # may no longer be in groups
    # Macro series (futures, a yield, VIX, the dollar, bitcoin) go by short display codes
    # mapped to their Yahoo symbols, the same way foreign listings do.
    macro = CFG.get("macro", {})
    # The Drivers panel's extra series: headline and factor ETFs, rates, credit and volatility
    # gauges (a code with a "symbol" is a Yahoo series named by that code, as in macro), and
    # SPY's largest holdings, whose weights decide which stocks it needs.
    drv = CFG.get("drivers", {})
    weights = fund_weights(drv.get("weights", {})) if drv else None
    dsym = {b["code"]: b["symbol"] for b in drv.get("barometers", []) if "symbol" in b} | drv.get("extra", {})
    dtk = set(drv.get("indices", [])) | {t for s in drv.get("styles", []) for k in "ab"
                                          for t in ([s[k]] if isinstance(s[k], str) else s[k])}
    dtk |= {t for b in drv.get("barometers", []) for t in b.get("ratio", [])} | set((weights or {}).get("top", {}))
    names = sorted(set(CFG["benchmarks"]) | set(CFG.get("sectors", {})) | set(macro)
                   | set(tickers(groups)) | {t for v in stacks.values() for t in tickers(v["groups"])}
                   | set(historical) | set(dsym) | dtk)
    sym = {n: macro[n]["symbol"] if n in macro else dsym.get(n) or foreign.get(n, n) for n in names}
    fx_syms = sorted({FX[k][0] for k in FX if any(s.endswith(k) for s in sym.values())})

    raw = fill_unfinalised(download(list(sym.values()) + fx_syms))

    # The New York clock decides what today's row means. Before the open it is a stale or
    # empty placeholder and is dropped. During the session the build now runs every 30
    # minutes, so the row is kept as a *live* bar - still moving, and flagged so the page
    # draws it as provisional rather than passing it off as a close. From 16:15 it is a
    # close. Anchored to New York, not UTC: the close is 20:00 UTC in summer, 21:00 in winter.
    ny = pd.Timestamp.now(tz="America/New_York")
    today = pd.Timestamp(ny.date())
    in_session = pd.Timestamp("09:30").time() <= ny.time() < pd.Timestamp("16:15").time()
    cutoff = today if ny.time() < pd.Timestamp("09:30").time() else today + pd.Timedelta(days=1)
    before = len(raw)
    raw = raw[raw.index < cutoff]
    if len(raw) < before:
        print(f"dropped {before - len(raw)} in-progress or future session row(s)", file=sys.stderr)

    if "SPY" not in raw or raw["SPY"].dropna().empty:
        sys.exit("SPY data missing - aborting so the previous site stays up")
    us = raw["SPY"].dropna().index

    # A symbol can carry history that predates the business it now names: CCXI traded as an
    # empty ~$10 SPAC shell until the Agility Robotics deal was announced on 2026-06-24.
    starts = {k: pd.Timestamp(v) for k, v in CFG.get("starts", {}).items()}

    px, missing, stale = {}, [], []
    for n, s in sym.items():
        p = raw[s].dropna() if s in raw else pd.Series(dtype=float)
        if n in starts:
            p = p[p.index >= starts[n]]
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
            p = p / fx if how == "div" else p / 100 * fx if how == "pence" else p * fx
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

    prune(groups, set(missing))
    for v in stacks.values():
        prune(v["groups"], set(missing))

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"asof": str(us[-1].date()), "dates": [str(x.date()) for x in us],
                               "px": px, "groups": groups,
                               "stacks": {k: {x: y for x, y in v.items() if not x.startswith("_")} for k, v in stacks.items()},
                               "names": {k: v for k, v in CFG.get("names", {}).items() if k in px},
                               # ad-hoc display codes (HYNIX, FANUC) rather than real tickers:
                               # the page decodes these inline, and shows every other name on hover
                               "adhoc": sorted(k for k in foreign if k in px),
                               "sectors": {k: v for k, v in CFG.get("sectors", {}).items() if k in px},
                               "macro": {k: v["label"] for k, v in macro.items() if k in px},
                               "benchmarks": [b for b in CFG["benchmarks"] if b in px],
                               "peers": CFG.get("peer_benchmarks", {}),
                               "membership": changes,
                               "drivers": drivers_payload(drv, weights, px, us),
                               # set only while the US session is in progress and today has trades
                               "live": ({"date": str(today.date()),
                                         "time": ny.strftime("%I:%M%p").lstrip("0").lower() + " ET"}
                                        if in_session and us[-1] == today else None),
                               # closes still standing in for an official close, by display name
                               "proxy": {n: sorted(str(d.date()) for d in PROXY[s] if d in us)
                                         for n, s in sym.items() if s in PROXY and n in px
                                         and any(d in us for d in PROXY[s])}},
                              separators=(",", ":")))
    print(f"wrote {OUT} - {len(px)} series through {us[-1].date()}")


def drivers_payload(drv, weights, px, us):
    """The Drivers config as the page needs it: only what has prices, weights dated."""
    if not drv:
        return None
    have = lambda t: all(x in px for x in ([t] if isinstance(t, str) else t))
    baro = [b for b in drv.get("barometers", [])
            if have(b.get("ratio") or b.get("spread") or b["code"])]
    w = dict(weights)
    w["asof"] = w.get("asof") or str(us[-1].date())
    w["sectors"] = {k: v for k, v in w.get("sectors", {}).items() if k in px}
    w["top"] = {k: v for k, v in w.get("top", {}).items() if k in px}
    return {"indices": [t for t in drv.get("indices", []) if t in px],
            "styles": [s for s in drv.get("styles", []) if have(s["a"]) and have(s["b"])],
            "barometers": [{k: v for k, v in b.items() if k != "symbol"} for b in baro],
            "model": [m for m in drv.get("model", []) if any(b["code"] == m for b in baro)],
            "weights": w}


if __name__ == "__main__":
    main()
