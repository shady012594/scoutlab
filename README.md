# ScoutLab Live ⚽

ScoutLab, upgraded from a 12-player demo to a **self-updating scouting board**: a pipeline pulls real current-season squads and stats from the Sportmonks API, scores every player with a transparent model, and regenerates the app's data — automatically, every night, for free, with nothing running on your own computer.

```
Sportmonks API ──▶ pipeline/build_data.py ──▶ data/scoutlab-data.js ──▶ index.html
                   (ratings · values · fits)        (auto-committed nightly by GitHub Actions)
```

## The honest reality of "most players in the world"

Football data is a market, and coverage is exactly what you pay for:

| Tier | What you get | Cost |
|---|---|---|
| **Sportmonks Free** | Danish Superliga + Scottish Premiership, current season, real player stats — forever free, no card | **€0** |
| Sportmonks Starter | Any **5 leagues** you choose | ~€29/mo |
| Sportmonks Growth / Pro | 30 / 120 leagues → tens of thousands of players | €99 / €249/mo |
| Enterprise (Sportmonks, Opta, StatsBomb, Wyscout) | Genuinely "most players in the world" — what real clubs and Jamestown-type firms buy | custom, €€€€ |

Two things make the free tier weirdly perfect for *this* app: the two free leagues are **FC Midtjylland's league and Hearts' league** — two clubs already on our demo board, and both squarely in Bloom-network territory. Scaling later means editing one list in `pipeline/config.yaml` and upgrading the plan; **zero code changes**.

One more honest note: **Transfermarkt has no official API**, and scraping it violates their terms. So this pipeline computes **its own value estimates** from rating, age, league and minutes — which is the most Jamestown-spirited part of the whole project. Real firms don't look prices up either; they model them.

## Setup (~15 minutes, all free, works from Windows)

**1. Get a Sportmonks token** — sign up at [my.sportmonks.com](https://my.sportmonks.com) (free plan, no credit card), copy your API token.

**2. Put this folder on GitHub** — create a repo (e.g. `scoutlab`), upload everything in this folder. Easiest on Windows: [GitHub Desktop](https://desktop.github.com), or drag-and-drop on github.com → "uploading an existing file".

**3. Add the token as a secret** — repo → Settings → Secrets and variables → Actions → New repository secret → name `SPORTMONKS_TOKEN`, paste the token.

**4. Turn on the site** — Settings → Pages → Source: *Deploy from a branch* → Branch `main`, folder `/ (root)`. Your board goes live at `https://<you>.github.io/scoutlab/` — phone, PC, anywhere.

**5. First data pull** — Actions tab → "Update ScoutLab data" → Run workflow. Two minutes later the board shows real players with a **MODEL DATA** pill and an "Updated …" stamp. From then on it refreshes itself every night at 04:30 UTC.

Prefer fully local instead? `pip install pyyaml`, set `SPORTMONKS_TOKEN`, run `python pipeline/build_data.py`, open `index.html`. Schedule it with Windows Task Scheduler if you want local auto-updates.

## How the model works (all knobs in `pipeline/config.yaml`)

- **Current rating** — per-90 output (goals, chance creation, dribbles, defensive actions, involvement) squashed against benchmarks, weighted by position, blended with the provider's match rating when present, then discounted by a **league-strength coefficient** (Superliga 0.74, Premiership 0.70, PL 1.00…).
- **Potential** — an age curve adds headroom (≈+13 at 19, ≈+3 at 25, 0 at 30), scaled by how good the underlying profile already is.
- **Values** — our own estimate: exponential in rating, shaped by age, selling-league market temperature, and minutes reliability. The peak-case value prices the player *at* their potential rating near peak age. Green→amber gap = the trade.
- **League fits** — projects the profile into target leagues (PL, Bundesliga, Eredivisie by default) via strength ratios.
- **Filters** — goalkeepers, under 450 minutes, and over-34s are skipped: not enough signal or no resale curve.

Every number is a **transparent toy estimate**, clearly labeled in the app. It is not Jamestown's model (theirs is private), and nothing here is affiliated with Jamestown Analytics, Starlizard, Tony Bloom or any club. Don't bet or trade on it.

## Testing & troubleshooting

- `python pipeline/test_offline.py` — runs the entire pipeline against bundled mock API fixtures, no token or internet needed. CI runs it before every live pull.
- The pipeline **never overwrites your data file with an empty one** — if it can't fetch, it exits loudly with a message saying exactly what to fix (bad token, league not in plan, etc.).
- League name doesn't match? The error prints every league name your plan actually exposes — copy the right one into `config.yaml`.

## Scaling up later

1. Upgrade the Sportmonks plan and pick leagues (Belgian Pro League, Eredivisie and the Championship are the on-brand picks).
2. Add their names under `leagues:` in `config.yaml`, plus a strength + market coefficient each.
3. Done — the next nightly run pulls them in. The app already handles big rosters (renders the top 300 of any sort, search/filters cover the rest).

## Files

```
index.html                      the app (open directly, or serve via GitHub Pages)
data/scoutlab-data.js           generated data (ships seeded with the 12-player demo)
pipeline/config.yaml            leagues + every model coefficient
pipeline/sportmonks.py          API client: auth, retries, rate-limit handling, mock mode
pipeline/model.py               ratings · potential · values · fits (documented heuristics)
pipeline/build_data.py          entry point
pipeline/test_offline.py        full offline test against mock fixtures
pipeline/mock/                  realistic API response fixtures
.github/workflows/update-data.yml   nightly auto-refresh
```
