#!/usr/bin/env python3
"""Fetch the next Australian thoroughbred races from PuntersEdge."""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_BASE = "https://api.puntersedge.online"
API_PATH = "/v1/racing/next-to-go"
FORM_PATH = "/v1/racing/form"
AET = ZoneInfo("Australia/Melbourne")
OUTPUT = Path(os.environ.get("RACE_DATA_PATH", "data/latest.json"))


def fetch_json(api_key: str, path: str, params: dict[str, str]) -> object:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{API_BASE}{path}?{query}",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "RaceLab-GitHub-Action/0.6 (paper research)",
            "X-API-Key": api_key,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))


def fetch_races(api_key: str) -> list[dict]:
    try:
        payload = fetch_json(api_key, API_PATH, {
            "num_races": "20",
            "categories": "horse",
            "country": "AU",
        })
    except Exception as exc:
        raise RuntimeError(f"PuntersEdge request failed: {exc}") from exc

    if isinstance(payload, dict) and isinstance(payload.get("races"), list):
        payload = payload["races"]
    if not isinstance(payload, list):
        raise RuntimeError("PuntersEdge returned an unexpected response shape")
    return [race for race in payload if isinstance(race, dict)]


def fetch_form(api_key: str, source_race: dict) -> dict | None:
    params = {"runs": "10"}
    race_id = str(source_race.get("race_id") or "").strip()
    if race_id:
        params["race_id"] = race_id
    else:
        venue = source_race.get("venue_canonical") or source_race.get("venue")
        start_time = source_race.get("start_time")
        if not venue or not start_time or source_race.get("race_number") is None:
            return None
        params.update({
            "venue": str(venue),
            "meeting_date": parse_time(start_time).astimezone(AET).date().isoformat(),
            "race_number": str(source_race["race_number"]),
        })
    try:
        payload = fetch_json(api_key, FORM_PATH, params)
    except Exception as exc:
        print(f"Form unavailable for race {race_id or params.get('race_number')}: {exc}", file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def numeric(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def finish_quality(run: dict) -> float | None:
    if str(run.get("status") or "ran").lower() not in {"ran", "finished", "complete"}:
        return None
    position = numeric(run.get("position"))
    field_size = numeric(run.get("field_size"))
    if position is None or field_size is None or position < 1 or field_size < 2:
        return None
    return max(0.0, min(1.0, (field_size - position) / max(field_size - 1.0, 1.0)))


def rating_from_runs(runs: list[dict]) -> int | None:
    values = [finish_quality(run) for run in runs]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(0, min(5, int(round(5 * sum(values) / len(values)))))


def condition_family(value: object) -> str:
    text = str(value or "").lower()
    if "synthetic" in text or "poly" in text:
        return "synthetic"
    for name in ("heavy", "soft", "good", "firm"):
        if name in text:
            return name
    return ""


def auto_ratings(form_runner: dict, distance: object, track_condition: object) -> tuple[dict, dict]:
    runs = [
        run for run in (form_runner.get("runs") or [])
        if isinstance(run, dict) and finish_quality(run) is not None
    ]
    target_distance = numeric(distance)
    target_going = condition_family(track_condition)
    ratings: dict[str, int] = {}
    evidence: dict[str, object] = {
        "runsUsed": len(runs),
        "method": "Automatic heuristic from PuntersEdge race form; not calibrated.",
    }

    recent = rating_from_runs(runs[:5])
    if recent is not None:
        ratings["recent"] = recent

    if target_distance is not None:
        distance_runs = [
            run for run in runs
            if numeric(run.get("distance_m")) is not None
            and abs(numeric(run.get("distance_m")) - target_distance) <= max(200.0, target_distance * 0.15)
        ]
        distance_rating = rating_from_runs(distance_runs)
        if distance_rating is not None:
            ratings["distance"] = distance_rating
            evidence["distanceRuns"] = len(distance_runs)

    if target_going:
        going_runs = [
            run for run in runs
            if condition_family(run.get("track_condition")) == target_going
        ]
        track_rating = rating_from_runs(going_runs)
        if track_rating is not None:
            ratings["track"] = track_rating
            evidence["trackRuns"] = len(going_runs)

    evidence["ratingsProvided"] = sorted(ratings)
    return ratings, evidence


def form_runner_by_number(form: dict | None, runner_number: object, runner_name: str) -> dict | None:
    if not form:
        return None
    for runner in form.get("runners") or []:
        if not isinstance(runner, dict):
            continue
        if runner.get("number") is not None and str(runner.get("number")) == str(runner_number):
            return runner
    wanted = re.sub(r"[^a-z0-9]+", "", runner_name.lower())
    for runner in form.get("runners") or []:
        if not isinstance(runner, dict):
            continue
        candidate = re.sub(r"[^a-z0-9]+", "", str(runner.get("name") or "").lower())
        if candidate and candidate == wanted:
            return runner
    return None


def form_summary(form_runner: dict | None, form: dict | None, form_source: str, checked_at: str) -> dict | None:
    if not form_runner:
        return None
    stats = form_runner.get("stats") if isinstance(form_runner.get("stats"), dict) else {}
    recent_runs = []
    for run in (form_runner.get("runs") or [])[:5]:
        if not isinstance(run, dict):
            continue
        recent_runs.append({
            "date": run.get("date"),
            "distance": run.get("distance_m"),
            "position": run.get("position"),
            "fieldSize": run.get("field_size"),
            "track": run.get("track_condition"),
            "class": run.get("race_class"),
            "status": run.get("status"),
        })
    return {
        "source": form_source,
        "checkedAt": checked_at,
        "asAt": (form or {}).get("form_as_at"),
        "runsAvailable": form_runner.get("runs_total", len(recent_runs)),
        "starts": stats.get("starts"),
        "wins": stats.get("wins"),
        "places": stats.get("places"),
        "winPct": stats.get("win_pct"),
        "placePct": stats.get("place_pct"),
        "recentRuns": recent_runs,
    }

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


def parse_local_race_start(meeting: dict, race: dict) -> datetime | None:
    date = str(meeting.get("date") or "")
    time = str(race.get("time") or "")
    try:
        return datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=AET)
    except ValueError:
        return None


def scheduled_refresh_ids(existing: dict) -> set[str]:
    """Return races in the final pre-race window that have not just refreshed."""
    now = datetime.now(AET)
    refresh_ids = set()
    for meeting in existing.get("meetings", []):
        for race in meeting.get("races", []):
            start = parse_local_race_start(meeting, race)
            if start is None:
                continue
            minutes = (start - now).total_seconds() / 60
            if 0.5 <= minutes <= 6:
                last = race.get("automatedRefreshAt")
                if last:
                    try:
                        last_at = parse_time(last).astimezone(AET)
                        if now - last_at < timedelta(minutes=10):
                            continue
                    except (TypeError, ValueError):
                        pass
                if race.get("raceId"):
                    refresh_ids.add(str(race["raceId"]))
    return refresh_ids


def normalise(
    races: list[dict],
    refresh_ids: set[str] | None = None,
    existing: dict | None = None,
    api_key: str | None = None,
    refresh_form: bool = False,
) -> dict:
    refresh_ids = refresh_ids or set()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    previous_races: dict[str, dict] = {}
    for old_meeting in (existing or {}).get("meetings", []):
        for old_race in old_meeting.get("races", []):
            key = str(old_race.get("raceId") or "")
            if key:
                previous_races[key] = old_race

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
            "formSource": f"{API_BASE}{FORM_PATH}",
            "races": [],
        })

        race_id = str(source_race.get("race_id") or "")
        previous = previous_races.get(race_id, {})
        previous_runners = {
            str(runner.get("no")): runner
            for runner in previous.get("runners", [])
            if isinstance(runner, dict) and runner.get("no") is not None
        }
        refresh_this_form = bool(
            refresh_form
            and (not refresh_ids or race_id in refresh_ids)
            and api_key
        )
        form_payload = fetch_form(api_key, source_race) if refresh_this_form else None
        form_fetched_at = generated_at if form_payload else previous.get("formCheckedAt")
        form_status = (
            "available" if form_payload
            else "unavailable" if refresh_this_form
            else previous.get("formStatus", "not-fetched")
        )

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
            old_runner = previous_runners.get(str(runner_number), {})
            form_runner = form_runner_by_number(form_payload, runner_number, name)
            ratings = old_runner.get("ratings") if isinstance(old_runner.get("ratings"), dict) else {}
            rating_evidence = old_runner.get("ratingEvidence")
            form_info = old_runner.get("form")

            if form_runner:
                ratings, rating_evidence = auto_ratings(
                    form_runner,
                    source_race.get("distance_m"),
                    source_race.get("track_condition"),
                )
                form_info = form_summary(
                    form_runner,
                    form_payload,
                    f"{API_BASE}{FORM_PATH}",
                    generated_at,
                )

            runner = {
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
                "ratings": ratings,
                "ratingSource": (
                    "PuntersEdge form · automatic heuristic"
                    if form_runner else old_runner.get("ratingSource", "")
                ),
                "ratingEvidence": rating_evidence,
                "form": form_info,
            }
            runners.append(runner)

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
            "formSource": f"{API_BASE}{FORM_PATH}",
            "formCheckedAt": form_fetched_at,
            "formAsAt": form_payload.get("form_as_at") if form_payload else previous.get("formAsAt"),
            "formStatus": form_status,
            "raceId": source_race.get("race_id"),
            "marketAgeSeconds": source_race.get("data_age_seconds"),
            "marketStale": bool(source_race.get("stale")),
            "automatedRefreshAt": generated_at if race_id in refresh_ids else None,
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
        "formDataSource": f"{API_BASE}{FORM_PATH}",
        "currentMeeting": ordered[0]["id"],
        "currentRace": ordered[0]["races"][0]["number"],
        "meetings": ordered,
    }

def main() -> None:
    api_key = os.environ.get("PUNTERS_EDGE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Missing PUNTERS_EDGE_API_KEY GitHub secret")

    mode = sys.argv[1] if len(sys.argv) > 1 else "--force"
    existing = None
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None

    if mode == "--scheduled":
        if not existing:
            print("No existing snapshot; waiting for the daily discovery run.")
            return
        refresh_ids = scheduled_refresh_ids(existing)
        if not refresh_ids:
            print("No race in the final pre-race window; no PuntersEdge call made.")
            return
    elif mode == "--force":
        refresh_ids = set()
    else:
        raise SystemExit("Use --force or --scheduled")

    output = normalise(fetch_races(api_key), refresh_ids, existing, api_key, refresh_form=(mode == "--force" or bool(refresh_ids)))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}: {sum(len(m['races']) for m in output['meetings'])} races")


if __name__ == "__main__":
    main()
