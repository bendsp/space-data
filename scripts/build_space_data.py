#!/usr/bin/env python3
"""Build a local SQLite database of people who have crossed the Karman line.

Primary sources:
- Wikipedia: List of space travellers by first flight
- Wikidata: gender, sitelinks, total time in space, and structured mission links
- Wikipedia mission infoboxes: exact launch/landing/duration enrichment
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import re
import sqlite3
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import pandas as pd
from dateutil import parser as date_parser


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "space_people.sqlite"
SCHEMA_PATH = ROOT / "schema.sql"
QUALITY_PATH = DATA_DIR / "quality_report.json"
ASTRONAUTS_CSV = DATA_DIR / "astronauts.csv"
SEGMENTS_CSV = DATA_DIR / "space_time_segments.csv"

USER_AGENT = (
    "space-data-builder/0.1 "
    "(local data project; sources: en.wikipedia.org and wikidata.org)"
)
SNAPSHOT_AT = datetime.now(UTC).replace(microsecond=0)
SNAPSHOT_ISO = SNAPSHOT_AT.isoformat().replace("+00:00", "Z")
SNAPSHOT_UNIX = calendar.timegm(SNAPSHOT_AT.utctimetuple())

FIRST_FLIGHT_URL = "https://en.wikipedia.org/wiki/List_of_space_travellers_by_first_flight"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKI_API = "https://en.wikipedia.org/w/api.php"

# Wikipedia's first-flight page uses the FAI/Karman-line definition. These
# mission records are often present in Wikidata as "spaceflights" but did not
# cross 100 km, so they should not become "in space" segments for this dataset.
KNOWN_NON_KARMAN_MISSION_LABELS = {
    "STS-51-L",
    "Soyuz MS-10",
    "Soyuz T-10a",
}

UNIT_SECONDS = {
    "Q11574": Decimal("1"),      # second
    "Q7727": Decimal("60"),      # minute
    "Q25235": Decimal("3600"),   # hour
    "Q573": Decimal("86400"),    # day
    "Q23387": Decimal("86400"),  # day
}


@dataclass
class FirstFlightRow:
    rank: int
    name: str
    sort_name: str
    nationality: str | None
    first_flight_date: str
    first_flight_iso: str | None
    first_flight_unix: int | None
    first_flight_name: str
    person_wikipedia_url: str | None
    person_wiki_title: str | None
    mission_wikipedia_url: str | None
    mission_wiki_title: str | None
    person_qid: str | None = None
    mission_qid: str | None = None


@dataclass
class PersonMeta:
    qid: str
    name: str | None = None
    gender: str | None = None
    gender_qid: str | None = None
    wikipedia_url: str | None = None
    time_in_space_seconds: int | None = None


@dataclass
class MissionMeta:
    id: str
    name: str
    wikipedia_url: str | None = None
    wikidata_id: str | None = None
    launch_at: str | None = None
    launch_unix: int | None = None
    launch_precision: int | None = None
    landing_at: str | None = None
    landing_unix: int | None = None
    landing_precision: int | None = None
    duration_seconds: int | None = None
    source: str = "wikidata"
    not_flown: bool = False


@dataclass
class RawInterval:
    astronaut_id: str
    mission_id: str
    launch_at: str | None
    launch_unix: int | None
    launch_precision: int | None
    landing_at: str | None
    landing_unix: int | None
    landing_precision: int | None
    duration_seconds: int | None
    source: str
    is_current: bool = False


@dataclass
class Astronaut:
    id: str
    name: str
    sort_name: str
    gender: str | None
    gender_qid: str | None
    nationality: str | None
    wikipedia_url: str | None
    wikidata_id: str | None
    first_flight_rank: int | None
    first_flight_date: str | None
    first_flight_unix: int | None
    first_flight_name: str | None
    time_in_space_seconds: int | None = None
    time_in_space_source: str | None = None
    segment_count: int = 0
    is_currently_in_space: bool = False
    source: str = "wikipedia_first_flight"


def fetch_text(url: str, params: dict[str, Any] | None = None, retries: int = 3) -> str:
    if params:
        url = f"{url}?{urlencode(params)}"

    delay = 1.0
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=90) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            if error.code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                retry_after = error.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after), 60))
                else:
                    time.sleep(delay)
                delay *= 2
                continue
            raise
        except URLError:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise

    raise RuntimeError(f"Failed to fetch {url}")


def fetch_json(url: str, params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    return json.loads(fetch_text(url, params=params, retries=retries))


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).replace("\xa0", " ")
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_name_markers(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r"\s*[◉△▲†‡]+", "", name)
    return name.strip()


def sort_key_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(ch for ch in normalized if ord(ch) < 128)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug or "unknown"


def qid_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    match = re.search(r"/entity/(Q\d+)$", uri)
    return match.group(1) if match else None


def unit_qid_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    match = re.search(r"/entity/(Q\d+)$", uri)
    return match.group(1) if match else None


def quantity_to_seconds(value: str | None, unit_uri: str | None) -> int | None:
    if value is None:
        return None
    unit_qid = unit_qid_from_uri(unit_uri)
    multiplier = UNIT_SECONDS.get(unit_qid or "")
    if not multiplier:
        return None
    try:
        seconds = Decimal(value) * multiplier
    except InvalidOperation:
        return None
    return int(seconds.to_integral_value())


def iso_to_unix(iso_value: str | None) -> int | None:
    if not iso_value:
        return None
    try:
        cleaned = iso_value.replace("Z", "+00:00")
        value = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return calendar.timegm(value.astimezone(UTC).utctimetuple())


def unix_to_iso(unix_value: int | None) -> str | None:
    if unix_value is None:
        return None
    return datetime.fromtimestamp(unix_value, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_wiki_date(text: str) -> tuple[str | None, int | None, int | None]:
    cleaned = clean_text(text)
    if not cleaned or cleaned.lower() in {"tbd", "unknown"}:
        return None, None, None

    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    utc_index = cleaned.upper().find("UTC")
    if utc_index != -1:
        cleaned = cleaned[: utc_index + 3]
    cleaned = cleaned.replace(" UTC", " UTC ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    tzinfos = {
        "UTC": 0,
        "GMT": 0,
        "CST": -6 * 3600,
        "CDT": -5 * 3600,
        "EST": -5 * 3600,
        "EDT": -4 * 3600,
        "MST": -7 * 3600,
        "MDT": -6 * 3600,
        "PST": -8 * 3600,
        "PDT": -7 * 3600,
    }
    try:
        value = date_parser.parse(cleaned, fuzzy=True, tzinfos=tzinfos)
    except (ValueError, OverflowError):
        return None, None, None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    iso_value = value.isoformat().replace("+00:00", "Z")
    return iso_value, calendar.timegm(value.utctimetuple()), 14


def parse_date_only(text: str) -> tuple[str | None, int | None]:
    try:
        value = datetime.strptime(clean_text(text), "%d %B %Y").replace(tzinfo=UTC)
    except ValueError:
        return None, None
    return value.date().isoformat(), calendar.timegm(value.utctimetuple())


def parse_duration_seconds(text: str) -> int | None:
    cleaned = clean_text(text).lower()
    if not cleaned:
        return None

    # Keep the primary value when infoboxes list two alternative durations.
    first_sentence = re.split(r";|\bor\b", cleaned, maxsplit=1)[0]
    multipliers = {
        "day": 86400,
        "days": 86400,
        "hour": 3600,
        "hours": 3600,
        "minute": 60,
        "minutes": 60,
        "min": 60,
        "mins": 60,
        "second": 1,
        "seconds": 1,
        "sec": 1,
        "secs": 1,
    }
    total = 0
    found = False
    for number, unit in re.findall(
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(days?|hours?|minutes?|mins?|seconds?|secs?)",
        first_sentence,
    ):
        found = True
        total += int(Decimal(number.replace(",", "")) * multipliers[unit])
    return total if found else None


def wiki_url_from_path(path: str | None) -> str | None:
    if not path or not path.startswith("/wiki/"):
        return None
    if ":" in path.removeprefix("/wiki/"):
        return None
    return urljoin("https://en.wikipedia.org", path)


def wiki_title_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "en.wikipedia.org":
        return None
    path = parsed.path
    if not path.startswith("/wiki/"):
        return None
    return unquote(path.removeprefix("/wiki/")).replace("_", " ")


def sparql(query: str) -> list[dict[str, Any]]:
    data = fetch_json(WIKIDATA_SPARQL_URL, {"query": query, "format": "json"}, retries=4)
    return data["results"]["bindings"]


def get_binding(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value["value"] if value else None


def read_first_flight_rows() -> list[FirstFlightRow]:
    html = fetch_text(FIRST_FLIGHT_URL)
    tables = pd.read_html(StringIO(html), extract_links="all")
    table = next(
        candidate
        for candidate in tables
        if any(isinstance(col, tuple) and col[0] == "Name" for col in candidate.columns)
    )

    def col(name: str) -> Any:
        return next(candidate for candidate in table.columns if isinstance(candidate, tuple) and candidate[0] == name)

    rows: list[FirstFlightRow] = []
    for _, raw in table.iterrows():
        rank_text, _ = raw[col("#")]
        name_text, name_path = raw[col("Name")]
        nationality_text, _ = raw[col("Nationality")]
        date_text, _ = raw[col("Date")]
        flight_text, flight_path = raw[col("Flight")]

        rank_clean = clean_text(rank_text)
        if not rank_clean.isdigit():
            continue

        name = strip_name_markers(name_text)
        flight_name = clean_text(flight_text)
        mission_url = wiki_url_from_path(flight_path)
        person_url = wiki_url_from_path(name_path)
        if person_url == mission_url:
            person_url = None

        first_date_iso, first_date_unix = parse_date_only(date_text)
        rows.append(
            FirstFlightRow(
                rank=int(rank_clean),
                name=name,
                sort_name=sort_key_name(name),
                nationality=clean_text(nationality_text) or None,
                first_flight_date=clean_text(date_text),
                first_flight_iso=first_date_iso,
                first_flight_unix=first_date_unix,
                first_flight_name=flight_name,
                person_wikipedia_url=person_url,
                person_wiki_title=wiki_title_from_url(person_url),
                mission_wikipedia_url=mission_url,
                mission_wiki_title=wiki_title_from_url(mission_url),
            )
        )
    return rows


def title_tokens(title: str) -> list[str]:
    title = re.sub(r"\([^)]*\)", "", title)
    normalized = unicodedata.normalize("NFKD", title)
    ascii_value = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", ascii_value.lower())


def plausible_person_redirect(original: str, redirected: str) -> bool:
    original_tokens = title_tokens(original)
    redirected_tokens = title_tokens(redirected)
    if not original_tokens or not redirected_tokens:
        return False
    original_set = set(original_tokens)
    redirected_set = set(redirected_tokens)
    if original_set == redirected_set:
        return True
    if redirected_set.issubset(original_set):
        return True
    if original_tokens[-1] == redirected_tokens[-1]:
        original_first = original_tokens[0]
        redirected_first = redirected_tokens[0]
        if original_first[0] == redirected_first[0]:
            return True
        if original_first.startswith(redirected_first) or redirected_first.startswith(original_first):
            return True
    return False


def filter_human_qids(qids: set[str]) -> set[str]:
    if not qids:
        return set()
    humans: set[str] = set()
    qid_list = sorted(qids)
    for start in range(0, len(qid_list), 200):
        values = " ".join(f"wd:{qid}" for qid in qid_list[start : start + 200])
        query = f"""
