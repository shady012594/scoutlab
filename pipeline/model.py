"""ScoutLab scoring model — transparent heuristics, documented inline.

Pipeline contract: take one raw squad-player record (Sportmonks shape, stats
already flattened to {type_name: value}) plus league context, return either a
ScoutLab player dict matching the app's schema, or None (goalkeepers, players
below the minutes threshold, unparseable records).

Honesty note: every number this file produces is an ESTIMATE from public
counting stats and a handful of hand-set coefficients in config.yaml. It is a
toy in the Jamestown spirit; the app labels it accordingly.
"""

from __future__ import annotations

import datetime
import math
import re
import unicodedata


# ----------------------------------------------------------------- positions

def map_position(player: dict) -> str | None:
    """Map provider position names to ScoutLab's st/w/am/cm/cb/fb. None = skip (GKs/unknown)."""
    detailed = ((player.get("detailedposition") or player.get("detailedPosition") or {}).get("name") or "")
    coarse = ((player.get("position") or {}).get("name") or "")
    d, c = detailed.lower(), coarse.lower()

    if "goalkeeper" in d or "goalkeeper" in c:
        return None
    if "centre back" in d or "center back" in d:
        return "cb"
    if "back" in d or "wing back" in d:          # left/right/wing backs
        return "fb"
    if "defensive midfield" in d or "central midfield" in d or "midfield" == d:
        return "cm"
    if "attacking midfield" in d:
        return "am"
    if "wing" in d or "winger" in d:
        return "w"
    if "centre forward" in d or "center forward" in d or "striker" in d or "forward" in d:
        return "st"
    # fall back to the coarse position
    if "defender" in c:
        return "cb"
    if "midfielder" in c:
        return "cm"
    if "attacker" in c or "forward" in c:
        return "st"
    return None


POSITION_WEIGHTS = {
    # weights over the normalized signal axes, per position
    #            finish  create  carry   defend  involve
    "st": dict(finish=0.46, create=0.16, carry=0.12, defend=0.04, involve=0.22),
    "w":  dict(finish=0.30, create=0.28, carry=0.22, defend=0.04, involve=0.16),
    "am": dict(finish=0.24, create=0.36, carry=0.16, defend=0.06, involve=0.18),
    "cm": dict(finish=0.10, create=0.26, carry=0.14, defend=0.26, involve=0.24),
    "fb": dict(finish=0.05, create=0.22, carry=0.18, defend=0.35, involve=0.20),
    "cb": dict(finish=0.03, create=0.08, carry=0.08, defend=0.58, involve=0.23),
}

# Benchmarks: roughly what an *excellent* Premier League-level per-90 looks
# like on each axis; used to squash raw per-90s into 0..1.
BENCH = dict(
    goals=0.70, assists=0.40, key_passes=2.2, big_chances=0.55,
    dribbles=2.2, duels_won=6.5, tackles_int=4.6, clearances=5.5,
    passes=70.0, shots=3.2,
)


def _squash(x: float, bench: float) -> float:
    """0..1 with diminishing returns; hits ~0.63 at the benchmark."""
    if bench <= 0:
        return 0.0
    return 1.0 - math.exp(-max(x, 0.0) / bench)


def _per90(stats: dict, names: list[str], minutes: float) -> float:
    total = 0.0
    for n in names:
        total += stats.get(n.lower(), 0.0)
    return total / minutes * 90.0 if minutes > 0 else 0.0


def _first(stats: dict, names: list[str], default: float = 0.0) -> float:
    for n in names:
        if n.lower() in stats:
            return stats[n.lower()]
    return default


# ------------------------------------------------------------------- ratings

def compute_axes(stats: dict, minutes: float) -> dict:
    goals = _per90(stats, ["Goals"], minutes)
    assists = _per90(stats, ["Assists"], minutes)
    keyp = _per90(stats, ["Key Passes"], minutes)
    bigch = _per90(stats, ["Big Chances Created"], minutes)
    drib = _per90(stats, ["Successful Dribbles", "Dribbled Attempts Succeeded"], minutes)
    duels = _per90(stats, ["Duels Won"], minutes)
    tkl_int = _per90(stats, ["Tackles"], minutes) + _per90(stats, ["Interceptions"], minutes)
    clear = _per90(stats, ["Clearances"], minutes)
    passes = _per90(stats, ["Accurate Passes", "Passes"], minutes)
    shots = _per90(stats, ["Shots Total"], minutes)

    return dict(
        finish=0.75 * _squash(goals, BENCH["goals"]) + 0.25 * _squash(shots, BENCH["shots"]),
        create=0.45 * _squash(assists, BENCH["assists"])
             + 0.35 * _squash(keyp, BENCH["key_passes"])
             + 0.20 * _squash(bigch, BENCH["big_chances"]),
        carry=_squash(drib, BENCH["dribbles"]),
        defend=0.55 * _squash(tkl_int, BENCH["tackles_int"])
             + 0.25 * _squash(clear, BENCH["clearances"])
             + 0.20 * _squash(duels, BENCH["duels_won"]),
        involve=0.6 * _squash(passes, BENCH["passes"]) + 0.4 * _squash(duels, BENCH["duels_won"]),
        raw=dict(goals=goals, assists=assists, key_passes=keyp, big_chances=bigch,
                 dribbles=drib, duels_won=duels, tackles_int=tkl_int,
                 clearances=clear, passes=passes, shots=shots),
    )


