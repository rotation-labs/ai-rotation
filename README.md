# Rotation

A relative rotation graph (RRG) of the AI trade in seven tiers - Core Semis, AI Compute, Networking & Optics, Power & Buildout, Platforms & Software, Physical AI, Bio AI - drilling into layers, subsectors and their stocks, plus a reference view of SPY's eleven sector ETFs. The site is a single static page that rebuilds itself every weekday after the US close.

## How it works

```
GitHub Actions (weekdays 21:30 UTC)
  scripts/fetch.py   -> downloads ~3y of daily closes for every ticker in config/baskets.json
                        (foreign listings converted to USD) -> build/data.json
  scripts/build.py   -> injects the data into template.html -> site/index.html
  deploy-pages       -> publishes site/ to GitHub Pages
```

All RRG math (baskets, RS-Ratio, RS-Momentum, quadrants) runs in the viewer's browser, so the page needs no server.

Reading the page: view state lives in the URL hash, so any view can be linked and the back button works. `/` focuses the search box. Both axes are scaled to standard deviations and the window is square, so equal distances mean the same thing either way. A badge such as `2/5` marks a quadrant that only holds under the smoothing settings shown — the page re-runs every node under four alternatives. The current, unfinished week is drawn hollow and never earns a NEW badge.
If a fetch fails (Yahoo rate limit, a benchmark missing, or more than 10% of tickers missing) the job stops and the previous version stays live.

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
