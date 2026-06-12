"""ScoutLab scoring model v1.2 — transparent heuristics, documented inline.

Two-pass design:
  1. raw_profile()  — per player: parse, filter (GK / minutes / age), compute
     stat axes and an absolute composite from whatever stats the feed carries.
  2. finalize()     — given the player's percentile rank within his league +
     role pool, blend rank with the absolute composite and produce ratings,
     values and league fits. League-relative ranking keeps the board well
     spread even when the feed only carries a thin slice of stats.

Honesty note: every number is an ESTIMATE from public counting stats and
hand-set coefficients in config.yaml. A toy in the Jamestown spirit.
"""

from __future__ import annotations

import datetime
import math
import re
import unicodedata


# ----------------------------------------------------------------- positions

def map_position(player: dict) -> str | None:
    detailed = ((player.get("detailedposition") or player.get("detailedPosition") or {}).get("name") or "")
    coarse = ((player.get("position") or {}).get("name") or "")
    d, c = detailed.lower(), coarse.lower()

    if "goalkeeper" in d or "goalkeeper" in c:
        return None
    if "centre back" in d or "center back" in d:
        return "cb"
    if "back" in d or "wing back" in d:
        return "fb"
    if "defensive midfield" in d or "central midfield" in d or d == "midfield":
        return "cm"
    if "attacking midfield" in d:
        return "am"
    if "wing" in d or "winger" in d:
        return "w"
    if "centre forward" in d or "center forward" in d or "striker" in d or "forward" in d:
        return "st"
    if "defender" in c:
        return "cb"
    if "midfielder" in c:
        return "cm"
    if "attacker" in c or "forward" in c:
        return "st"
    return None


POOLS = {"st": "ATT", "w": "ATT", "am": "ATT", "cm": "MID", "cb": "DEF", "fb": "DEF"}
POSITION_LABEL = {"st": "striker", "w": "winger", "am": "attacking midfielder",
                  "cm": "central midfielder", "cb": "centre back", "fb": "full back"}

POSITION_WEIGHTS = {
    "st": dict(finish=0.46, create=0.16, carry=0.12, defend=0.04, involve=0.22),
    "w":  dict(finish=0.30, create=0.28, carry=0.22, defend=0.04, involve=0.16),
    "am": dict(finish=0.24, create=0.36, carry=0.16, defend=0.06, involve=0.18),
    "cm": dict(finish=0.10, create=0.26, carry=0.14, defend=0.26, involve=0.24),
    "fb": dict(finish=0.05, create=0.22, carry=0.18, defend=0.35, involve=0.20),
    "cb": dict(finish=0.03, create=0.08, carry=0.08, defend=0.58, involve=0.23),
}

# Benchmarks: roughly an *excellent* per-90 on each axis; used to squash raw
# per-90s into 0..1.
BENCH = dict(
    goals=0.70, assists=0.40, key_passes=2.2, big_chances=0.55,
    dribbles=2.2, duels_won=6.5, tackles_int=4.6, clearances=5.5,
    passes=70.0, shots=3.2,
)

# Stat-name aliases: feeds spell the same stat several ways. All lowercase;
# stats_by_season() lowercases keys before we get here. First hit wins.
ALIASES = {
    "minutes":       ["minutes played", "minutes"],
    "goals":         ["goals", "goals scored"],
    "assists":       ["assists"],
    "key_passes":    ["key passes", "keypasses", "passes key"],
    "big_chances":   ["big chances created", "big chances"],
    "dribbles":      ["successful dribbles", "dribbles", "dribble attempts"],
    "duels_won":     ["duels won", "total duels", "duels"],
    "tackles":       ["tackles", "total tackles"],
    "interceptions": ["interceptions", "total interceptions"],
    "clearances":    ["clearances", "total clearances", "effective clearances"],
    "passes":        ["accurate passes", "passes", "total passes"],
    "shots":         ["shots total", "shots", "total shots", "shots on target"],
    "rating":        ["rating", "average rating"],
    "penalties":     ["penalties", "penalties scored", "penalty goals"],
}