def age_from_dob(dob: str | None) -> int | None:
    if not dob:
        return None
    try:
        born = datetime.date.fromisoformat(dob[:10])
    except ValueError:
        return None
    today = datetime.date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def potential_bonus(age: int, table: dict) -> float:
    """Linear interpolation over the config's age→bonus anchors."""
    pts = sorted((int(k), float(v)) for k, v in table.items())
    if age <= pts[0][0]:
        return pts[0][1]
    if age >= pts[-1][0]:
        return pts[-1][1]
    for (a1, b1), (a2, b2) in zip(pts, pts[1:]):
        if a1 <= age <= a2:
            t = (age - a1) / (a2 - a1)
            return b1 + t * (b2 - b1)
    return 0.0


def league_lookup(table: dict, league_name: str) -> float:
    if league_name in table:
        return float(table[league_name])
    for k, v in table.items():
        if k != "default" and k.lower() in league_name.lower():
            return float(v)
    return float(table.get("default", 0.6))


def value_from_rating(rating: float, age: int, market_mult: float, minutes_factor: float, mcfg: dict) -> float:
    base = float(mcfg["value_base_m"]) * math.exp(float(mcfg["value_slope"]) * (rating - 60.0))
    peak = int(mcfg.get("peak_age", 26))
    # Younger than peak: full price (the curve is the asset). Older: decay.
    age_mult = 1.0 if age <= peak else max(0.35, 1.0 - 0.09 * (age - peak))
    value = base * age_mult * market_mult * (0.6 + 0.4 * minutes_factor)
    return min(value, float(mcfg.get("max_value_m", 180)))


# ----------------------------------------------------------------- the model

def flag_emoji(iso2: str | None) -> str:
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c.upper()) - ord("A")) for c in iso2)


def slugify(name: str, pid) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return f"{s or 'player'}-{pid}"


def score_player(squad_entry: dict, stats: dict, ctx: dict, cfg: dict) -> dict | None:
    """ctx = {league_name, club_name, season_label}. Returns app-schema dict or None."""
    mcfg = cfg["model"]
    player = squad_entry.get("player") or {}
    name = player.get("display_name") or player.get("name") or player.get("common_name")
    if not name:
        return None

    pos = map_position(player)
    if pos is None:
        return None

    minutes = _first(stats, ["Minutes Played"], 0.0)
    if minutes < float(mcfg["min_minutes"]):
        return None

    age = age_from_dob(player.get("date_of_birth"))
    if age is None or age > 34:
        return None

    league = ctx["league_name"]
    strength = league_lookup(cfg["league_strength"], league)
    market_mult = league_lookup(cfg["league_market_multiplier"], league)

    axes = compute_axes(stats, minutes)
    w = POSITION_WEIGHTS[pos]
    composite = sum(axes[k] * w[k] for k in w)          # 0..~1

    # Minutes confidence: full trust around a starter's season (~1900')
    minutes_factor = min(1.0, minutes / 1900.0)
    composite *= (0.75 + 0.25 * minutes_factor)

    # Provider's own match rating (avg, ~6..8) blended in when available.
    provider_rating = _first(stats, ["Rating"], 0.0)
    if 4.0 < provider_rating < 10.0:
        composite = 0.75 * composite + 0.25 * ((provider_rating - 5.8) / 2.4)

    floor, ceil = float(mcfg["rating_floor"]), float(mcfg["rating_ceiling"])
    gamma = float(mcfg.get("composite_gamma", 1.0))
    blend_a, blend_b = mcfg.get("strength_blend", [0.55, 0.45])
    pl_equivalent = floor + (composite ** gamma) * (ceil - floor) * (float(blend_a) + float(blend_b) * strength)
    current = int(round(max(floor, min(ceil, pl_equivalent))))

    bonus = potential_bonus(age, mcfg["potential_bonus_by_age"])
    potential = int(round(min(ceil, current + bonus * (0.7 + 0.3 * composite))))

    cur_val = value_from_rating(current, age, market_mult, minutes_factor, mcfg)
    peak_val = value_from_rating(potential, min(age + 3, mcfg.get("peak_age", 26)), 1.0, 1.0, mcfg)
    peak_val = max(peak_val, cur_val * 1.05)

    # League fits: project the player into each configured target league.
    fits = []
    for tgt in cfg["fit_targets"]:
        ratio = strength / float(tgt["strength"])
        fit = composite * 100 * (0.55 + 0.45 * ratio) + 6 * minutes_factor
        fits.append({
            "league": tgt["name"],
            "score": int(round(max(35, min(96, fit)))),
            "note": fit_note(pos, axes, tgt["name"], ratio),
        })
    fits.sort(key=lambda f: -f["score"])

    raw = axes["raw"]
    risk = "low" if (minutes_factor > 0.7 and age >= 21) else ("high" if minutes_factor < 0.45 or age <= 18 else "medium")

    attributes = dict(
        technique=int(round(35 + 60 * (0.5 * axes["carry"] + 0.5 * axes["create"]))),
        physical=int(round(35 + 60 * min(1.0, raw["duels_won"] / BENCH["duels_won"]))),
        pace=int(round(38 + 55 * axes["carry"])),                      # proxy: ball-carrying
        vision=int(round(35 + 60 * axes["create"])),
        finishing=int(round(32 + 63 * axes["finish"])),
        defending=int(round(30 + 65 * axes["defend"])),
    )

    key_stats = build_key_stats(pos, raw, minutes)

    return {
        "id": slugify(name, player.get("id", "")),
        "name": name,
        "flag": flag_emoji((player.get("country") or {}).get("iso2")),
        "nationality": (player.get("country") or {}).get("name") or "—",
        "age": age,
        "position": pos,
        "club": ctx["club_name"],
        "league": league,
        "leagueCode": league_code(league),
        "currentRating": current,
        "potentialRating": potential,
        "currentValueM": round(cur_val, 1),
        "projectedValueM": round(peak_val, 1),
        "contractUntil": "—",
        "risk": risk,
        "attributes": attributes,
        "keyStats": key_stats,
        "leagueFits": fits,
        "comps": [
            f"Model sees a {current}-rated {POSITION_LABEL[pos]} producing in a league at "
            f"{strength:.2f}× Premier League strength.",
            f"{int(minutes)} league minutes this season — "
            f"{'a nailed-on starter' if minutes_factor > 0.8 else 'a rotation player' if minutes_factor > 0.5 else 'limited minutes, wide error bars'}.",
        ],
        "notes": scout_note(name, pos, age, raw, minutes),
        "asOf": ctx["season_label"],
    }


