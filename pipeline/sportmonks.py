"""Sportmonks v3 client for the ScoutLab pipeline.

Design notes
------------
* Auth: token from the SPORTMONKS_TOKEN env var, sent as ?api_token=.
* The squads endpoint allows at most 3 levels of nested includes, so stat
  details arrive with numeric type_ids; names are resolved via one cached
  pull of the core /types endpoint instead of a 4-deep include.
* Season rollover safe: leagues are resolved together with their season list
  so the pipeline can fall back to the most recent season that has data.
* MOCK_DIR env var switches the client to local JSON fixtures so the whole
  pipeline can be tested offline (used by test_offline.py and CI).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "ScoutLab-pipeline/1.1 (personal project)"


class SportmonksError(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: dict):
        api = cfg.get("api", {})
        self.base = api.get("base_url", "https://api.sportmonks.com/v3/football").rstrip("/")
        self.core_base = self.base.rsplit("/", 1)[0] + "/core"
        self.delay = float(api.get("request_delay_seconds", 0.6))
        self.retries = int(api.get("max_retries", 4))
        self.timeout = int(api.get("timeout_seconds", 25))
        self.mock_dir = os.environ.get("MOCK_DIR")
        self.token = os.environ.get("SPORTMONKS_TOKEN", "")
        if not self.mock_dir and not self.token:
            raise SportmonksError(
                "SPORTMONKS_TOKEN is not set. Create a free account at my.sportmonks.com, "
                "copy your API token, and export it as an environment variable "
                "(or add it as a GitHub Actions secret named SPORTMONKS_TOKEN)."
            )
        self._calls = 0
        self._type_map: dict[int, str] | None = None

    # ------------------------------------------------------------------ http

    def _mock_path(self, path: str) -> Path:
        slug = path.strip("/").replace("/", "_")
        return Path(self.mock_dir) / f"{slug}.json"

    def _get(self, base: str, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if self.mock_dir:
            fp = self._mock_path(path)
            if not fp.exists():
                raise SportmonksError(f"[mock] fixture not found: {fp}")
            return json.loads(fp.read_text(encoding="utf-8"))

        params["api_token"] = self.token
        url = f"{base}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                self._calls += 1
                time.sleep(self.delay)
                return body
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
                if e.code == 429:
                    wait = min(60 * attempt, 180)
                    print(f"  rate-limited (429); sleeping {wait}s…", file=sys.stderr)
                    time.sleep(wait)
                    last_err = e
                    continue
                if e.code in (401, 403):
                    raise SportmonksError(
                        f"Auth failed ({e.code}) for {path}. Check SPORTMONKS_TOKEN, and that this "
                        f"league/season is included in your plan. API said: {detail}"
                    ) from e
                raise SportmonksError(f"HTTP {e.code} on {path}: {detail}") from e
            except Exception as e:  # network blips
                last_err = e
                time.sleep(2 * attempt)
        raise SportmonksError(f"Request failed after {self.retries} attempts: {path} ({last_err})")

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._get(self.base, path, params)

    def get_core(self, path: str, params: dict | None = None) -> dict:
        return self._get(self.core_base, path, params)

    def get_paginated(self, path: str, params: dict | None = None,
                      max_pages: int = 80, core: bool = False) -> list[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 50)
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            body = (self.get_core if core else self.get)(path, {**params, "page": page})
            data = body.get("data") or []
            if isinstance(data, dict):
                data = [data]
            out.extend(data)
            pag = (body.get("pagination") or {})
            if not pag.get("has_more"):
                break
            page += 1
        return out

    # ----------------------------------------------------------------- domain

    def type_map(self) -> dict[int, str]:
        """id → name for every stat type, fetched once from the core API."""
        if self._type_map is None:
            rows = self.get_paginated("types", core=True)
            self._type_map = {int(r["id"]): (r.get("name") or "").strip()
                              for r in rows if r.get("id") is not None}
            if not self._type_map:
                raise SportmonksError("Could not load the stat type list from /core/types.")
        return self._type_map

    def resolve_leagues(self, wanted_names: list[str]) -> list[dict]:
        """Match configured league names against the account's leagues.

        Returns, per league: id, name, and its seasons sorted newest-first as
        [{id, name, starting_at, is_current}], so callers can fall back to the
        last completed season when the current one has no stats yet.
        """
        rows = self.get_paginated("leagues", {"include": "currentSeason;seasons"})
        available: dict[str, dict] = {}
        for league in rows:
            current = league.get("currentseason") or league.get("currentSeason") or {}
            seasons = league.get("seasons") or []
            norm = []
            for s in seasons:
                norm.append({
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "starting_at": s.get("starting_at") or "",
                    "is_current": s.get("id") == current.get("id"),
                })
            norm.sort(key=lambda s: (s["is_current"], s["starting_at"] or ""), reverse=True)
            if not norm and current.get("id"):
                norm = [{"id": current["id"], "name": current.get("name"),
                         "starting_at": "", "is_current": True}]
            available[league.get("name", "")] = {
                "id": league.get("id"), "name": league.get("name"), "seasons": norm,
            }
        resolved = []
        for want in wanted_names:
            hit = available.get(want) or next(
                (v for k, v in available.items() if want.lower() in k.lower()), None
            )
            if not hit:
                raise SportmonksError(
                    f"League '{want}' not found in your plan. Available: {sorted(available)}"
                )
            if not hit["seasons"]:
                raise SportmonksError(f"League '{want}' exposes no seasons — check plan/season access.")
            resolved.append(hit)
        return resolved

    def season_teams(self, season_id: int) -> list[dict]:
        return self.get_paginated(f"teams/seasons/{season_id}")

    def team_squad_with_stats(self, season_id: int, team_id: int) -> list[dict]:
        """One call per team. Max nesting on this endpoint is 3, so the chain
        stops at statistics.details; type names come from type_map()."""
        include = ";".join([
            "player.statistics.details",
            "player.position",
            "player.detailedPosition",
            "player.country",
        ])
        body = self.get(f"squads/seasons/{season_id}/teams/{team_id}", {"include": include})
        data = body.get("data") or []
        return data if isinstance(data, list) else [data]

    def player_statistics(self, player_id) -> list[dict]:
        """Full per-player statistics — richer detail set than the squads include
        (3-level include is legal from the /players base)."""
        body = self.get(f"players/{player_id}", {"include": "statistics.details.type"})
        data = body.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return data.get("statistics") or []

    @property
    def calls_made(self) -> int:
        return self._calls


# --------------------------------------------------------------------- stats

def detail_value(detail: dict) -> float | None:
    """Stat detail values arrive as scalars or small dicts like {"total": 12},
    {"average": "7.21"}. Normalize to float."""
    v = detail.get("value")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    if isinstance(v, dict):
        for key in ("total", "all", "average", "count", "goals"):
            if key in v and isinstance(v[key], (int, float, str)):
                try:
                    return float(v[key])
                except (TypeError, ValueError):
                    continue
        for vv in v.values():
            if isinstance(vv, (int, float)):
                return float(vv)
    return None


def stats_by_season(player: dict, type_map: dict[int, str]) -> dict[int, dict[str, float]]:
    """Flatten a player's statistics into {season_id: {type_name_lower: value}}."""
    out: dict[int, dict[str, float]] = {}
    for block in player.get("statistics") or []:
        sid = block.get("season_id")
        if sid is None:
            continue
        bucket = out.setdefault(sid, {})
        for det in block.get("details") or []:
            name = ""
            t = det.get("type")
            if isinstance(t, dict):
                name = (t.get("name") or "").strip()
            if not name:
                name = type_map.get(det.get("type_id") or -1, "")
            if not name:
                continue
            val = detail_value(det)
            if val is not None:
                bucket[name.lower()] = val
    return out