def _stat(stats: dict, key: str, default: float = 0.0) -> float:
    for name in ALIASES[key]:
        if name in stats:
            return stats[name]
    return default


def _squash(x: float, bench: float) -> float:
    if bench <= 0:
        return 0.0
    return 1.0 - math.exp(-max(x, 0.0) / bench)


def _per90(stats: dict, key: str, minutes: float) -> float:
    return _stat(stats, key) / minutes * 90.0 if minutes > 0 else 0.0


# ------------------------------------------------------------------- helpers

def compute_axes(stats: dict, minutes: float) -> dict:
    goals = _per90(stats, "goals", minutes)
    pens = _per90(stats, "penalties", minutes)
    goals_np = max(goals - pens, 0.0)
    assists = _per90(stats, "assists", minutes)
    keyp = _per90(stats, "key_passes", minutes)
    bigch = _per90(stats, "big_chances", minutes)
    drib = _per90(stats, "dribbles", minutes)
    duels = _per90(stats, "duels_won", minutes)
    tkl_int = _per90(stats, "tackles", minutes) + _per90(stats, "interceptions", minutes)
    clear = _per90(stats, "clearances", minutes)
    passes = _per90(stats, "passes", minutes)
    shots = _per90(stats, "shots", minutes)

    return dict(
        finish=0.75 * _squash(goals_np, BENCH["goals"]) + 0.25 * _squash(shots, BENCH["shots"]),
        create=0.45 * _squash(assists, BENCH["assists"])
             + 0.35 * _squash(keyp, BENCH["key_passes"])
             + 0.20 * _squash(bigch, BENCH["big_chances"]),
        carry=_squash(drib, BENCH["dribbles"]),
        defend=0.55 * _squash(tkl_int, BENCH["tackles_int"])
             + 0.25 * _squash(clear, BENCH["clearances"])
             + 0.20 * _squash(duels, BENCH["duels_won"]),
        involve=0.6 * _squash(passes, BENCH["passes"]) + 0.4 * _squash(duels, BENCH["duels_won"]),
        raw=dict(goals=goals, goals_np=goals_np, pens=pens,
                 assists=assists, key_passes=keyp, big_chances=bigch,
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
    decay = float(mcfg.get("value_age_decay", 0.16))
    floor_ = float(mcfg.get("value_age_floor", 0.12))
    age_mult = 1.0 if age <= peak else max(floor_, 1.0 - decay * (age - peak))
    # Market liquidity: below the elite tier, buyers thin out fast.
    lf = float(mcfg.get("liquidity_floor_rating", 58))
    span = float(mcfg.get("liquidity_span", 20))
    lmin = float(mcfg.get("liquidity_min", 0.3))
    liquidity = max(lmin, min(1.0, (rating - lf) / span))
    value = base * age_mult * market_mult * liquidity * (0.6 + 0.4 * minutes_factor)
    return min(value, float(mcfg.get("max_value_m", 180)))


def flag_emoji(iso2: str | None) -> str:
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c.upper()) - ord("A")) for c in iso2)


def slugify(name: str, pid) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return f"{s or 'player'}-{pid}"


# ------------------------------------------------------------- pass 1: parse

