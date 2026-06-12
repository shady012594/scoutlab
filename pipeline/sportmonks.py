"""Sportmonks v3 client for the ScoutLab pipeline.

Design notes
------------
* Auth: token from the SPORTMONKS_TOKEN env var, sent as ?api_token=.
* Robustness over cleverness: stat types and positions are resolved by NAME
  via API includes (statistics.details.type, player.position), never by
  hard-coded numeric IDs, so provider-side ID changes can't silently break us.
* Efficiency: one squad call per team with a deep include pulls every squad
  player together with their season statistics — ~1 call per team instead of
  ~25 calls per team.
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

USER_AGENT = "ScoutLab-pipeline/1.0 (personal project)"


class SportmonksError(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: dict):
        api = cfg.get("api", {})
        self.base = api.get("base_url", "https://api.sportmonks.com/v3/football").rstrip("/")
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

    # ------------------------------------------------------------------ http

    def _mock_path(self, path: str, params: dict) -> Path:
        """Deterministic fixture filename for a request, e.g. 'leagues' or 'squads_seasons_23_teams_86'."""
        slug = path.strip("/").replace("/", "_")
        return Path(self.mock_dir) / f"{slug}.json"

    def get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if self.mock_dir:
            fp = self._mock_path(path, params)
            if not fp.exists():
                raise SportmonksError(f"[mock] fixture not found: {fp}")
            return json.loads(fp.read_text(encoding="utf-8"))

        params["api_token"] = self.token
        url = f"{self.base}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
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

    def get_paginated(self, path: str, params: dict | None = None, max_pages: int = 40) -> list[dict]:
        """Follow v3 pagination, concatenating `data` arrays."""
        params = dict(params or {})
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            body = self.get(path, {**params, "page": page})
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

    def resolve_leagues(self, wanted_names: list[str]) -> list[dict]:
        """Match configured league names against the account's available leagues.

        Returns [{id, name, season_id, season_name}], failing loudly with the
        actual available names if something in config doesn't match.
        """
        rows = self.get_paginated("leagues", {"include": "currentSeason"})
        available = {}
        for league in rows:
            season = league.get("currentseason") or league.get("currentSeason") or {}
            available[league.get("name", "")] = {
                "id": league.get("id"),
                "name": league.get("name"),
                "season_id": season.get("id"),
                "season_name": season.get("name"),
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
            if not hit["season_id"]:
                raise SportmonksError(f"League '{want}' has no current season exposed — check plan/season access.")
            resolved.append(hit)
        return resolved

    def season_teams(self, season_id: int) -> list[dict]:
        return self.get_paginated(f"teams/seasons/{season_id}")

    def team_squad_with_stats(self, season_id: int, team_id: int) -> list[dict]:
        """One call: every squad player + season statistics, with names for
        stat types and positions resolved server-side via includes."""
        include = ";".join([
            "player.statistics.details.type",
            "player.position",
            "player.detailedPosition",
            "player.country",
        ])
        body = self.get(
            f"squads/seasons/{season_id}/teams/{team_id}",
            {"include": include},
        )
        data = body.get("data") or []
        return data if isinstance(data, list) else [data]

    @property
    def calls_made(self) -> int:
        return self._calls


# --------------------------------------------------------------------- stats

def detail_value(detail: dict) -> float | None:
    """Sportmonks stat detail values arrive as scalars or small dicts like
    {"total": 12}, {"average": "7.21"}, {"goals": 9, ...}. Normalize to float."""
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


def stats_by_name(player: dict, season_id: int) -> dict[str, float]:
    """Flatten a player's statistics for the given season into {type_name: value}."""
    out: dict[str, float] = {}
    for block in player.get("statistics") or []:
        if season_id and block.get("season_id") not in (season_id, None):
            continue
        for det in block.get("details") or []:
            t = det.get("type") or {}
            name = (t.get("name") or "").strip()
            if not name:
                continue
            val = detail_value(det)
            if val is not None:
                out[name] = val
    return out
