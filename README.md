# AI Rotation Graph

A relative rotation graph (RRG) of the AI trade: 11 stack layers vs SPY, drilling into semiconductor and AI-ecosystem subsectors and their stocks. The site is a single static page that rebuilds itself every weekday after the US close.

## How it works

```
GitHub Actions (weekdays 21:30 UTC)
  scripts/fetch.py   -> downloads ~3y of daily closes for every ticker in config/baskets.json
                        (foreign listings converted to USD) -> build/data.json
  scripts/build.py   -> injects the data into template.html -> site/index.html
  deploy-pages       -> publishes site/ to GitHub Pages
```

All RRG math (baskets, RS-Ratio, RS-Momentum, quadrants) runs in the viewer's browser, so the page needs no server.
If a fetch fails (Yahoo rate limit, a benchmark missing, or more than 10% of tickers missing) the job stops and the previous version stays live.

## Deployment

The site is deployed from this repository to GitHub Pages at
**https://rotation-labs.github.io/ai-rotation/**

Pages is configured with **Settings → Pages → Source: GitHub Actions**. The
`Update AI Rotation Graph` workflow builds and deploys on three triggers: the
weekday cron, any push to `main`, and the manual **Run workflow** button.

To stand this up somewhere else: create a public repo, push these files
(including the hidden `.github` folder), set Pages source to GitHub Actions,
then run the workflow once from the Actions tab.

## Everyday changes

- **Edit baskets:** change `config/baskets.json` (families → baskets → tickers) and commit. Any push to `main` triggers a rebuild.
  Non-US tickers get a short name in `foreign_listings` mapped to their Yahoo symbol (e.g. `"HYNIX": "000660.KS"`). Supported suffixes: `.KS .T .TW .TWO .SZ .SS .HK .AS .DE .PA`.
- **Change the schedule:** edit the `cron` line in `.github/workflows/update.yml` (times are UTC).
- **Refresh right now:** Actions → Update AI Rotation Graph → Run workflow.
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