def raw_profile(squad_entry: dict, stats: dict, cfg: dict) -> dict | None:
    """Parse + filter one squad entry. Returns an intermediate profile or None."""
    mcfg = cfg["model"]
    player = squad_entry.get("player") or {}
    name = player.get("display_name") or player.get("name") or player.get("common_name")
    if not name:
        return None
    pos = map_position(player)
    if pos is None:
        return None
    minutes = _stat(stats, "minutes")
    if minutes < float(mcfg["min_minutes"]):
        return None
    age = age_from_dob(player.get("date_of_birth"))
    if age is None or age > 34:
        return None

    axes = compute_axes(stats, minutes)
    w = POSITION_WEIGHTS[pos]
    composite = sum(axes[k] * w[k] for k in w)
    minutes_factor = min(1.0, minutes / 1900.0)
    composite *= (0.75 + 0.25 * minutes_factor)
    provider_rating = _stat(stats, "rating")
    if 4.0 < provider_rating < 10.0:
        composite = 0.75 * composite + 0.25 * ((provider_rating - 5.8) / 2.4)

    return dict(
        pid=player.get("id", ""), name=name, pos=pos, age=age,
        photo=player.get("image_path") or "",
        jersey=squad_entry.get("jersey_number"),
        captain=bool(squad_entry.get("captain")),
        contract_end=(squad_entry.get("end") or "")[:4],
        iso2=(player.get("country") or {}).get("iso2"),
        nationality=(player.get("country") or {}).get("name") or "—",
        minutes=minutes, minutes_factor=minutes_factor,
        axes=axes, c_abs=max(0.0, min(1.0, composite)),
    )


# ---------------------------------------------------------- pass 2: finalize

