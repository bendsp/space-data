#!/usr/bin/env python3
"""Export the SQLite data store into a compact static JSON payload for Vite."""

from __future__ import annotations

import calendar
import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "space_people.sqlite"
OUT_PATH = ROOT / "public" / "data" / "space-data.json"
WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "space-data-site-export/0.1 (local static site build)"


def fetch_json(url: str, params: dict[str, str]) -> dict:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def wiki_title(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    if not path.startswith("/wiki/"):
        return None
    return unquote(path.removeprefix("/wiki/")).replace("_", " ")


def wikipedia_thumbnails(urls: list[str | None]) -> dict[str, str]:
    title_by_url = {url: wiki_title(url) for url in urls if wiki_title(url)}
    thumbnail_by_url: dict[str, str] = {}
    titles = sorted(set(title_by_url.values()))

    for start in range(0, len(titles), 50):
        chunk = titles[start : start + 50]
        data = fetch_json(
            WIKI_API,
            {
                "action": "query",
                "format": "json",
                "prop": "pageimages",
                "pithumbsize": "320",
                "titles": "|".join(chunk),
                "redirects": "1",
            },
        )
        thumbnail_by_title: dict[str, str] = {}
        for page in data.get("query", {}).get("pages", {}).values():
            title = page.get("title")
            thumbnail = page.get("thumbnail", {}).get("source")
            if title and thumbnail:
                thumbnail_by_title[title] = thumbnail

        redirect_map = {item["from"]: item["to"] for item in data.get("query", {}).get("redirects", [])}
        for url, title in title_by_url.items():
            resolved = redirect_map.get(title, title)
            if resolved in thumbnail_by_title:
                thumbnail_by_url[url] = thumbnail_by_title[resolved]
        time.sleep(0.2)

    return thumbnail_by_url


def iso_year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-wikipedia-images",
        action="store_true",
        help="Fetch thumbnail URLs from Wikipedia. Disabled by default so static builds do not depend on network APIs.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    people_rows = conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.gender,
            a.nationality,
            a.wikipedia_url,
            a.wikidata_id,
            a.first_flight_rank,
            a.first_flight_date,
            a.first_flight_unix,
            a.first_flight_name,
            a.time_in_space_seconds,
            a.segment_count,
            a.is_currently_in_space,
            COUNT(i.id) AS mission_count
        FROM astronauts a
        LEFT JOIN mission_crew_intervals i ON i.astronaut_id = a.id
        GROUP BY a.id
        ORDER BY a.first_flight_rank IS NULL, a.first_flight_rank, a.name
        """
    ).fetchall()

    mission_rows = conn.execute(
        """
        SELECT id, name, wikipedia_url, launch_at, launch_unix, landing_at,
               landing_unix, duration_seconds
        FROM missions
        """
    ).fetchall()

    interval_rows = conn.execute(
        """
        SELECT
            i.id,
            i.astronaut_id,
            i.mission_id,
            m.name AS mission_name,
            m.wikipedia_url AS mission_url,
            i.launch_at,
            i.launch_unix,
            i.landing_at,
            i.landing_unix,
            i.duration_seconds,
            i.is_current
        FROM mission_crew_intervals i
        JOIN missions m ON m.id = i.mission_id
        ORDER BY i.astronaut_id, i.launch_unix
        """
    ).fetchall()

    segment_rows = conn.execute(
        """
        SELECT id, astronaut_id, segment_index, start_at, start_unix, end_at,
               end_unix, duration_seconds, is_current, source_interval_ids
        FROM space_time_segments
        ORDER BY start_unix, astronaut_id
        """
    ).fetchall()

    thumbnails = (
        wikipedia_thumbnails([row["wikipedia_url"] for row in people_rows])
        if args.with_wikipedia_images
        else {}
    )
    mission_by_id = {row["id"]: dict(row) for row in mission_rows}

    people = []
    for row in people_rows:
        person = dict(row)
        person["image_url"] = thumbnails.get(row["wikipedia_url"])
        person["first_flight_year"] = (
            datetime.fromtimestamp(row["first_flight_unix"], UTC).year
            if row["first_flight_unix"]
            else None
        )
        people.append(person)

    intervals = [dict(row) for row in interval_rows]
    intervals_by_id = {str(row["id"]): dict(row) for row in interval_rows}

    segments = []
    for row in segment_rows:
        interval_ids = [part for part in row["source_interval_ids"].split(",") if part]
        segment_intervals = [intervals_by_id[item] for item in interval_ids if item in intervals_by_id]
        mission_names = []
        mission_ids = []
        for interval in segment_intervals:
            if interval["mission_id"] not in mission_ids:
                mission_ids.append(interval["mission_id"])
                mission_names.append(interval["mission_name"])
        segment = dict(row)
        segment["mission_ids"] = mission_ids
        segment["mission_names"] = mission_names
        segment["year"] = iso_year(row["start_at"])
        segments.append(segment)

    now = datetime.now(UTC)
    now_unix = calendar.timegm(now.utctimetuple())
    total_time_seconds = sum(row["time_in_space_seconds"] or 0 for row in people_rows)
    countries = {
        country.strip()
        for row in people_rows
        for country in (row["nationality"] or "").replace("/", ",").split(",")
        if country.strip()
    }

    payload = {
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "snapshot_unix": now_unix,
        "stats": {
            "astronauts": len(people_rows),
            "missions": len(mission_rows),
            "segments": len(segment_rows),
            "current": sum(1 for row in people_rows if row["is_currently_in_space"]),
            "countries": len(countries),
            "total_time_seconds": total_time_seconds,
        },
        "people": people,
        "missions": [mission_by_id[key] for key in sorted(mission_by_id)],
        "intervals": intervals,
        "segments": segments,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
