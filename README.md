# Rotation

A relative rotation graph (RRG) of the AI trade in seven tiers - Core Semis, AI Compute, Networking & Optics, Power & Buildout, Platforms & Software, Physical AI, Bio AI - drilling into layers, subsectors and their stocks, plus two reference views: SPY's eleven sector ETFs, and a macro bucket (NQ, RTY, US 10Y yield, crude, VIX, gold, copper, the dollar and bitcoin against ES). The site is a single static page that rebuilds itself every weekday after the US close.

## How it works

```
GitHub Actions (weekdays 21:30 UTC)
  scripts/fetch.py   -> downloads ~3y of daily closes for every ticker in config/baskets.json
                        (foreign listings converted to USD) -> build/data.json
  scripts/build.py   -> injects the data into template.html -> site/index.html
  deploy-pages       -> publishes site/ to GitHub Pages
```

All RRG math (baskets, RS-Ratio, RS-Momentum, quadrants) runs in the viewer's browser, so the page needs no server. `index.html` embeds the most recent 320 sessions, enough for every view at the latest date; the full three years sit in `history.json` on the same site and load the first time replay, Play or Compare is used.

Reading the page: the default Full stack view compares seven tiers against QQQ; opening a layer switches to its industry peer benchmark, and the Benchmark check under the lists shows each call against SPY, QQQ, RSP, SOXX and the peer. Rows show breadth (the share of members themselves outperforming) and are sorted by strength, the relative return from about six months to one month ago, which the backtest below supported better than the chart's 10-bar reading; the **Strength** tab ranks every dot on it, and the line above the chart says whether the benchmark is above its 40-week average, the condition under which that ranking held. The strip above the chart lists recent quadrant changes, flagged as having no predictive record, and **Compare** plots any two groups against each other. View state lives in the URL hash, so any view can be linked and the back button works. `/` focuses the search box. Both axes are scaled to standard deviations and the window is square, so equal distances mean the same thing either way. **Scale +/−** above the chart (or the `+`, `-` and `0` keys, or a trackpad pinch) zooms up to 8× so small moves show when one name sets the scale; a zoomed window follows the bulk of the current dots, and any dot beyond the edge is pinned to it with an arrow. A badge such as `2/5` marks a quadrant that only holds under the smoothing settings shown — the page re-runs every node under four alternatives. The current, unfinished week is drawn hollow and never earns a NEW badge.
The job runs after the US close and also every 30 minutes through the US session. During the session today's bar is kept as a live, provisional bar - drawn hollow, labelled with the build time, never given a NEW badge - and replaced by the official close after 16:15 New York time. An open page polls `version.json` and offers a reload when a newer build lands. Yahoo's quotes can lag about 15 minutes and GitHub's scheduler can run late, so live data is typically 20-45 minutes behind.

The build parses the page script with Node before publishing, so a syntax error fails the build rather than blanking the site.

If a fetch fails (Yahoo rate limit, a benchmark missing, or more than 10% of tickers missing) the job stops and the previous version stays live.

## Backtest

`scripts/backtest.py` checks whether the chart's readings (RS-Ratio, RS-Momentum, their inflection, quadrant moves) and plain relative momentum predict forward relative returns. It runs every signal over a grid of settings, horizons (1-26 weeks), benchmarks and universes: the AI stocks over the last three years and over ten, about 40 sector and industry ETFs since 2000, and the 11 SPDR sectors. Spreads and ICs carry Newey-West t-stats, judged against a placebo of random signals run through the same code. Run it from **Actions → Backtest → Run workflow**; it also runs on any push that changes it. The report appears in the run summary and as the `backtest` artifact. Locally:

```
FETCH_PERIOD=10y python scripts/fetch.py && python scripts/backtest.py   # writes build/backtest/
```

## Deployment

The site is deployed from this repository to GitHub Pages at
**https://rotation-labs.github.io/ai-rotation/**

Pages is configured with **Settings → Pages → Source: GitHub Actions**. The
`Update Rotation` workflow builds and deploys on three triggers: the
weekday cron, any push to `main`, and the manual **Run workflow** button.

To stand this up somewhere else: create a public repo, push these files
(including the hidden `.github` folder), set Pages source to GitHub Actions,
then run the workflow once from the Actions tab.

## Everyday changes

- **Edit baskets:** change `config/baskets.json` (tier → layer → subsector → tickers) and commit. Any push to `main` triggers a rebuild. Give every new ticker a display name in `names`; it is shown on hover.
  Non-US tickers get a short name in `foreign_listings` mapped to their Yahoo symbol (e.g. `"HYNIX": "000660.KS"`), and a display name in `names` (e.g. `"HYNIX": "SK Hynix"`) so the chart is readable without the glossary. Supported suffixes: `.KS .KQ .T .TW .TWO .SZ .SS .HK .AS .DE .PA .SW .L` (London is quoted in pence and converted accordingly).
- **Macro bucket:** `macro` maps a short code to its Yahoo symbol and chart label (e.g. `"CL": {"symbol": "CL=F", "label": "Crude oil"}`); give each code a name in `names` too. The view is measured against `ES`, so ES itself is the yardstick rather than a dot. Futures are Yahoo's front-month continuous contracts, so a roll shows as a small jump, and `^TNX`/`^VIX` are levels, so the 10Y dot moving right means yields rising faster than the S&P.
- **Peer benchmarks:** `peer_benchmarks` maps a layer name to the ETF its members are measured against when that layer is opened (Accelerators → SOXX, software → IGV, biotech → XBI). Tier views always use QQQ. Any ETF used must be in `benchmarks` or `sectors`.
- **Start dates:** `starts` discards a ticker's history before a date, for symbols whose history predates the business they now name (CCXI traded as an empty SPAC until the Agility deal was announced).
- **Membership changes:** to make replay show a basket as it was, add an entry to `membership_changes.entries`: `{"ticker", "basket": "Layer / Subsector" (or "Layer"), "from", "until"}`. Adding a name on a date is a `from` entry; removing one is an `until` entry kept after the name leaves `groups`; a move is one of each. Don't record renames or classification fixes.
- **Fonts:** `assets/fonts/*.woff2` are vendored and copied into `site/` at build time, so the page makes no third-party request. See `assets/fonts/NOTICE.md`.
- **Change the schedule:** edit the `cron` line in `.github/workflows/update.yml` (times are UTC).
- **Refresh right now:** Actions → Update Rotation → Run workflow.
- **Custom domain:** Settings → Pages → Custom domain.

## Run it locally

```
pip install -r requirements.txt
python scripts/fetch.py && python scripts/build.py
open site/index.html
```

## Notes

- GitHub disables scheduled workflows in a public repo after 60 days with no repository activity. Any commit (or re-enabling the workflow in the Actions tab) restarts them.
- Data comes from Yahoo Finance through the `yfinance` library, which is intended for personal use. Before promoting the site widely, consider a provider whose terms allow public display (e.g. Tiingo, Polygon, EODHD). Only `download()` in `scripts/fetch.py` would need to change.
- The page is for information only and is not investment advice.