def finalize(profile: dict, percentile: float, ctx: dict, cfg: dict) -> dict:
    """percentile ∈ [0,1]: rank within this league + role pool (1 = best)."""
    mcfg = cfg["model"]
    pos, age = profile["pos"], profile["age"]
    axes, raw = profile["axes"], profile["axes"]["raw"]
    minutes, minutes_factor = profile["minutes"], profile["minutes_factor"]

    league = ctx["league_name"]
    strength = league_lookup(cfg["league_strength"], league)
    market_mult = league_lookup(cfg["league_market_multiplier"], league)

    # Blend league-relative rank with the absolute composite: rank gives the
    # board a healthy spread even on thin stats; the absolute term keeps
    # cross-league comparisons honest.
    shaped = 0.18 + 0.62 * percentile
    c_final = 0.5 * shaped + 0.5 * min(profile["c_abs"] * 1.6, 1.0)

    floor, ceil = float(mcfg["rating_floor"]), float(mcfg["rating_ceiling"])
    gamma = float(mcfg.get("composite_gamma", 1.0))
    blend_a, blend_b = mcfg.get("strength_blend", [0.55, 0.45])
    pl_equivalent = floor + (c_final ** gamma) * (ceil - floor) * (float(blend_a) + float(blend_b) * strength)
    current = int(round(max(floor, min(ceil, pl_equivalent))))

    bonus = potential_bonus(age, mcfg["potential_bonus_by_age"])
    pa, pb = mcfg.get("potential_confidence_blend", [0.5, 0.5])
    trend = profile.get("trend")
    trend_mult = max(0.7, 1.0 + float(mcfg.get("trend_ceiling_weight", 0.15)) * trend) if trend is not None else 1.0
    potential = int(round(min(ceil, current + bonus * (float(pa) + float(pb) * c_final) * trend_mult)))

    cur_val = value_from_rating(current, age, market_mult, minutes_factor, mcfg)
    pm = float(mcfg.get("peak_market_blend", 0.5))
    peak_market = pm + (1.0 - pm) * market_mult
    peak = int(mcfg.get("peak_age", 26))
    pricing_age = min(age + 3, peak) if age < peak else age   # veterans never get re-aged
    peak_val = value_from_rating(potential, pricing_age,
                                 peak_market, 1.0, mcfg) * float(mcfg.get("peak_haircut", 0.78))
    peak_val = max(peak_val, cur_val * 1.05)

    fits = []
    for tgt in cfg["fit_targets"]:
        ratio = strength / float(tgt["strength"])
        fit = 12 + c_final * 95 * (0.45 + 0.55 * ratio) + 6 * minutes_factor
        fits.append({
            "league": tgt["name"],
            "score": int(round(max(15, min(96, fit)))),
            "note": fit_note(axes, tgt["name"], ratio),
        })
    fits.sort(key=lambda f: -f["score"])

    risk = "low" if (minutes_factor > 0.7 and age >= 21) else ("high" if minutes_factor < 0.45 or age <= 18 else "medium")
    availability = profile.get("availability")
    if availability is not None and availability < 45:
        risk = "high" if risk == "medium" else ("medium" if risk == "low" else risk)

    coverage = sum(1 for v in raw.values() if v > 0)
    confidence = 0.5 * minutes_factor + 0.5 * min(coverage / 8.0, 1.0)
    spread = 0.45 - 0.27 * confidence
    val_low = max(0.1, round(cur_val * (1 - spread), 1))
    val_high = round(cur_val * (1 + spread), 1)

    attributes = dict(
        technique=int(round(35 + 60 * (0.5 * axes["carry"] + 0.5 * axes["create"]))),
        physical=int(round(35 + 60 * min(1.0, raw["duels_won"] / BENCH["duels_won"]))),
        pace=int(round(38 + 55 * axes["carry"])),
        vision=int(round(35 + 60 * axes["create"])),
        finishing=int(round(32 + 63 * axes["finish"])),
        defending=int(round(30 + 65 * axes["defend"])),
    )

    return {
        "id": slugify(profile["name"], profile["pid"]),
        "name": profile["name"],
        "flag": flag_emoji(profile["iso2"]),
        "nationality": profile["nationality"],
        "age": age,
        "position": pos,
        "club": ctx["club_name"],
        "league": league,
        "leagueCode": league_code(league),
        "currentRating": current,
        "potentialRating": potential,
        "currentValueM": round(cur_val, 1),
        "projectedValueM": round(peak_val, 1),
        "valueLowM": val_low,
        "valueHighM": val_high,
        "confidence": ("High" if confidence > 0.72 else "Medium" if confidence > 0.45 else "Low"),
        **({"trend": round(trend, 2)} if trend is not None else {}),
        **({"availability": int(round(availability))} if availability is not None else {}),
        "contractUntil": profile.get("contract_end") or "—",
        "photo": profile.get("photo", ""),
        **({"jersey": int(profile["jersey"])} if profile.get("jersey") else {}),
        **({"captain": True} if profile.get("captain") else {}),
        "risk": risk,
        "attributes": attributes,
        "keyStats": build_key_stats(pos, raw, minutes),
        "leagueFits": fits,
        "comps": [
            f"Top {max(1, round((1 - percentile) * 100))}% of {POOLS[pos].lower()}-pool peers in this league by model output.",
            f"{int(minutes)} league minutes — "
            f"{'a nailed-on starter' if minutes_factor > 0.8 else 'a rotation player' if minutes_factor > 0.5 else 'limited minutes, wide error bars'}.",
        ],
        "notes": scout_note(pos, age, raw, minutes),
        "asOf": ctx["season_label"],
    }


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
    relevant = [(label, v) for label, v, ok in candidates if ok]
    nonzero = [(l, v) for l, v in relevant if v > 0]
    picked = (nonzero + [(l, v) for l, v in relevant if v <= 0])[:3]
    stats = [{"label": label, "value": f"{v:.2f}"} for label, v in picked]
    stats.append({"label": "Minutes", "value": f"{int(minutes)}"})
    return stats


def fit_note(axes: dict, target: str, ratio: float) -> str:
    if ratio >= 0.85:
        return "Small step up — current output should translate with a normal adaptation season."
    strong = max(("finish", "create", "carry", "defend"), key=lambda k: axes[k])
    nice = dict(finish="finishing volume", create="chance creation",
                carry="ball-carrying", defend="defensive workload")[strong]
    return f"Big league-strength jump; the {nice} is the part of the profile most likely to survive it."


def scout_note(pos: str, age: int, raw: dict, minutes: float) -> str:
    bits = [f"Auto-generated profile from counting stats ({int(minutes)} minutes)."]
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
