# Space People Data Store

This repository builds a local SQLite database for people who have been to space, with names, gender, Wikipedia links, total time in space, and per-person in-space segments.

## Build

```bash
python3 -m pip install -r requirements.txt
python3 scripts/build_space_data.py
```

The build writes:

- `data/space_people.sqlite`: normalized SQLite database
- `data/astronauts.csv`: quick export of the people table
- `data/space_time_segments.csv`: quick export of merged in-space segments
- `data/quality_report.json`: source coverage and caveats

## Tables

- `astronauts`: one row per person, keyed by Wikidata when available.
- `missions`: one row per source mission/flight.
- `mission_crew_intervals`: raw mission/person associations from Wikidata or synthesized from the first-flight list.
- `space_time_segments`: merged continuous intervals when a person was in space. This is the table the website should usually use for timeline visualizations.

Useful views:

- `astronaut_leaderboard`
- `current_spacefarers`

Example:

```bash
sqlite3 data/space_people.sqlite \
  "SELECT name, ROUND(time_in_space_seconds / 86400.0, 2) AS days FROM astronaut_leaderboard LIMIT 10;"
```

## Timestamp Policy

Unix timestamps are useful for sorting, filtering, and charting, so the database stores `*_unix` integer columns in UTC seconds.

They are not sufficient by themselves because some public sources only provide a date, while others provide exact launch and landing times. For that reason, the database also stores UTC ISO-8601 text columns such as `launch_at` and precision columns such as `launch_precision`.

Wikidata precision values are preserved:

- `11`: date-level precision
- `14`: second-level precision

For website work, use Unix seconds for calculations and use the ISO columns plus precision fields when displaying source-sensitive dates.

## Website

The static site is a Vite/React app. It exports the SQLite data into `public/data/space-data.json` during `npm run build`, then serves the site from `dist`.

```bash
npm install
npm run dev
npm run build
```

Railway can use:

- Build command: `npm run build`
- Start command: `npm start`

## Sources

The canonical person list comes from Wikipedia's FAI/Karman-line first-flight list. Wikidata enriches people with gender, total-time statements, sitelinks, and mission crew links. Mission pages on Wikipedia enrich exact launch, landing, and duration values when Wikidata only has date precision.
