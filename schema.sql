PRAGMA foreign_keys = ON;

DROP VIEW IF EXISTS astronaut_leaderboard;
DROP VIEW IF EXISTS current_spacefarers;
DROP TABLE IF EXISTS source_events;
DROP TABLE IF EXISTS space_time_segments;
DROP TABLE IF EXISTS mission_crew_intervals;
DROP TABLE IF EXISTS missions;
DROP TABLE IF EXISTS astronauts;

CREATE TABLE astronauts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sort_name TEXT NOT NULL,
    gender TEXT,
    gender_wikidata_id TEXT,
    nationality TEXT,
    wikipedia_url TEXT,
    wikidata_id TEXT UNIQUE,
    first_flight_rank INTEGER,
    first_flight_date TEXT,
    first_flight_unix INTEGER,
    first_flight_name TEXT,
    time_in_space_seconds INTEGER,
    time_in_space_source TEXT,
    segment_count INTEGER NOT NULL DEFAULT 0,
    is_currently_in_space INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE missions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    wikipedia_url TEXT,
    wikidata_id TEXT UNIQUE,
    launch_at TEXT,
    launch_unix INTEGER,
    launch_precision INTEGER,
    landing_at TEXT,
    landing_unix INTEGER,
    landing_precision INTEGER,
    duration_seconds INTEGER,
    source TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE mission_crew_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    astronaut_id TEXT NOT NULL REFERENCES astronauts(id) ON DELETE CASCADE,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    launch_at TEXT,
    launch_unix INTEGER,
    launch_precision INTEGER,
    landing_at TEXT,
    landing_unix INTEGER,
    landing_precision INTEGER,
    duration_seconds INTEGER,
    is_current INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (astronaut_id, mission_id, source)
);

CREATE TABLE space_time_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    astronaut_id TEXT NOT NULL REFERENCES astronauts(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    start_unix INTEGER NOT NULL,
    start_precision INTEGER,
    end_at TEXT,
    end_unix INTEGER,
    end_precision INTEGER,
    duration_seconds INTEGER,
    is_current INTEGER NOT NULL DEFAULT 0,
    source_interval_ids TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (astronaut_id, segment_index)
);

CREATE TABLE source_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX idx_astronauts_name ON astronauts (sort_name);
CREATE INDEX idx_astronauts_time ON astronauts (time_in_space_seconds DESC);
CREATE INDEX idx_intervals_astronaut ON mission_crew_intervals (astronaut_id, launch_unix);
CREATE INDEX idx_segments_astronaut ON space_time_segments (astronaut_id, start_unix);
CREATE INDEX idx_segments_current ON space_time_segments (is_current, start_unix);
CREATE INDEX idx_missions_launch ON missions (launch_unix);

CREATE VIEW astronaut_leaderboard AS
SELECT
    id,
    name,
    gender,
    nationality,
    wikipedia_url,
    time_in_space_seconds,
    ROUND(time_in_space_seconds / 86400.0, 3) AS time_in_space_days,
    segment_count,
    is_currently_in_space
FROM astronauts
ORDER BY time_in_space_seconds DESC NULLS LAST, name;

CREATE VIEW current_spacefarers AS
SELECT
    a.id,
    a.name,
    a.gender,
    a.nationality,
    a.wikipedia_url,
    s.start_at,
    s.start_unix,
    s.duration_seconds
FROM astronauts a
JOIN space_time_segments s ON s.astronaut_id = a.id
WHERE s.is_current = 1
ORDER BY s.start_unix, a.name;
