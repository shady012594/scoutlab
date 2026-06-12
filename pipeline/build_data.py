#!/usr/bin/env python3
"""ScoutLab data pipeline — fetch squads + season stats, score every player,
regenerate data/scoutlab-data.js for the web app.

Usage:
    SPORTMONKS_TOKEN=xxxx python3 pipeline/build_data.py
    MOCK_DIR=pipeline/mock python3 pipeline/build_data.py     # offline test run

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
from sportmonks import Client, SportmonksError, stats_by_name   # noqa: E402

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


def main() -> int:
    cfg = yaml.safe_load((ROOT / "pipeline" / "config.yaml").read_text(encoding="utf-8"))

    try:
        client = Client(cfg)
    except SportmonksError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    players: list[dict] = []
    league_summaries = []

    try:
        leagues = client.resolve_leagues(cfg["leagues"])
        for lg in leagues:
            print(f"== {lg['name']} (season {lg['season_name'] or lg['season_id']}) ==")
            teams = client.season_teams(lg["season_id"])
            print(f"   {len(teams)} teams")
            kept = 0
            for team in teams:
                squad = client.team_squad_with_stats(lg["season_id"], team["id"])
                for entry in squad:
                    player = entry.get("player") or {}
                    stats = stats_by_name(player, lg["season_id"])
                    scored = M.score_player(
                        entry, stats,
                        ctx={"league_name": lg["name"], "club_name": team.get("name", "—"),
                             "season_label": str(lg.get("season_name") or "current season")},
                        cfg=cfg,
                    )
                    if scored:
                        players.append(scored)
                        kept += 1
            print(f"   kept {kept} players (≥{cfg['model']['min_minutes']}' and outfield)")
            league_summaries.append(f"{lg['name']}")
    except SportmonksError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not players:
        print("ERROR: pipeline produced 0 players — not overwriting data file. "
              "Check league names in config.yaml and your plan's season access.", file=sys.stderr)
        return 1

    # Dedupe (loan/squad duplicates), keep highest-minutes instance per id
    players = list({p["id"]: p for p in sorted(players, key=lambda p: p["currentRating"])}.values())
    players.sort(key=lambda p: p["projectedValueM"] - p["currentValueM"], reverse=True)
    players = players[: int(cfg["output"]["max_players"])]

    data = {
        "meta": {
            "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": "Sportmonks · " + ", ".join(league_summaries),
            "demo": False,
            "model": "scoutlab-heuristic-v1",
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