SELECT ?item WHERE {{
  VALUES ?item {{ {values} }}
  ?item wdt:P31 wd:Q5.
}}
"""
        for row in sparql(query):
            qid = qid_from_uri(get_binding(row, "item"))
            if qid:
                humans.add(qid)
    return humans


def map_titles_to_qids(titles: set[str], expected_kind: str | None = None) -> dict[str, str]:
    titles = {title for title in titles if title}
    result: dict[str, str] = {}
    title_list = sorted(titles)
    for start in range(0, len(title_list), 150):
        chunk = title_list[start : start + 150]
        values = " ".join(f"{json.dumps(title, ensure_ascii=False)}@en" for title in chunk)
        query = f"""
SELECT ?title ?item WHERE {{
  VALUES ?title {{ {values} }}
  ?article schema:name ?title;
           schema:isPartOf <https://en.wikipedia.org/>;
           schema:about ?item.
}}
"""
        for row in sparql(query):
            title = get_binding(row, "title")
            qid = qid_from_uri(get_binding(row, "item"))
            if title and qid:
                result[title] = qid

    if expected_kind == "human":
        human_qids = filter_human_qids(set(result.values()))
        result = {title: qid for title, qid in result.items() if qid in human_qids}

    unresolved = sorted(titles - set(result))
    for start in range(0, len(unresolved), 50):
        chunk = unresolved[start : start + 50]
        data = fetch_json(
            WIKI_API,
            {
                "action": "query",
                "format": "json",
                "prop": "pageprops",
                "titles": "|".join(chunk),
                "redirects": "1",
            },
            retries=5,
        )

        redirect_map = {item["from"]: item["to"] for item in data.get("query", {}).get("redirects", [])}
        returned: dict[str, tuple[str, str]] = {}
        for page in data.get("query", {}).get("pages", {}).values():
            qid = page.get("pageprops", {}).get("wikibase_item")
            title = page.get("title")
            if title and qid:
                returned[title] = (qid, title)

        fallback_candidates: dict[str, str] = {}

        for original in chunk:
            redirected = redirect_map.get(original, original)
            if redirected in returned:
                qid, returned_title = returned[redirected]
                if expected_kind == "human" and not plausible_person_redirect(original, returned_title):
                    continue
                fallback_candidates[original] = qid

        if expected_kind == "human":
            human_qids = filter_human_qids(set(fallback_candidates.values()))
            fallback_candidates = {
                title: qid for title, qid in fallback_candidates.items() if qid in human_qids
            }
        result.update(fallback_candidates)
        time.sleep(0.5)
    return result


def fetch_person_metadata(qids: set[str]) -> dict[str, PersonMeta]:
    result: dict[str, PersonMeta] = {}
    qid_list = sorted(qids)
    for start in range(0, len(qid_list), 150):
        values = " ".join(f"wd:{qid}" for qid in qid_list[start : start + 150])
        query = f"""
