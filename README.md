# Abnormal Movers

Hourly screen of abnormal share-price moves across the S&P 500, NASDAQ (>= US$1bn), ASX (~300), Nikkei 225 and KOSPI (~220),
with the cap stack (net debt, cash, EBITDA, EV) and catalysts behind each flagged name. Built for the IREN capital-markets desk.

This is the GitHub Actions + GitHub Pages edition of the claude.ai dashboard of the same name (migrated 8 Nov 2026).

## How it works

| Piece | What it does |
|---|---|
| `screen.py` | The screen. Standard library only. `run` builds the universe, quotes it, applies the tier rules and writes JSON into `docs/data/`. `merge-notes` folds one-line notes back in. |
| `notes.py` | Asks Claude (Anthropic API) for a one-line "why did it move" note per flagged name, then calls `screen.py merge-notes`. Skipped when the `ANTHROPIC_API_KEY` secret is not set. |
| `docs/index.html` | The page. Reads `docs/data/*.json` and re-checks for new data every 15 minutes. |
| `docs/data/` | `movers.json`, `markets.json`, `sparks.json` (the screen output) and `notes.json` (the notes cache, 7-day TTL). |
| `.github/workflows/refresh.yml` | Runs hourly on weekdays. Pulls the last published data from the site first (so the notes cache persists and a failed run keeps the previous page), runs the screen, publishes `docs/` to Pages. |

## Rules (agreed 27 Oct 2026)

- Stage filter: trailing revenue >= US$50m; pre-revenue names are set aside behind a toggle.
- Tier 1: market cap >= US$10bn and |z| >= 3, where z is today's log return against the name's own 200-session return distribution.
- Tier 2: |5-session move| >= 30%. Tier 3: |day move| >= 10%.
- Peers (IREN, CIFR, CORZ, APLD, WULF, HUT, RIOT, MARA, CLSK, BTDR, NBIS, CRWV, GLXY) always shown in their own strip.

Constants live at the top of `screen.py`.

## Set-up (once)

1. Create a GitHub repository and push this folder to its `main` branch.
2. Settings > Pages > Source: **GitHub Actions**.
3. Optional: Settings > Secrets and variables > Actions > New repository secret `ANTHROPIC_API_KEY` for the one-line notes.
   Without it everything else still runs; the note column just stays empty.
4. Actions > Refresh screen > Run workflow for the first publish. The page URL appears on the deploy step.

## Daylight saving

The cron is in UTC. Sydney leaves daylight saving on the first Sunday of April and returns on the first Sunday of October,
so the screen runs one hour earlier in Sydney time during winter unless the cron is moved. Nothing breaks either way.

## Local test

`python screen.py run --out out` writes the same files into `out/`; `python notes.py --out out` adds notes.
The page needs a web server (fetch does not work from `file://`): any static server pointed at `docs/` will do.
