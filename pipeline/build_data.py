#!/usr/bin/env python3
"""ScoutLab data pipeline — fetch squads + season stats, score every player,
regenerate data/scoutlab-data.js for the web app.

Usage:
    SPORTMONKS_TOKEN=xxxx python3 pipeline/build_data.py
    MOCK_DIR=pipeline/mock python3 pipeline/build_data.py     # offline test run

Season rollover: if the current season has no meaningful minutes yet (e.g. it
is June and the new season hasn't kicked off), each player is scored from the
most recent season that does, and labeled accordingly.

Exit codes: 0 ok · 1 config/auth/data error (message says what to fix).
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model as M                      # noqa: E402
from sportmonks import Client, SportmonksError, stats_by_season   # noqa: E402

try:
    import yaml
except ImportError:
    print("Missing dependency: run  pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# The track-record cards are editorial, not API data — kept here.
CASE_STUDIES = [
    {"player": "Moisés Caicedo", "path": "Independiente del Valle → Brighton → Chelsea",
     "bought": "~£4.5M (2021)", "sold": "~£115M (2023)",
     "note": "The canonical win: scouted in Ecuador's league off the data, sold for a British record."},
    {"player": "Alexis Mac Allister", "path": "Argentinos Juniors → Brighton → Liverpool",
     "bought": "~£7M (2019)", "sold": "~£35M+ (2023)",
     "note": "Bought pre-hype on underlying numbers, sold months after winning the World Cup."},
    {"player": "Marc Cucurella", "path": "Getafe → Brighton → Chelsea",
     "bought": "~£15M (2021)", "sold": "~£60M (2022)",
     "note": "One season on the south coast, roughly a 4x return."},
    {"player": "Kaoru Mitoma", "path": "Kawasaki Frontale → Brighton (via Union SG loan)",
     "bought": "~£2.5M (2021)", "sold": "Still held — value 10x+",
     "note": "J-League inefficiency: elite take-on data the market hadn't priced."},
    {"player": "João Pedro", "path": "Watford → Brighton → Chelsea",
     "bought": "~£30M (2023)", "sold": "~£60M (2025)",
     "note": "Proof the model works at bigger ticket sizes, not just bargain bins."},
    {"player": "Rayan", "path": "Vasco da Gama → Bournemouth",
     "bought": "Academy", "sold": "~€28.5M + add-ons (Jan 2026)",
     "note": "Vasco's record sale — the South America → mid-PL pipeline this board hunts in."},
]


def pick_season_stats(per_season: dict, candidates: list[dict], min_minutes: float):
    """First candidate season (newest first) where the player has enough minutes.
    Returns (stats, season_label) or (None, None)."""
    for season in candidates:
        stats = per_season.get(season["id"]) or {}
        if stats.get("minutes played", 0.0) >= min_minutes:
            return stats, str(season.get("name") or season["id"])
    return None, None


def main() -> int:
    cfg = yaml.safe_load((ROOT / "pipeline" / "config.yaml").read_text(encoding="utf-8"))
    seasons_back = int(cfg.get("model", {}).get("seasons_back", 1))
    min_minutes = float(cfg["model"]["min_minutes"])

    try:
        client = Client(cfg)
    except SportmonksError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    players: list[dict] = []
    league_summaries = []

    try:
        type_map = client.type_map()
        print(f"Loaded {len(type_map)} stat types")
        leagues = client.resolve_leagues(cfg["leagues"])
        for lg in leagues:
            candidates = lg["seasons"][: seasons_back + 1]   # current + N previous
            print(f"== {lg['name']} (seasons considered: "
                  f"{', '.join(str(s['name']) for s in candidates)}) ==")

            # Squad-level rollover fallback: a brand-new season can have teams
            # but no registered squads yet — step back until squads exist.
            squads_by_team = []
            for season in candidates:
                teams = client.season_teams(season["id"])
                squads_by_team = [(team, client.team_squad_with_stats(season["id"], team["id"]))
                                  for team in teams]
                total_entries = sum(len(s) for _, s in squads_by_team)
                print(f"   {season['name']}: {len(teams)} teams, {total_entries} squad entries")
                if total_entries > 0:
                    break
                print(f"   no squads registered for {season['name']} yet — trying previous season")

            # Pass 1: parse + filter every squad entry into a raw profile
            stat_names_seen = {}
            profiles = []
            for team, squad in squads_by_team:
                for entry in squad:
                    player = entry.get("player") or {}
                    per_season = stats_by_season(player, type_map)
                    for bucket in per_season.values():
                        for name in bucket:
                            stat_names_seen[name] = stat_names_seen.get(name, 0) + 1
                    stats, season_label = pick_season_stats(per_season, candidates, min_minutes)
                    if stats is None:
                        continue
                    profile = M.raw_profile(entry, stats, cfg)
                    if profile:
                        profile["_ctx"] = {"league_name": lg["name"],
                                           "club_name": team.get("name", "—"),
                                           "season_label": season_label}
                        profiles.append(profile)

            # Pass 2: percentile rank within league + role pool, then finalize
            pools = {}
            for pr in profiles:
                pools.setdefault(M.POOLS[pr["pos"]], []).append(pr)
            for pool in pools.values():
                pool.sort(key=lambda p: (p["c_abs"], p["minutes"]))
                n = len(pool)
                for idx, pr in enumerate(pool):
                    pr["_pct"] = (idx + 0.5) / n   # midpoint rank: never exactly 0 or 1
            for pr in profiles:
                players.append(M.finalize(pr, pr["_pct"], pr["_ctx"], cfg))

            print(f"   kept {len(profiles)} players (≥{int(min_minutes)}' in a considered season, outfield)")
            top_names = sorted(stat_names_seen.items(), key=lambda kv: -kv[1])[:30]
            print("   stat types seen: " + ", ".join(f"{n} ({c})" for n, c in top_names))
            league_summaries.append(f"{lg['name']}")
    except SportmonksError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not players:
        print("ERROR: pipeline produced 0 players — not overwriting data file. "
              "Check league names in config.yaml and your plan's season access.", file=sys.stderr)
        return 1

    # Dedupe (loan/squad duplicates), keep highest-rated instance per id
    players = list({p["id"]: p for p in sorted(players, key=lambda p: p["currentRating"])}.values())
    players.sort(key=lambda p: p["projectedValueM"] - p["currentValueM"], reverse=True)
    players = players[: int(cfg["output"]["max_players"])]

    data = {
        "meta": {
            "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": "Sportmonks · " + ", ".join(league_summaries),
            "demo": False,
            "model": "scoutlab-heuristic-v1.2",
        },
        "players": players,
        "cases": CASE_STUDIES,
    }

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out_path = ROOT / cfg["output"]["data_js"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "/* Written by pipeline/build_data.py — do not edit by hand. */\n"
        f"window.SCOUTLAB_DATA = {payload};\n",
        encoding="utf-8",
    )

    print(f"\nWrote {out_path} — {len(players)} players · {client.calls_made} API calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