SELECT ?person ?personLabel ?gender ?genderLabel ?article ?time ?timeUnit WHERE {{
  VALUES ?person {{ {values} }}
  OPTIONAL {{ ?person wdt:P21 ?gender. }}
  OPTIONAL {{
    ?person p:P2873 ?timeStatement.
    ?timeStatement ps:P2873 ?time;
                   psv:P2873 ?timeNode.
    ?timeNode wikibase:quantityUnit ?timeUnit.
  }}
  OPTIONAL {{
    ?article schema:about ?person;
             schema:isPartOf <https://en.wikipedia.org/>.
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""
        for row in sparql(query):
            qid = qid_from_uri(get_binding(row, "person"))
            if not qid:
                continue
            meta = result.setdefault(qid, PersonMeta(qid=qid))
            meta.name = meta.name or get_binding(row, "personLabel")
            meta.gender = meta.gender or get_binding(row, "genderLabel")
            meta.gender_qid = meta.gender_qid or qid_from_uri(get_binding(row, "gender"))
            meta.wikipedia_url = meta.wikipedia_url or get_binding(row, "article")
            seconds = quantity_to_seconds(get_binding(row, "time"), get_binding(row, "timeUnit"))
            if seconds is not None and (meta.time_in_space_seconds is None or seconds > meta.time_in_space_seconds):
                meta.time_in_space_seconds = seconds
    return result


def fetch_wikidata_mission_rows() -> tuple[dict[str, MissionMeta], list[tuple[str, str]]]:
    query = """
SELECT ?mission ?missionLabel ?missionArticle ?person ?personLabel ?personArticle
       ?launch ?launchPrecision ?landing ?landingPrecision
       ?duration ?durationUnit WHERE {
  ?mission wdt:P31/wdt:P279* wd:Q5916;
           wdt:P1029 ?person.
  ?person wdt:P31 wd:Q5.
  OPTIONAL {
    ?mArticle schema:about ?mission;
              schema:isPartOf <https://en.wikipedia.org/>.
    BIND(STR(?mArticle) AS ?missionArticle)
  }
  OPTIONAL {
    ?pArticle schema:about ?person;
              schema:isPartOf <https://en.wikipedia.org/>.
    BIND(STR(?pArticle) AS ?personArticle)
  }
  OPTIONAL {
    ?mission p:P619/psv:P619 ?launchNode.
    ?launchNode wikibase:timeValue ?launch;
                wikibase:timePrecision ?launchPrecision.
  }
  OPTIONAL {
    ?mission p:P620/psv:P620 ?landingNode.
    ?landingNode wikibase:timeValue ?landing;
                 wikibase:timePrecision ?landingPrecision.
  }
  OPTIONAL {
    ?mission p:P2047 ?durationStatement.
    ?durationStatement ps:P2047 ?duration;
                       psv:P2047 ?durationNode.
    ?durationNode wikibase:quantityUnit ?durationUnit.
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""
    missions: dict[str, MissionMeta] = {}
    crew_pairs: set[tuple[str, str]] = set()

    for row in sparql(query):
        mission_qid = qid_from_uri(get_binding(row, "mission"))
        person_qid = qid_from_uri(get_binding(row, "person"))
        if not mission_qid or not person_qid:
            continue

        name = get_binding(row, "missionLabel") or mission_qid
        if name in KNOWN_NON_KARMAN_MISSION_LABELS:
            continue

        mission_id = f"wikidata:{mission_qid}"
        mission = missions.setdefault(
            mission_id,
            MissionMeta(
                id=mission_id,
                name=name,
                wikipedia_url=get_binding(row, "missionArticle"),
                wikidata_id=mission_qid,
            ),
        )
        mission.wikipedia_url = mission.wikipedia_url or get_binding(row, "missionArticle")

        launch = get_binding(row, "launch")
        landing = get_binding(row, "landing")
        if launch and not mission.launch_at:
            mission.launch_at = launch
            mission.launch_unix = iso_to_unix(launch)
            mission.launch_precision = int(get_binding(row, "launchPrecision") or 0) or None
        if landing and not mission.landing_at:
            mission.landing_at = landing
            mission.landing_unix = iso_to_unix(landing)
            mission.landing_precision = int(get_binding(row, "landingPrecision") or 0) or None

        seconds = quantity_to_seconds(get_binding(row, "duration"), get_binding(row, "durationUnit"))
        if seconds is not None and (mission.duration_seconds is None or seconds > mission.duration_seconds):
            mission.duration_seconds = seconds

        crew_pairs.add((mission_id, f"wikidata:{person_qid}"))

    return missions, sorted(crew_pairs)


def extract_infobox_fields(html: str) -> dict[str, str]:
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return {}

    best: dict[str, str] = {}
    for table in tables[:3]:
        if table.shape[1] < 2:
            continue
        fields: dict[str, str] = {}
        for _, row in table.iterrows():
            key = clean_text(row.iloc[0])
            value = clean_text(row.iloc[1])
            if key and value and key != value:
                fields[key.lower()] = value
        if any(key in fields for key in ("launch date", "landing date", "mission duration")):
            best = fields
            break
    return best


def fetch_mission_infobox(article_url: str) -> tuple[str, dict[str, Any]]:
    try:
        html = fetch_text(article_url, retries=2)
    except Exception as error:
        return article_url, {"error": str(error)}

    fields = extract_infobox_fields(html)
    result: dict[str, Any] = {"fields_found": sorted(fields.keys())}
    status_text = " ".join(fields.values()).lower()
    if any(word in status_text for word in ("cancelled", "canceled", "not flown")):
        result["not_flown"] = True
    if (
        "mission duration" in fields
        and "planned" in fields["mission duration"].lower()
        and "in progress" not in fields["mission duration"].lower()
    ):
        result["not_flown"] = True
    if "launch date" in fields and any(word in fields["launch date"].lower() for word in ("cancelled", "canceled")):
        result["not_flown"] = True

    for key in ("launch date", "launch"):
        if key in fields:
            iso_value, unix_value, precision = parse_wiki_date(fields[key])
            if iso_value:
                result["launch_at"] = iso_value
                result["launch_unix"] = unix_value
                result["launch_precision"] = precision
                break

    for key in ("landing date", "recovery date", "splashdown"):
        if key in fields:
            iso_value, unix_value, precision = parse_wiki_date(fields[key])
            if iso_value:
                result["landing_at"] = iso_value
                result["landing_unix"] = unix_value
                result["landing_precision"] = precision
                break

    if "mission duration" in fields:
        result["duration_seconds"] = parse_duration_seconds(fields["mission duration"])

    return article_url, result


def fetch_infobox_enrichment(urls: set[str], workers: int) -> dict[str, dict[str, Any]]:
    urls = {url for url in urls if url}
    enrichment: dict[str, dict[str, Any]] = {}
    if not urls:
        return enrichment

    print(f"Fetching {len(urls)} Wikipedia mission infoboxes...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_url = {executor.submit(fetch_mission_infobox, url): url for url in sorted(urls)}
        completed = 0
        for future in as_completed(future_to_url):
            url, info = future.result()
            enrichment[url] = info
            completed += 1
            if completed % 50 == 0 or completed == len(urls):
                print(f"  {completed}/{len(urls)} mission pages processed")
    return enrichment


def apply_mission_enrichment(missions: dict[str, MissionMeta], enrichment: dict[str, dict[str, Any]]) -> None:
    for mission in missions.values():
        if not mission.wikipedia_url:
            continue
        info = enrichment.get(mission.wikipedia_url)
        if not info or info.get("error"):
            continue
        if info.get("not_flown"):
            mission.not_flown = True
            mission.source = "wikidata+wikipedia_infobox:not_flown"
            continue

        changed = False
        if info.get("launch_at"):
            mission.launch_at = info["launch_at"]
            mission.launch_unix = info["launch_unix"]
            mission.launch_precision = info["launch_precision"]
            changed = True
        if info.get("landing_at"):
            mission.landing_at = info["landing_at"]
            mission.landing_unix = info["landing_unix"]
            mission.landing_precision = info["landing_precision"]
            changed = True
        if info.get("duration_seconds"):
            mission.duration_seconds = info["duration_seconds"]
            changed = True
        if changed:
            mission.source = "wikidata+wikipedia_infobox"

        if mission.duration_seconds is None and mission.launch_unix is not None and mission.landing_unix is not None:
            duration = mission.landing_unix - mission.launch_unix
            if duration > 0:
                mission.duration_seconds = duration


def choose_astronaut_id(row: FirstFlightRow) -> str:
    if row.person_qid:
        return f"wikidata:{row.person_qid}"
    return f"wikipedia-first-flight:{row.rank}:{slugify(row.name)}"


def choose_mission_id(row: FirstFlightRow) -> str:
    if row.mission_qid:
        return f"wikidata:{row.mission_qid}"
    return f"wikipedia-mission:{slugify(row.first_flight_name)}"


def build_astronauts(
    first_rows: list[FirstFlightRow],
    person_meta: dict[str, PersonMeta],
    wikidata_crew_pairs: list[tuple[str, str]],
    missions: dict[str, MissionMeta],
) -> dict[str, Astronaut]:
    astronauts: dict[str, Astronaut] = {}

    for row in first_rows:
        person_id = choose_astronaut_id(row)
        meta = person_meta.get(row.person_qid or "")
        astronauts[person_id] = Astronaut(
            id=person_id,
            name=row.name,
            sort_name=row.sort_name,
            gender=meta.gender if meta else None,
            gender_qid=meta.gender_qid if meta else None,
            nationality=row.nationality,
            wikipedia_url=row.person_wikipedia_url or (meta.wikipedia_url if meta else None),
            wikidata_id=row.person_qid,
            first_flight_rank=row.rank,
            first_flight_date=row.first_flight_date,
            first_flight_unix=row.first_flight_unix,
            first_flight_name=row.first_flight_name,
            time_in_space_seconds=meta.time_in_space_seconds if meta else None,
            time_in_space_source="wikidata:P2873" if meta and meta.time_in_space_seconds is not None else None,
        )

    return astronauts


def ensure_first_flight_missions(first_rows: list[FirstFlightRow], missions: dict[str, MissionMeta], enrichment: dict[str, dict[str, Any]]) -> None:
    for row in first_rows:
        mission_id = choose_mission_id(row)
        if mission_id in missions:
            continue
        mission = MissionMeta(
            id=mission_id,
            name=row.first_flight_name,
            wikipedia_url=row.mission_wikipedia_url,
            wikidata_id=row.mission_qid,
            launch_at=unix_to_iso(row.first_flight_unix) if row.first_flight_unix else None,
            launch_unix=row.first_flight_unix,
            launch_precision=11 if row.first_flight_unix else None,
            source="wikipedia_first_flight",
        )
        if row.mission_wikipedia_url and row.mission_wikipedia_url in enrichment:
            info = enrichment[row.mission_wikipedia_url]
            if info.get("not_flown"):
                mission.not_flown = True
                mission.source = "wikipedia_first_flight+wikipedia_infobox:not_flown"
            if info.get("launch_at"):
                mission.launch_at = info["launch_at"]
                mission.launch_unix = info["launch_unix"]
                mission.launch_precision = info["launch_precision"]
                mission.source = "wikipedia_first_flight+wikipedia_infobox"
            if info.get("landing_at"):
                mission.landing_at = info["landing_at"]
                mission.landing_unix = info["landing_unix"]
                mission.landing_precision = info["landing_precision"]
                mission.source = "wikipedia_first_flight+wikipedia_infobox"
            if info.get("duration_seconds"):
                mission.duration_seconds = info["duration_seconds"]
                mission.source = "wikipedia_first_flight+wikipedia_infobox"
        if mission.duration_seconds is None and mission.launch_unix is not None and mission.landing_unix is not None:
            mission.duration_seconds = mission.landing_unix - mission.launch_unix
        missions[mission_id] = mission


def build_raw_intervals(
    astronauts: dict[str, Astronaut],
    missions: dict[str, MissionMeta],
    wikidata_crew_pairs: list[tuple[str, str]],
    first_rows: list[FirstFlightRow],
) -> list[RawInterval]:
    intervals: dict[tuple[str, str, str], RawInterval] = {}

    for mission_id, person_id in wikidata_crew_pairs:
        if person_id not in astronauts:
            continue
        mission = missions.get(mission_id)
        if not mission or mission.name in KNOWN_NON_KARMAN_MISSION_LABELS:
            continue
        if mission.not_flown:
            continue
        if mission.launch_unix is None and mission.duration_seconds is None:
            continue
        landing_unix = mission.landing_unix
        landing_at = mission.landing_at
        landing_precision = mission.landing_precision
        if landing_unix is None and mission.launch_unix is not None and mission.duration_seconds is not None:
            landing_unix = mission.launch_unix + mission.duration_seconds
            landing_at = unix_to_iso(landing_unix)
            landing_precision = mission.launch_precision
        is_current = bool(
            mission.launch_unix
            and mission.launch_unix <= SNAPSHOT_UNIX
            and (landing_unix is None or landing_unix > SNAPSHOT_UNIX)
            and mission.launch_unix >= SNAPSHOT_UNIX - (730 * 86400)
        )
        intervals[(person_id, mission_id, "wikidata_crew")] = RawInterval(
            astronaut_id=person_id,
            mission_id=mission_id,
            launch_at=mission.launch_at,
            launch_unix=mission.launch_unix,
            launch_precision=mission.launch_precision,
            landing_at=landing_at,
            landing_unix=landing_unix,
            landing_precision=landing_precision,
            duration_seconds=mission.duration_seconds,
            source=mission.source,
            is_current=is_current,
        )

    for row in first_rows:
        person_id = choose_astronaut_id(row)
        mission_id = choose_mission_id(row)
        if person_id not in astronauts or mission_id not in missions:
            continue
        if any(key[0] == person_id and key[1] == mission_id for key in intervals):
            continue
        mission = missions[mission_id]
        if mission.not_flown:
            continue
        landing_unix = mission.landing_unix
        landing_at = mission.landing_at
        landing_precision = mission.landing_precision
        if landing_unix is None and mission.launch_unix is not None and mission.duration_seconds is not None:
            landing_unix = mission.launch_unix + mission.duration_seconds
            landing_at = unix_to_iso(landing_unix)
            landing_precision = mission.launch_precision
        is_current = bool(
            mission.launch_unix
            and mission.launch_unix <= SNAPSHOT_UNIX
            and (landing_unix is None or landing_unix > SNAPSHOT_UNIX)
            and mission.launch_unix >= SNAPSHOT_UNIX - (730 * 86400)
        )
        intervals[(person_id, mission_id, "wikipedia_first_flight")] = RawInterval(
            astronaut_id=person_id,
            mission_id=mission_id,
            launch_at=mission.launch_at,
            launch_unix=mission.launch_unix,
            launch_precision=mission.launch_precision,
            landing_at=landing_at,
            landing_unix=landing_unix,
            landing_precision=landing_precision,
            duration_seconds=mission.duration_seconds,
            source=f"synthesized_first_flight:{mission.source}",
            is_current=is_current,
        )

    return sorted(intervals.values(), key=lambda item: (item.astronaut_id, item.launch_unix or 0, item.mission_id))


def merge_segments(intervals: list[RawInterval]) -> dict[str, list[dict[str, Any]]]:
    by_person: dict[str, list[tuple[int, RawInterval]]] = {}
    for index, interval in enumerate(intervals, start=1):
        if interval.launch_unix is None:
            continue
        by_person.setdefault(interval.astronaut_id, []).append((index, interval))

    gap_seconds = 48 * 3600
    merged: dict[str, list[dict[str, Any]]] = {}
    for astronaut_id, person_intervals in by_person.items():
        person_intervals.sort(key=lambda item: (item[1].launch_unix or 0, item[1].landing_unix or SNAPSHOT_UNIX))
        segments: list[dict[str, Any]] = []

        for raw_id, interval in person_intervals:
            start = interval.launch_unix
            end = interval.landing_unix
            if start is None:
                continue
            current = interval.is_current
            effective_end = end if end is not None else (SNAPSHOT_UNIX if current else start)

            if not segments:
                segments.append(
                    {
                        "start_unix": start,
                        "start_at": interval.launch_at or unix_to_iso(start),
                        "start_precision": interval.launch_precision,
                        "end_unix": None if current else end,
                        "end_at": None if current else interval.landing_at,
                        "end_precision": None if current else interval.landing_precision,
                        "effective_end": effective_end,
                        "is_current": current,
                        "raw_ids": [raw_id],
                    }
                )
                continue

            last = segments[-1]
            last_effective_end = last["effective_end"]
            if start <= last_effective_end + gap_seconds:
                if effective_end > last_effective_end:
                    last["effective_end"] = effective_end
                    last["end_unix"] = end
                    last["end_at"] = interval.landing_at
                    last["end_precision"] = interval.landing_precision
                if current:
                    last["is_current"] = True
                    last["end_unix"] = None
                    last["end_at"] = None
                    last["end_precision"] = None
                    last["effective_end"] = SNAPSHOT_UNIX
                last["raw_ids"].append(raw_id)
            else:
                segments.append(
                    {
                        "start_unix": start,
                        "start_at": interval.launch_at or unix_to_iso(start),
                        "start_precision": interval.launch_precision,
                        "end_unix": None if current else end,
                        "end_at": None if current else interval.landing_at,
                        "end_precision": None if current else interval.landing_precision,
                        "effective_end": effective_end,
                        "is_current": current,
                        "raw_ids": [raw_id],
                    }
                )

        for segment in segments:
            if segment["is_current"]:
                segment["duration_seconds"] = SNAPSHOT_UNIX - segment["start_unix"]
            elif segment["end_unix"] is not None:
                segment["duration_seconds"] = segment["end_unix"] - segment["start_unix"]
            else:
                segment["duration_seconds"] = None
            segment.pop("effective_end", None)

        merged[astronaut_id] = segments
    return merged


def update_astronaut_totals(astronauts: dict[str, Astronaut], segments: dict[str, list[dict[str, Any]]]) -> None:
    for astronaut_id, astronaut in astronauts.items():
        person_segments = segments.get(astronaut_id, [])
        segment_total = sum(segment["duration_seconds"] or 0 for segment in person_segments)
        has_segment_total = any(segment["duration_seconds"] is not None for segment in person_segments)
        astronaut.segment_count = len(person_segments)
        astronaut.is_currently_in_space = any(segment["is_current"] for segment in person_segments)

        if astronaut.time_in_space_seconds is None and has_segment_total:
            astronaut.time_in_space_seconds = segment_total
            astronaut.time_in_space_source = "computed_from_segments"
        elif astronaut.is_currently_in_space and has_segment_total:
            # Wikidata total-time values are often static; for people currently
            # in space the snapshot segment total is more useful for the site.
            astronaut.time_in_space_seconds = max(astronaut.time_in_space_seconds or 0, segment_total)
            astronaut.time_in_space_source = "computed_from_segments_snapshot"


def write_database(
    astronauts: dict[str, Astronaut],
    missions: dict[str, MissionMeta],
    intervals: list[RawInterval],
    segments: dict[str, list[dict[str, Any]]],
    quality: dict[str, Any],
) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.executemany(
            """
            INSERT INTO astronauts (
                id, name, sort_name, gender, gender_wikidata_id, nationality,
                wikipedia_url, wikidata_id, first_flight_rank, first_flight_date,
                first_flight_unix, first_flight_name, time_in_space_seconds,
                time_in_space_source, segment_count, is_currently_in_space,
                source, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    astronaut.id,
                    astronaut.name,
                    astronaut.sort_name,
                    astronaut.gender,
                    astronaut.gender_qid,
                    astronaut.nationality,
                    astronaut.wikipedia_url,
                    astronaut.wikidata_id,
                    astronaut.first_flight_rank,
                    astronaut.first_flight_date,
                    astronaut.first_flight_unix,
                    astronaut.first_flight_name,
                    astronaut.time_in_space_seconds,
                    astronaut.time_in_space_source,
                    astronaut.segment_count,
                    int(astronaut.is_currently_in_space),
                    astronaut.source,
                    SNAPSHOT_ISO,
                )
                for astronaut in sorted(astronauts.values(), key=lambda item: item.sort_name)
            ],
        )
        conn.executemany(
            """
            INSERT INTO missions (
                id, name, wikipedia_url, wikidata_id, launch_at, launch_unix,
                launch_precision, landing_at, landing_unix, landing_precision,
                duration_seconds, source, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    mission.id,
                    mission.name,
                    mission.wikipedia_url,
                    mission.wikidata_id,
                    mission.launch_at,
                    mission.launch_unix,
                    mission.launch_precision,
                    mission.landing_at,
                    mission.landing_unix,
                    mission.landing_precision,
                    mission.duration_seconds,
                    mission.source,
                    SNAPSHOT_ISO,
                )
                for mission in sorted(missions.values(), key=lambda item: item.name)
            ],
        )
        conn.executemany(
            """
            INSERT INTO mission_crew_intervals (
                astronaut_id, mission_id, launch_at, launch_unix, launch_precision,
                landing_at, landing_unix, landing_precision, duration_seconds,
                is_current, source, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    interval.astronaut_id,
                    interval.mission_id,
                    interval.launch_at,
                    interval.launch_unix,
                    interval.launch_precision,
                    interval.landing_at,
                    interval.landing_unix,
                    interval.landing_precision,
                    interval.duration_seconds,
                    int(interval.is_current),
                    interval.source,
                    SNAPSHOT_ISO,
                )
                for interval in intervals
            ],
        )

        segment_rows = []
        for astronaut_id, person_segments in segments.items():
            for segment_index, segment in enumerate(person_segments, start=1):
                segment_rows.append(
                    (
                        astronaut_id,
                        segment_index,
                        segment["start_at"],
                        segment["start_unix"],
                        segment["start_precision"],
                        segment["end_at"],
                        segment["end_unix"],
                        segment["end_precision"],
                        segment["duration_seconds"],
                        int(segment["is_current"]),
                        ",".join(str(raw_id) for raw_id in segment["raw_ids"]),
                        SNAPSHOT_ISO,
                    )
                )
        conn.executemany(
            """
            INSERT INTO space_time_segments (
                astronaut_id, segment_index, start_at, start_unix, start_precision,
                end_at, end_unix, end_precision, duration_seconds, is_current,
                source_interval_ids, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            segment_rows,
        )
        conn.executemany(
            """
            INSERT INTO source_events (source_name, source_url, fetched_at, detail)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    "Wikipedia first-flight list",
                    FIRST_FLIGHT_URL,
                    SNAPSHOT_ISO,
                    json.dumps({"rows": quality["wikipedia_first_flight_rows"]}, sort_keys=True),
                ),
                (
                    "Wikidata SPARQL",
                    WIKIDATA_SPARQL_URL,
                    SNAPSHOT_ISO,
                    json.dumps({"mission_crew_pairs": quality["wikidata_crew_pairs"]}, sort_keys=True),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def write_csv_exports() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for path, query in [
            (
                ASTRONAUTS_CSV,
                """
                SELECT id, name, gender, nationality, wikipedia_url, wikidata_id,
                       first_flight_rank, first_flight_date, first_flight_name,
                       time_in_space_seconds, time_in_space_source,
                       segment_count, is_currently_in_space
                FROM astronauts
                ORDER BY first_flight_rank IS NULL, first_flight_rank, name
                """,
            ),
            (
                SEGMENTS_CSV,
                """
                SELECT s.astronaut_id, a.name, s.segment_index, s.start_at,
                       s.start_unix, s.end_at, s.end_unix, s.duration_seconds,
                       s.is_current
                FROM space_time_segments s
                JOIN astronauts a ON a.id = s.astronaut_id
                ORDER BY a.first_flight_rank IS NULL, a.first_flight_rank, a.name, s.segment_index
                """,
            ),
        ]:
            rows = conn.execute(query).fetchall()
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if rows:
                    writer.writerow(rows[0].keys())
                    writer.writerows([list(row) for row in rows])
    finally:
        conn.close()


def build_quality_report(
    first_rows: list[FirstFlightRow],
    astronauts: dict[str, Astronaut],
    missions: dict[str, MissionMeta],
    intervals: list[RawInterval],
    segments: dict[str, list[dict[str, Any]]],
    wikidata_crew_pairs: list[tuple[str, str]],
    enrichment: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing_gender = sorted(a.name for a in astronauts.values() if not a.gender)
    missing_wiki = sorted(a.name for a in astronauts.values() if not a.wikipedia_url)
    missing_segments = sorted(a.name for a in astronauts.values() if not segments.get(a.id))
    missing_total = sorted(a.name for a in astronauts.values() if a.time_in_space_seconds is None)
    current = sorted(a.name for a in astronauts.values() if a.is_currently_in_space)
    enriched = sum(1 for info in enrichment.values() if not info.get("error"))

    return {
        "snapshot_at": SNAPSHOT_ISO,
        "definition": "People listed by Wikipedia's 'List of space travellers by first flight' using the FAI/Karman-line criterion, plus structured Wikidata mission data.",
        "timestamp_policy": "All instants are stored as UTC ISO-8601 text plus Unix seconds. Wikidata precision is stored separately; precision 11 means date-only and precision 14 means second-level.",
        "wikipedia_first_flight_rows": len(first_rows),
        "astronauts": len(astronauts),
        "missions": len(missions),
        "wikidata_crew_pairs": len(wikidata_crew_pairs),
        "raw_mission_crew_intervals": len(intervals),
        "space_time_segments": sum(len(items) for items in segments.values()),
        "wikipedia_mission_infoboxes_requested": len(enrichment),
        "wikipedia_mission_infoboxes_enriched": enriched,
        "current_spacefarers": current,
        "missing_gender_count": len(missing_gender),
        "missing_gender_sample": missing_gender[:50],
        "missing_wikipedia_url_count": len(missing_wiki),
        "missing_wikipedia_url_sample": missing_wiki[:50],
        "missing_segment_count": len(missing_segments),
        "missing_segment_sample": missing_segments[:50],
        "missing_time_in_space_count": len(missing_total),
        "missing_time_in_space_sample": missing_total[:50],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Output SQLite path")
    parser.add_argument(
        "--infobox-workers",
        type=int,
        default=3,
        help="Concurrent Wikipedia mission infobox fetches",
    )
    return parser.parse_args()


def main() -> int:
    global DB_PATH
    args = parse_args()
    DB_PATH = args.db

    DATA_DIR.mkdir(exist_ok=True)
    print("Fetching canonical first-flight list from Wikipedia...")
    first_rows = read_first_flight_rows()

    person_titles = {row.person_wiki_title for row in first_rows if row.person_wiki_title}
    mission_titles = {row.mission_wiki_title for row in first_rows if row.mission_wiki_title}
    print(
        f"Resolving {len(person_titles)} person titles and "
        f"{len(mission_titles)} mission titles to Wikidata ids..."
    )
    person_title_qids = map_titles_to_qids(person_titles, expected_kind="human")
    mission_title_qids = map_titles_to_qids(mission_titles)
    for row in first_rows:
        if row.person_wiki_title:
            row.person_qid = person_title_qids.get(row.person_wiki_title)
        if row.mission_wiki_title:
            row.mission_qid = mission_title_qids.get(row.mission_wiki_title)

    print("Fetching Wikidata mission crew graph...")
    missions, wikidata_crew_pairs = fetch_wikidata_mission_rows()

    person_qids = {row.person_qid for row in first_rows if row.person_qid}
    person_qids.update(person_id.removeprefix("wikidata:") for _, person_id in wikidata_crew_pairs)
    print(f"Fetching Wikidata metadata for {len(person_qids)} people...")
    person_meta = fetch_person_metadata(person_qids)

    mission_urls = {mission.wikipedia_url for mission in missions.values() if mission.wikipedia_url}
    mission_urls.update(row.mission_wikipedia_url for row in first_rows if row.mission_wikipedia_url)
    enrichment = fetch_infobox_enrichment(mission_urls, workers=max(1, args.infobox_workers))
    apply_mission_enrichment(missions, enrichment)
    ensure_first_flight_missions(first_rows, missions, enrichment)

    astronauts = build_astronauts(first_rows, person_meta, wikidata_crew_pairs, missions)
    intervals = build_raw_intervals(astronauts, missions, wikidata_crew_pairs, first_rows)
    segments = merge_segments(intervals)
    update_astronaut_totals(astronauts, segments)

    quality = build_quality_report(first_rows, astronauts, missions, intervals, segments, wikidata_crew_pairs, enrichment)
    write_database(astronauts, missions, intervals, segments, quality)
    write_csv_exports()
    QUALITY_PATH.write_text(json.dumps(quality, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {DB_PATH}")
    print(f"Wrote {QUALITY_PATH}")
    print(f"Wrote {ASTRONAUTS_CSV}")
    print(f"Wrote {SEGMENTS_CSV}")
    print(
        "Summary: "
        f"{quality['astronauts']} astronauts, "
        f"{quality['space_time_segments']} merged in-space segments, "
        f"{quality['raw_mission_crew_intervals']} raw mission intervals."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
