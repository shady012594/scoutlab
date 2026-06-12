#!/usr/bin/env python3
"""Offline pipeline test — runs the full build against bundled mock fixtures.
No API token or network needed:   python3 pipeline/test_offline.py
"""
import json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = {**os.environ, "MOCK_DIR": str(ROOT / "pipeline" / "mock")}
for _ in range(2):   # second run exercises the day-over-day movement pass
    r = subprocess.run([sys.executable, str(ROOT / "pipeline" / "build_data.py")], env=env)
    assert r.returncode == 0, "pipeline exited non-zero"

raw = (ROOT / "data" / "scoutlab-data.js").read_text(encoding="utf-8")
data = json.loads(re.search(r"window\.SCOUTLAB_DATA = (.*);\n$", raw, re.S).group(1).replace("<\\/", "</"))
players = data["players"]
assert len(players) == 7, f"expected 7 mock players, got {len(players)}"
for p in players:
    assert p["potentialRating"] >= p["currentRating"], p["id"]
    assert p["projectedValueM"] > p["currentValueM"] > 0, p["id"]
    assert p["position"] in ("st","w","am","cm","cb","fb"), p["id"]
    assert len(p["leagueFits"]) == 3 and len(p["keyStats"]) == 4, p["id"]
    assert all(0 <= v <= 100 for v in p["attributes"].values()), p["id"]
assert data["meta"]["demo"] is False and data["cases"], "meta/cases missing"
assert all(p["asOf"] == "2025/2026" for p in players), "season fallback failed: " + str({p["id"]: p["asOf"] for p in players})
assert all(0 <= p["breakout"] <= 100 and p["breakoutWhy"] for p in players), "breakout missing"
assert all(len(p.get("similar", [])) >= 1 for p in players), "similar players missing"
assert all("deltaValueM" in p and "deltaRating" in p for p in players), "movement deltas missing after 2nd run"
assert any("pct" in s for p in players for s in p["keyStats"]), "stat percentiles missing"
assert data["meta"].get("focusClub") == "Heart of Midlothian", data["meta"].get("focusClub")
assert data["meta"].get("squadChanges") == {"joined": [], "left": []}, data["meta"].get("squadChanges")
hearts = [p for p in players if p["club"] == "Heart of Midlothian"]
assert hearts and all(p.get("squadRole") and p.get("clubRank") and p.get("jersey") and p["contractUntil"] == "2027" for p in hearts), \
    "focus-club annotation incomplete: " + str([(p["name"], p.get("squadRole"), p.get("jersey"), p["contractUntil"]) for p in hearts])
assert any(p.get("captain") for p in hearts), "captain flag missing"
assert all(p.get("photo") for p in players), "photos missing"
thin = next(p for p in players if "thinstats" in p["id"])
assert thin["currentRating"] > 52 and all(f["score"] > 30 for f in thin["leagueFits"]), \
    "thin-stats player collapsed to the floor: " + str(thin["currentRating"]) + " " + str([f["score"] for f in thin["leagueFits"]])
print("OFFLINE TEST PASSED — restore the demo seed or run the live pipeline before publishing.")
