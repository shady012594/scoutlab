#!/usr/bin/env python3
"""Offline pipeline test — runs the full build against bundled mock fixtures.
No API token or network needed:   python3 pipeline/test_offline.py
"""
import json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = {**os.environ, "MOCK_DIR": str(ROOT / "pipeline" / "mock")}
r = subprocess.run([sys.executable, str(ROOT / "pipeline" / "build_data.py")], env=env)
assert r.returncode == 0, "pipeline exited non-zero"

raw = (ROOT / "data" / "scoutlab-data.js").read_text(encoding="utf-8")
data = json.loads(re.search(r"window\.SCOUTLAB_DATA = (.*);\n$", raw, re.S).group(1).replace("<\\/", "</"))
players = data["players"]
assert len(players) == 6, f"expected 6 mock players, got {len(players)}"
for p in players:
    assert p["potentialRating"] >= p["currentRating"], p["id"]
    assert p["projectedValueM"] > p["currentValueM"] > 0, p["id"]
    assert p["position"] in ("st","w","am","cm","cb","fb"), p["id"]
    assert len(p["leagueFits"]) == 3 and len(p["keyStats"]) == 4, p["id"]
    assert all(0 <= v <= 100 for v in p["attributes"].values()), p["id"]
assert data["meta"]["demo"] is False and data["cases"], "meta/cases missing"
assert all(p["asOf"] == "2025/2026" for p in players), "season fallback failed: " + str({p["id"]: p["asOf"] for p in players})
print("OFFLINE TEST PASSED — restore the demo seed or run the live pipeline before publishing.")
