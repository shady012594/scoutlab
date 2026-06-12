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



STAT_LABEL_TO_KEY = {
    "Goals /90": "goals", "Assists /90": "assists", "Key passes /90": "key_passes",
    "Dribbles won /90": "dribbles", "Tackles+Int /90": "tackles_int",
    "Duels won /90": "duels_won", "Clearances /90": "clearances", "Shots /90": "shots",
}


def attach_stat_percentiles(finals):
    """finals: list of (profile, player) for ONE league.
    Adds 'pct' to each per-90 keyStat: rank within league + role pool."""
    pools = {}
    for pr, _ in finals:
        pools.setdefault(M.POOLS[pr["pos"]], []).append(pr)
    for pr, pl in finals:
        pool = pools[M.POOLS[pr["pos"]]]
        n = len(pool)
        for stat in pl["keyStats"]:
            key = STAT_LABEL_TO_KEY.get(stat["label"])
            if not key:
                continue
            mine = pr["axes"]["raw"][key]
            less = sum(1 for q in pool if q["axes"]["raw"][key] < mine)
            equal = sum(1 for q in pool if q["axes"]["raw"][key] == mine)
            stat["pct"] = round((less + 0.5 * equal) / n, 3)


def attach_breakout(finals):
    """Buy-signal score 0-100: rank + age curve + price room + minutes + raw signal."""
    for pr, pl in finals:
        age, cur_val = pr["age"], pl["currentValueM"]
        parts = [
            ("rank", 35 * pr["_pct"]),
            ("age", 25 * max(0.0, min(1.0, (24 - age) / 7.0))),
            ("price", 20 * max(0.0, min(1.0, 1 - cur_val / 30.0))),
            ("minutes", 10 * pr["minutes_factor"]),
            ("signal", 10 * min(1.0, pr["c_abs"] * 1.6)),
        ]
        score = int(round(sum(v for _, v in parts)))
        why = {
            "rank": f"top {max(1, round((1 - pr['_pct']) * 100))}% of role pool",
            "age": f"age {age} — curve ahead",
            "price": f"priced at €{cur_val}M — room to run",
            "minutes": "trusted with heavy minutes",
            "signal": "strong underlying output",
        }
        top = sorted(parts, key=lambda kv: -kv[1])[:2]
        pl["breakout"] = score
        pl["breakoutWhy"] = " · ".join(why[k] for k, _ in top)


def attach_similar(all_finals):
    """Top-3 nearest profiles in the same role pool across the whole board."""
    import math as _math
    reg = []
    for pr, pl in all_finals:
        vec = [pr["axes"][k] for k in ("finish", "create", "carry", "defend", "involve")]
        reg.append((M.POOLS[pr["pos"]], vec, pr["age"], pl))
    for i, (pool_i, vec_i, age_i, pl_i) in enumerate(reg):
        cands = []
        for j, (pool_j, vec_j, age_j, pl_j) in enumerate(reg):
            if i == j:
                continue
            d = _math.sqrt(sum((a - b) ** 2 for a, b in zip(vec_i, vec_j))) + 0.03 * abs(age_i - age_j)
            if pool_i != pool_j:
                d += 0.9   # prefer same-role matches; cross-pool only fills gaps
            cands.append((d, pl_j))
        cands.sort(key=lambda t: t[0])
        pl_i["similar"] = [{"id": p["id"], "name": p["name"], "club": p["club"], "flag": p["flag"]}
                           for _, p in cands[:3]]


def load_previous(out_path):
    """id -> (rating, value) from the existing data file, for day-over-day movers."""
    import re as _re
    try:
        raw = out_path.read_text(encoding="utf-8")
        d = json.loads(_re.search(r"window\.SCOUTLAB_DATA = (.*);", raw, _re.S).group(1).replace("<\\/", "</"))
        if d.get("meta", {}).get("demo"):
            return {}, None
        return ({p["id"]: (p["currentRating"], p["currentValueM"]) for p in d.get("players", [])},
                d.get("meta", {}).get("generatedAt"))
    except Exception:
        return {}, None


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
    all_finals: list[tuple] = []
    league_summaries = []
    out_path_early = ROOT / cfg["output"]["data_js"]
    prev_map, prev_generated = load_previous(out_path_early)

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
            finals = [(pr, M.finalize(pr, pr["_pct"], pr["_ctx"], cfg)) for pr in profiles]
            attach_stat_percentiles(finals)
            attach_breakout(finals)
            all_finals.extend(finals)
            players.extend(pl for _, pl in finals)

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

    attach_similar(all_finals)
    for p in players:
        if p["id"] in prev_map:
            r0, v0 = prev_map[p["id"]]
            p["deltaRating"] = p["currentRating"] - r0
            p["deltaValueM"] = round(p["currentValueM"] - v0, 1)

    # Dedupe (loan/squad duplicates), keep highest-rated instance per id
    players = list({p["id"]: p for p in sorted(players, key=lambda p: p["currentRating"])}.values())
    players.sort(key=lambda p: p["projectedValueM"] - p["currentValueM"], reverse=True)
    players = players[: int(cfg["output"]["max_players"])]

    data = {
        "meta": {
            "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": "Sportmonks · " + ", ".join(league_summaries),
            "demo": False,
            "previousGeneratedAt": prev_generated,
            "model": "scoutlab-heuristic-v1.4",
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
