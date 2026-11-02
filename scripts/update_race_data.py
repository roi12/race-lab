#!/usr/bin/env python3
"""Fetch the next Australian thoroughbred races from PuntersEdge."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_BASE = "https://api.puntersedge.online"
API_PATH = "/v1/racing/next-to-go"
AET = ZoneInfo("Australia/Melbourne")
OUTPUT = Path(os.environ.get("RACE_DATA_PATH", "data/latest.json"))


def fetch_races(api_key: str) -> list[dict]:
    query = urllib.parse.urlencode({
        "num_races": "20",
        "categories": "horse",
        "country": "AU",
    })
    request = urllib.request.Request(
        f"{API_BASE}{API_PATH}?{query}",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "RaceLab-GitHub-Action/0.5 (paper research)",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"PuntersEdge request failed: {exc}") from exc

    if isinstance(payload, dict) and isinstance(payload.get("races"), list):
        payload = payload["races"]
    if not isinstance(payload, list):
        raise RuntimeError("PuntersEdge returned an unexpected response shape")
    return [race for race in payload if isinstance(race, dict)]


def parse_time(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return result or "unknown-venue"


def quote_for(runner: dict) -> tuple[float | None, str, str]:
    quotes = runner.get("bookmakers") or []
    candidates = []
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        price = quote.get("win_price", quote.get("price"))
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 1:
            continue
        candidates.append((
            price,
            str(quote.get("key") or quote.get("bookmaker") or "unknown"),
            str(quote.get("source_url") or quote.get("source_link") or ""),
        ))
    if not candidates:
        try:
            price = float(runner.get("win_price", runner.get("price")))
            if price > 1:
                return price, "PuntersEdge", ""
        except (TypeError, ValueError):
            pass
        return None, "", ""

    return max(candidates, key=lambda item: item[0])


def normalise(races: list[dict]) -> dict:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    meetings: dict[str, dict] = {}

    for source_race in races:
        if str(source_race.get("country") or "").upper() != "AU":
            continue
        if str(source_race.get("category") or "").lower() != "horse":
            continue
        venue = source_race.get("venue_canonical") or source_race.get("venue")
        number = source_race.get("race_number")
        start_time = source_race.get("start_time")
        if not venue or number is None or not start_time:
            continue

        local_start = parse_time(start_time).astimezone(AET)
        meeting_id = f"{slug(venue)}-{local_start.date().isoformat()}"
        meeting = meetings.setdefault(meeting_id, {
            "id": meeting_id,
            "venue": str(venue),
            "date": local_start.date().isoformat(),
            "track": "",
            "trackSource": "",
            "programSource": f"{API_BASE}{API_PATH}",
            "races": [],
        })

        scratch_numbers = {
            str(item.get("number"))
            for item in (source_race.get("scratchings") or [])
            if isinstance(item, dict) and item.get("number") is not None
        }
        runners = []
        for raw_runner in source_race.get("runners") or []:
            if not isinstance(raw_runner, dict):
                continue
            runner_number = raw_runner.get("number", raw_runner.get("no"))
            name = str(raw_runner.get("name") or "").strip()
            if runner_number is None or not name:
                continue
            price, bookmaker, source_url = quote_for(raw_runner)
            scratched = bool(
                raw_runner.get("scratched")
                or raw_runner.get("is_scratched")
                or str(runner_number) in scratch_numbers
            )
            runners.append({
                "no": int(runner_number),
                "name": name,
                "barrier": str(raw_runner.get("barrier") or ""),
                "weight": str(raw_runner.get("weight_kg", raw_runner.get("weight")) or ""),
                "jockey": str(raw_runner.get("jockey") or ""),
                "trainer": str(raw_runner.get("trainer") or ""),
                "scratched": scratched,
                "odds": price,
                "oddsSource": f"PuntersEdge:{bookmaker}" if bookmaker else "",
                "oddsAt": generated_at if price is not None else None,
                "oddsLink": source_url,
            })

        runners.sort(key=lambda runner: runner["no"])
        active_count = sum(1 for runner in runners if not runner["scratched"])
        track = str(source_race.get("track_condition") or "").strip()
        if track:
            meeting["track"] = track
            meeting["trackSource"] = f"{API_BASE}{API_PATH} · checked {generated_at}"

        meeting["races"].append({
            "number": int(number),
            "name": str(source_race.get("race_name") or f"Race {number}"),
            "time": local_start.strftime("%H:%M"),
            "distance": source_race.get("distance_m"),
            "expectedCount": active_count,
            "runners": runners,
            "fieldSource": f"{API_BASE}{API_PATH}",
            "fieldCheckedAt": generated_at,
            "raceId": source_race.get("race_id"),
            "marketAgeSeconds": source_race.get("data_age_seconds"),
            "marketStale": bool(source_race.get("stale")),
        })

    for meeting in meetings.values():
        meeting["races"].sort(key=lambda race: (race["time"], race["number"]))
    ordered = sorted(meetings.values(), key=lambda meeting: (meeting["date"], meeting["venue"]))
    if not ordered:
        raise RuntimeError("PuntersEdge returned no Australian thoroughbred races")

    return {
        "version": 1,
        "generatedAt": generated_at,
        "dataSource": "PuntersEdge API",
        "dataSourceUrl": f"{API_BASE}{API_PATH}",
        "currentMeeting": ordered[0]["id"],
        "currentRace": ordered[0]["races"][0]["number"],
        "meetings": ordered,
    }


def main() -> None:
    api_key = os.environ.get("PUNTERS_EDGE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Missing PUNTERS_EDGE_API_KEY GitHub secret")
    output = normalise(fetch_races(api_key))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}: {sum(len(m['races']) for m in output['meetings'])} races")


if __name__ == "__main__":
    main()