POSITION_LABEL = {"st": "striker", "w": "winger", "am": "attacking midfielder",
                  "cm": "central midfielder", "cb": "centre back", "fb": "full back"}


def league_code(league_name: str) -> str:
    words = [w for w in re.split(r"\W+", league_name) if w]
    return (words[0][:3] if len(words) == 1 else "".join(w[0] for w in words[:3])).upper()


def build_key_stats(pos: str, raw: dict, minutes: float) -> list[dict]:
    candidates = [
        ("Goals /90", raw["goals"], pos in ("st", "w", "am")),
        ("Assists /90", raw["assists"], True),
        ("Key passes /90", raw["key_passes"], pos in ("am", "w", "cm", "fb")),
        ("Dribbles won /90", raw["dribbles"], pos in ("w", "am", "st", "fb")),
        ("Tackles+Int /90", raw["tackles_int"], pos in ("cb", "fb", "cm")),
        ("Duels won /90", raw["duels_won"], pos in ("cb", "cm", "st")),
        ("Clearances /90", raw["clearances"], pos == "cb"),
        ("Shots /90", raw["shots"], pos in ("st", "w")),
    ]
    picked = [(label, v) for label, v, ok in candidates if ok][:3]
    stats = [{"label": label, "value": f"{v:.2f}"} for label, v in picked]
    stats.append({"label": "Minutes", "value": f"{int(minutes)}"})
    return stats


def fit_note(pos: str, axes: dict, target: str, ratio: float) -> str:
    if ratio >= 0.85:
        return f"Small step up — current output should translate with a normal adaptation season."
    strong = max(("finish", "create", "carry", "defend"), key=lambda k: axes[k])
    nice = dict(finish="finishing volume", create="chance creation", carry="ball-carrying", defend="defensive workload")[strong]
    return f"Big league-strength jump; the {nice} is the part of the profile most likely to survive it."


def scout_note(name: str, pos: str, age: int, raw: dict, minutes: float) -> str:
    bits = [f"Auto-generated profile from current-season counting stats ({int(minutes)} minutes)."]
    if raw["goals"] >= 0.45:
        bits.append(f"Scoring at {raw['goals']:.2f} per 90 — genuine penalty-box output.")
    if raw["assists"] + raw["key_passes"] >= 1.6:
        bits.append("Creates steadily for teammates.")
    if raw["dribbles"] >= 1.4:
        bits.append("Beats opponents off the dribble.")
    if raw["tackles_int"] >= 3.2:
        bits.append("High defensive activity for the role.")
    bits.append(f"Age {age}: " + ("most of the development curve still ahead." if age <= 21
                else "approaching peak — value case rests on the now, not the ceiling." if age >= 25
                else "prime development window."))
    return " ".join(bits)
